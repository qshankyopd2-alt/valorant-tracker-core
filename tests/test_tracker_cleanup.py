from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
import encounter_log
import history
import knowledge_store
import live_match
import match_meta
import tracker_log
import session_tracker


ROOT = Path(__file__).resolve().parents[1]


class OutcomeTests(unittest.TestCase):
    def test_score_aware_match_outcome_preserves_draw_and_unknown(self):
        self.assertEqual(
            live_match.match_outcome(
                {
                    "Blue": {"won": False, "roundsWon": 13},
                    "Red": {"won": False, "roundsWon": 11},
                },
                "Blue",
            ),
            "Victory",
        )
        self.assertEqual(
            live_match.match_outcome(
                {
                    "Blue": {"won": False, "roundsWon": 12},
                    "Red": {"won": False, "roundsWon": 12},
                },
                "Blue",
            ),
            "Draw",
        )
        self.assertIsNone(
            live_match.match_outcome(
                {"Blue": {"won": False}, "Red": {"won": False}}, "Blue"
            )
        )

    def test_summaries_exclude_draw_and_unknown_from_win_rate_denominator(self):
        points = [
            {"result": "Victory", "ts": 1, "map": "Ascent"},
            {"result": "Defeat", "ts": 2, "map": "Ascent"},
            {"result": "Draw", "ts": 3, "map": "Ascent"},
            {"result": None, "ts": 4, "map": "Ascent"},
        ]
        for summary in (session_tracker._summary(points), history._summary(points)):
            self.assertEqual(summary["totalMatches"], 4)
            self.assertEqual(summary["decisiveMatches"], 2)
            self.assertEqual(summary["wins"], 1)
            self.assertEqual(summary["losses"], 1)
            self.assertEqual(summary["draws"], 1)
            self.assertEqual(summary["unresolved"], 1)
            self.assertEqual(summary["winRate"], 50)

        rows = history._split_rows(points[2:], "map")
        self.assertEqual(rows[0]["draws"], 1)
        self.assertEqual(rows[0]["unresolved"], 1)
        self.assertIsNone(rows[0]["winRate"])

    def test_career_summary_tracks_all_four_result_states(self):
        matches = []
        for result in ("Victory", "Defeat", "Draw", None):
            matches.append(
                {
                    "result": result,
                    "kills": 10,
                    "deaths": 5,
                    "assists": 2,
                    "hsPct": 25,
                    "teammates": [],
                    "agent": "Sage",
                    "map": "Ascent",
                }
            )
        summary = live_match._career_summary(matches)["averages"]
        self.assertEqual(summary["games"], 4)
        self.assertEqual(summary["decidedGames"], 2)
        self.assertEqual(summary["draws"], 1)
        self.assertEqual(summary["unresolved"], 1)
        self.assertEqual(summary["winRate"], 50)

    def test_history_enrichment_revisits_complete_but_unresolved_rows(self):
        old_store, old_save = history._STORE, history._save
        old_enrich_at = history._enrich_at

        class FakeLive:
            self_puuid = "owner"

            @staticmethod
            def ingest_match(match_id, puuid, **_kwargs):
                knowledge_store.save_match({
                    "matchId": match_id,
                    "startedAt": 1,
                    "map": "Ascent",
                    "mode": "Competitive",
                    "scores": {"Blue": 12, "Red": 12},
                    "players": [{"puuid": puuid, "team": "Blue", "result": "Draw",
                                 "resultExact": True, "agent": "Sage", "kills": 10,
                                 "deaths": 10, "assists": 5, "kd": 1, "acs": 200,
                                 "hsPct": 25}],
                }, provenance="test")

        old_path = knowledge_store._PATH
        with tempfile.TemporaryDirectory() as temp:
            knowledge_store.init(Path(temp) / "tracker.sqlite3", migrate=False)
            history._STORE = {
                "version": 3,
                "accounts": {
                    "owner": {
                        "points": [{
                            "matchId": "unresolved-1",
                            "ts": 1,
                            "result": None,
                            "resultExact": False,
                            "acs": 200,
                            "scores": {"Blue": 12, "Red": 12},
                            "partySize": 1,
                        }]
                    }
                },
                "discardedOrphanPoints": 0,
            }
            history._save = lambda: None
            history._enrich_at = {}
            history.enrich(FakeLive(), "owner", limit=1)
            point = history._STORE["accounts"]["owner"]["points"][0]
            self.assertEqual(point["result"], "Draw")
            self.assertTrue(point["resultExact"])
            history._STORE = old_store
            history._save = old_save
            history._enrich_at = old_enrich_at
        knowledge_store.init(old_path, migrate=False)


class EncounterTests(unittest.TestCase):
    def setUp(self):
        self.old_path = knowledge_store._PATH
        self.temp = tempfile.TemporaryDirectory()
        knowledge_store.init(Path(self.temp.name) / "tracker.sqlite3", migrate=False)

    def tearDown(self):
        knowledge_store.init(self.old_path, migrate=False)
        self.temp.cleanup()

    @staticmethod
    def board(match_id: str, state: str = "INGAME") -> dict:
        return {
            "source": "local",
            "state": state,
            "selfPuuid": "owner",
            "selfTeam": "Blue",
            "matchId": match_id,
            "map": "Ascent",
            "players": [
                {"puuid": "owner", "team": "Blue", "isSelf": True},
                {"puuid": "ally", "team": "Blue", "agent": "Sage"},
                {"puuid": "enemy", "team": "Red", "agent": "Jett"},
            ],
        }

    def test_draw_is_separate_and_idempotent(self):
        board = self.board("draw-match")
        encounter_log.record_board(board)
        encounter_log.record_result(board, "Draw")
        encounter_log.record_result(board, "Draw")
        ally = encounter_log.get_one("owner", "ally")
        enemy = encounter_log.get_one("owner", "enemy")
        self.assertEqual(ally["drawsWith"], 1)
        self.assertEqual(ally["lossesWith"], 0)
        self.assertEqual(enemy["drawsAgainst"], 1)
        self.assertEqual(enemy["lossesAgainst"], 0)
        self.assertEqual(ally["timeline"][0]["result"], "draw")

    def test_unresolved_can_be_upgraded_without_becoming_a_loss(self):
        board = self.board("pending-match")
        encounter_log.record_board(board)
        encounter_log.record_result(board, None)
        pending = encounter_log.get_one("owner", "ally")
        self.assertEqual(pending["unresolvedWith"], 1)
        self.assertEqual(pending["lossesWith"], 0)

        encounter_log.record_result(board, "Victory")
        resolved = encounter_log.get_one("owner", "ally")
        self.assertEqual(resolved["unresolvedWith"], 0)
        self.assertEqual(resolved["winsWith"], 1)
        self.assertEqual(resolved["lossesWith"], 0)

    def test_lobby_is_not_recorded_as_an_encounter(self):
        encounter_log.record_board(self.board("lobby", state="MENUS"))
        self.assertEqual(encounter_log.get_all("owner"), [])


class TeamVisibilityTests(unittest.TestCase):
    class FakeAuth:
        puuid = "self"

        def headers(self):
            return {}

        def glz_get(self, path):
            responses = {
                "/pregame/v1/players/self": {"MatchID": "pregame-1"},
                "/pregame/v1/matches/pregame-1": {
                    "AllyTeam": {
                        "TeamID": "Blue",
                        "Players": [{"Subject": "self"}, {"Subject": "ally"}],
                    },
                    "EnemyTeam": {
                        "TeamID": "Red",
                        "Players": [{"Subject": "enemy"}],
                    },
                    "MapID": "map",
                    "QueueID": "competitive",
                },
                "/core-game/v1/players/self": {"MatchID": "game-1"},
                "/core-game/v1/matches/game-1": {
                    "Players": [
                        {"Subject": "self", "TeamID": "Blue"},
                        {"Subject": "ally", "TeamID": "Blue"},
                        {"Subject": "enemy", "TeamID": "Red"},
                    ],
                    "MapID": "map",
                    "MatchmakingData": {"QueueID": "competitive"},
                },
            }
            return responses[path]

    def test_pregame_is_ally_only_and_ingame_has_both_teams(self):
        tracker = live_match.LiveMatch(self.FakeAuth())
        pregame, _, _, _ = tracker._current_players("PREGAME")
        self.assertEqual({player["Subject"] for player in pregame}, {"self", "ally"})
        self.assertTrue(all(player["TeamID"] == "Blue" for player in pregame))

        ingame, _, _, _ = tracker._current_players("INGAME")
        self.assertEqual({player["TeamID"] for player in ingame}, {"Blue", "Red"})


class ApiTests(unittest.TestCase):
    def setUp(self):
        app._CACHE.clear()
        self.client = app.app.test_client()

    def test_offline_live_response_is_real_not_generated(self):
        with mock.patch.object(app.LocalAuth, "available", return_value=False):
            response = self.client.get("/api/live")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["state"], "OFFLINE")
        self.assertEqual(payload["source"], "local")
        self.assertFalse(payload["available"])
        self.assertEqual(payload["players"], [])

    def test_route_surface_is_tracker_only(self):
        rules: dict[str, set[str]] = {}
        for rule in app.app.url_map.iter_rules():
            rules.setdefault(rule.rule, set()).update(rule.methods)
        for removed in (
            "/api/player/<puuid>",
            "/api/remote-mode",
            "/api/dodge",
            "/api/launch-offline",
            "/api/offline-toggle",
            "/api/instalock",
        ):
            self.assertNotIn(removed, rules)
        self.assertIn("/api/profile/<puuid>", rules)
        self.assertIn("GET", rules["/api/matches/<match_id>/meta"])
        self.assertIn("PUT", rules["/api/matches/<match_id>/meta"])
        self.assertEqual(rules["/api/queue"] & {"GET", "POST", "PUT", "DELETE"}, {"GET"})

    def test_profile_and_match_routes_keep_real_tracker_payloads(self):
        class FakeAuth:
            puuid = "owner"

            @staticmethod
            def available():
                return True

            def headers(self):
                return {}

        class FakeTracker:
            def __init__(self, auth):
                self.auth = auth

            def player_profile(self, puuid, count=8):
                return {
                    "puuid": puuid,
                    "riotId": "Player#EUW",
                    "currentRank": "Gold 2",
                    "rr": 42,
                    "peakRank": "Platinum 1",
                    "previousRank": "Gold 1",
                    "winRate": 50,
                    "matches": [],
                }

            def match_detail(self, match_id, subject):
                return {"matchId": match_id, "subject": subject, "result": "Draw"}

        with (
            mock.patch.object(app, "LocalAuth", FakeAuth),
            mock.patch.object(app.live_match, "LiveMatch", FakeTracker),
            mock.patch.object(app, "_current_weapons", return_value=[{"name": "Vandal"}]),
            mock.patch.object(app.encounter_log, "get_one", return_value={"withCount": 2}),
        ):
            profile = self.client.get("/api/profile/player-123").get_json()
            detail = self.client.get("/api/match/match-1?subject=player-123").get_json()

        self.assertEqual(profile["rr"], 42)
        self.assertEqual(profile["peakRank"], "Platinum 1")
        self.assertEqual(profile["previousRank"], "Gold 1")
        self.assertEqual(profile["weapons"][0]["name"], "Vandal")
        self.assertEqual(profile["encounter"]["withCount"], 2)
        self.assertEqual(detail["result"], "Draw")


class StorageAndPolicyTests(unittest.TestCase):
    def test_match_metadata_rejects_string_as_tag_sequence(self):
        old_path = knowledge_store._PATH
        with tempfile.TemporaryDirectory() as temp:
            knowledge_store.init(Path(temp) / "tracker.sqlite3", migrate=False)
            result = match_meta.update("owner", "match", {"tags": "not-a-list"})
            self.assertEqual(result["meta"]["tags"], [])
        knowledge_store.init(old_path, migrate=False)

    def test_log_redaction_keeps_general_secret_protection(self):
        redacted = tracker_log.redact(
            'Authorization=Bearer abcdefghijk token=secretvalue "password":"hunter2xx"'
        )
        self.assertNotIn("abcdefghijk", redacted)
        self.assertNotIn("secretvalue", redacted)
        self.assertNotIn("hunter2xx", redacted)

    def test_tls_bypass_is_only_used_for_loopback_calls(self):
        tree = ast.parse((ROOT / "riot_client.py").read_text(encoding="utf-8"))
        bypasses = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if any(
                keyword.arg == "verify"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
                for keyword in node.keywords
            ):
                bypasses.append(ast.unparse(node.args[0]))
        self.assertTrue(bypasses)
        self.assertTrue(all("127.0.0.1" in target for target in bypasses))

    def test_no_riot_control_http_verbs_remain(self):
        for filename in ("riot_client.py", "live_match.py"):
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "requests":
                    self.assertNotIn(node.func.attr, {"post", "patch", "delete"})
                    if node.func.attr == "put":
                        parent = next(
                            (
                                item
                                for item in ast.walk(tree)
                                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                                and node in ast.walk(item)
                            ),
                            None,
                        )
                        self.assertIsNotNone(parent)
                        self.assertEqual(parent.name, "name_service")


if __name__ == "__main__":
    unittest.main()
