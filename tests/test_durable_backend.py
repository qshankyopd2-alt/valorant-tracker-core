from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
import knowledge_store
import live_match
import riot_client
import tracker_runtime


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.previous = knowledge_store._PATH
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "valorant_tracker.sqlite3"
        knowledge_store.init(self.path, migrate=False)

    def tearDown(self):
        knowledge_store.init(self.previous, migrate=False)
        self.temp.cleanup()

    @staticmethod
    def match(match_id="match-1", result="Victory"):
        return {
            "matchId": match_id, "startedAt": 100, "map": "Ascent",
            "mode": "Competitive", "scores": {"Blue": 13, "Red": 8},
            "players": [
                {"puuid": "owner", "team": "Blue", "name": "Owner#EUW",
                 "nameConfirmed": True, "result": result, "resultExact": True},
                {"puuid": "ally", "team": "Blue", "name": "Player-ALLY",
                 "nameConfirmed": False, "result": result, "resultExact": True},
                {"puuid": "enemy", "team": "Red", "result": "Defeat",
                 "resultExact": True},
            ],
        }

    def test_restart_and_match_ingestion_are_durable_and_idempotent(self):
        knowledge_store.save_match(self.match(), owner="owner", provenance="test")
        knowledge_store.save_match(self.match(), owner="owner", provenance="test")
        knowledge_store.init(self.path, migrate=False)
        self.assertTrue(knowledge_store.get_match("match-1", "owner")["complete"])
        self.assertEqual(knowledge_store.encounter("owner", "ally")["withCount"], 1)
        self.assertEqual(knowledge_store.get_player("owner")["riotId"], "Owner#EUW")
        self.assertIsNone(knowledge_store.get_player("ally")["riotId"])

    def test_saved_status_survives_name_change(self):
        knowledge_store.set_saved("saved-player", True, "watch")
        knowledge_store.upsert_player(
            "saved-player", riot_id="First#EU", confirmed=True, source="name_service"
        )
        knowledge_store.upsert_player(
            "saved-player", riot_id="Second#EU", confirmed=True, source="match_detail"
        )
        player = knowledge_store.get_player("saved-player")
        self.assertTrue(player["saved"])
        self.assertEqual(player["riotId"], "Second#EU")
        self.assertEqual(len(player["nameHistory"]), 2)

    def test_name_retry_schedule_and_deferred_reconsideration(self):
        knowledge_store.schedule_name("hidden-player", "match-x", now=100)
        self.assertEqual(knowledge_store.due_name_jobs(now=100)[0]["attempts"], 0)
        knowledge_store.fail_name_job("hidden-player", now=100)
        self.assertEqual(knowledge_store.due_name_jobs(now=129), [])
        knowledge_store.fail_name_job("hidden-player", now=520)
        self.assertEqual(knowledge_store.due_name_jobs(now=1300)[0]["next_attempt_at"], 1300)
        knowledge_store.fail_name_job("hidden-player", now=1300)
        self.assertEqual(knowledge_store.due_name_jobs(now=9999), [])
        self.assertEqual(knowledge_store.reconsider_deferred_names(now=10000), 1)

    def test_legacy_migration_preserves_totals_and_is_idempotent(self):
        directory = Path(self.temp.name)
        legacy = {"accounts": {"owner": {"players": {"ally": {
            "name": "Ally#EU", "withCount": 3, "winsWith": 2,
            "unresolvedWith": 1, "lastSeen": 99,
            "resultByMatch": {"known": {"side": "with", "result": "Victory"}},
        }}}}}
        source = directory / "encounters.json"
        source.write_text(json.dumps(legacy), encoding="utf-8")
        knowledge_store.migrate_legacy(directory)
        knowledge_store.migrate_legacy(directory)
        row = knowledge_store.encounter("owner", "ally")
        self.assertEqual(row["withCount"], 3)
        self.assertEqual(row["winsWith"], 2)
        self.assertTrue(source.exists())

    def test_schema_and_inventory_reject_credential_fields_and_binaries(self):
        knowledge_store.save_inventory("owner", {
            "available": True, "Authorization": "Bearer secret-token",
            "apiKey": "secret-key", "top": [{"name": "Skin", "icon": "data:image/png;base64,abc"}],
        })
        raw = self.path.read_bytes()
        self.assertNotIn(b"secret-token", raw)
        self.assertNotIn(b"secret-key", raw)
        self.assertNotIn(b"base64,abc", raw)
        with sqlite3.connect(self.path) as db:
            columns = {row[1].lower() for table in ("players", "matches", "inventory_snapshots")
                       for row in db.execute(f"PRAGMA table_info({table})")}
        self.assertFalse(columns & {"password", "token", "authorization", "api_key"})

    def test_party_inference_ignores_opponents(self):
        knowledge_store.save_match(self.match("shared-1"), provenance="test")
        players = [{"puuid": "owner", "team": "Blue"},
                   {"puuid": "ally", "team": "Blue"},
                   {"puuid": "enemy", "team": "Blue"}]
        groups = knowledge_store.party_groups(players)
        self.assertEqual(groups[0]["confidence"], "weak")
        self.assertEqual(groups[0]["members"], ["ally", "owner"])


class RuntimeAndApiTests(unittest.TestCase):
    match = staticmethod(StoreTest.match)

    def setUp(self):
        self.previous = knowledge_store._PATH
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "valorant_tracker.sqlite3"
        knowledge_store.init(self.path, migrate=False)

    def tearDown(self):
        knowledge_store.init(self.previous, migrate=False)
        self.temp.cleanup()

    def test_reconciliation_stops_after_fully_known_page(self):
        for index in range(20):
            knowledge_store.save_match(self.match(f"known-{index}"), provenance="test")

        class Auth:
            def pd_get(self, _path):
                return {"History": [{"MatchID": f"known-{i}"} for i in range(20)]}

        class Tracker:
            self_puuid = "owner"
            auth = Auth()

            def ingest_match(self, *_args, **_kwargs):
                raise AssertionError("known match fetched")

        self.assertEqual(tracker_runtime.reconcile(Tracker()), 0)

    def test_offline_durable_routes_and_debug_gate(self):
        knowledge_store.save_match(self.match(), owner="owner", provenance="test")
        knowledge_store.upsert_player("player-owner", source="test")
        knowledge_store.save_inventory("owner", {"available": True, "counts": {"skins": 1}})
        client = app.app.test_client()
        with mock.patch.object(app.LocalAuth, "available", return_value=False):
            self.assertEqual(client.get("/api/profile/owner").status_code, 400)
            self.assertEqual(client.get("/api/profile/player-owner").status_code, 200)
            self.assertEqual(client.get("/api/match/match-1?subject=owner").get_json()["source"], "stored")
            self.assertEqual(client.get("/api/inventory").get_json()["source"], "stored")
        with mock.patch.dict("os.environ", {"FLASK_DEBUG": "false"}):
            self.assertEqual(client.get("/api/debug/reveal").status_code, 404)
        self.assertEqual(client.get("/api/region").status_code, 404)
        self.assertEqual(client.get("/api/health").get_json()["region"], "eu")

    def test_fixed_eu_routing_rejects_non_eu(self):
        with (mock.patch.object(riot_client.LocalAuth, "_get_lockfile", return_value={}),
              mock.patch.object(riot_client.LocalAuth, "_get_region", return_value=["na", ["na-1", "na"]])):
            with self.assertRaises(riot_client.ClientNotReady):
                riot_client.LocalAuth()
        with (mock.patch.object(riot_client.LocalAuth, "_get_lockfile", return_value={}),
              mock.patch.object(riot_client.LocalAuth, "_get_region", return_value=["eu", ["eu-2", "eu"]])):
            auth = riot_client.LocalAuth()
            self.assertEqual(auth.pd_url, "https://pd.eu.a.pvp.net")

    def test_form_streak_breaks_on_draw_and_unresolved(self):
        self.assertIsNone(live_match.form_streak(["D", "W", "W"]))
        self.assertIsNone(live_match.form_streak(["U", "L"]))
        self.assertEqual(live_match.form_streak(["W", "W", "D", "W"])["count"], 2)

    def test_win_rate_fields_keep_matching_sample_counts(self):
        player = live_match.assemble_player(
            puuid="player", name="Player#EU", name_hidden=False, team="Blue",
            is_self=False, agent_id="", rank_tier=10, rr=20, leaderboard=0,
            peak_tier=12, prev_tier=9, win_rate=70, games=80, kd=1.2, hs=25,
            level=100, level_hidden=False, party=None,
            intel={"winRate": 60, "decidedGames": 10},
            season_win_rate=70, season_games=80, season_wins=56,
        )
        self.assertEqual((player["winRate"], player["winRateGames"]), (60, 10))
        self.assertEqual((player["seasonWinRate"], player["seasonGames"]), (70, 80))

    def test_presence_monitor_only_builds_on_transition_or_staleness(self):
        states = iter(("MENUS", "MENUS", "PREGAME", "PREGAME"))
        calls = []

        class Tracker:
            def __init__(self, _auth):
                self.self_puuid = "owner"

            def _presences(self):
                return []

            def game_state(self, _presences):
                return next(states)

            def build_scoreboard(self, include_stats):
                calls.append(include_stats)
                return {"source": "local", "state": "PREGAME", "players": []}

        tracker_runtime._LAST_STATE = None
        tracker_runtime._LAST_REFRESH = 0
        with (mock.patch.object(tracker_runtime, "LiveMatch", Tracker),
              mock.patch.object(tracker_runtime.session_tracker, "observe"),
              mock.patch.object(tracker_runtime.session_tracker, "attach")):
            tracker_runtime.poll_once(lambda: object(), now=1)
            tracker_runtime.poll_once(lambda: object(), now=2)
            tracker_runtime.poll_once(lambda: object(), now=3)
            tracker_runtime.poll_once(lambda: object(), now=4)
        self.assertEqual(calls, [False, True])


if __name__ == "__main__":
    unittest.main()
