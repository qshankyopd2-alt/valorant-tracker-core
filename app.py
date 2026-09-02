from __future__ import annotations

import json
import os
import threading
import time

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import encounter_log
import history
import inventory
import knowledge_store
import live_match
import match_meta
import tracker_log
import session_tracker
import tracker_runtime
from agents import AGENTS
from riot_client import ClientNotReady, LocalAuth, read_party_state
from vconstants import APP_VERSION, STATES, rank_from_tier


app = Flask(__name__)
CORS(app)

for _handler in tracker_log.get_logger("backend").handlers:
    app.logger.addHandler(_handler)

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = float(os.getenv("PLAYER_CACHE_TTL", "60"))
_ENCOUNTER_BACKFILL_AT: dict[str, float] = {}
_ENCOUNTER_BACKFILL_LOCK = threading.Lock()

_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "data", "settings.json")
_SETTINGS_LOCK = threading.Lock()
_SETTINGS_KEYS = {"autoRefresh"}

_LAST_GOOD: dict[str, object] = {"board": None, "at": 0.0, "notReady": False}
_HOLD_SECS = 12.0
_BUILD_FRESH = 3.5
_BUILD_LOCK = threading.Lock()


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return (
            {key: value for key, value in data.items() if key in _SETTINGS_KEYS}
            if isinstance(data, dict)
            else {}
        )
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
    temporary = f"{_SETTINGS_PATH}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temporary, _SETTINGS_PATH)


def _live_enabled() -> bool:
    return LocalAuth.available()


def _client_notice() -> dict:
    if not LocalAuth.available():
        return {
            "level": "info",
            "action": "open_game",
            "message": "Open VALORANT to see live tracker data.",
        }
    return {
        "level": "warn",
        "action": "retry",
        "message": "VALORANT is running, but its local data is not ready yet.",
    }


def _offline_payload(error: object | None = None) -> dict:
    payload = {
        "available": False,
        "state": "OFFLINE",
        "stateLabel": STATES.get("OFFLINE", "Offline"),
        "source": "local",
        "players": [],
        "teams": {},
        "parties": [],
        "notice": _client_notice(),
        "appVersion": APP_VERSION,
    }
    if error:
        payload["error"] = str(error)
    return payload


def _attach_encounters(board: dict) -> dict:
    if board.get("source") != "local":
        return board
    owner = board.get("selfPuuid")
    for player in board.get("players") or []:
        if not isinstance(player, dict):
            continue
        encounter = encounter_log.encounter_for(owner, player.get("puuid"))
        player["encounter"] = encounter
    return board


def build_live() -> dict:
    if not _live_enabled():
        return _offline_payload()

    with _BUILD_LOCK:
        now = time.time()
        cached = _LAST_GOOD.get("board")
        cached_at = float(_LAST_GOOD.get("at") or 0)
        if isinstance(cached, dict) and now - cached_at < _BUILD_FRESH:
            return cached
        try:
            tracker = live_match.LiveMatch(LocalAuth())
            board = tracker.build_scoreboard(
                include_stats=os.getenv("LIVE_INCLUDE_STATS", "true").lower() != "false"
            )
            board.setdefault("source", "local")
            board.setdefault("sourceDetail", "Local VALORANT client")
            board["selfPuuid"] = tracker.self_puuid
            board["available"] = True

            try:
                session_tracker.observe(board, tracker)
                session_tracker.attach(board)
            except Exception:
                app.logger.exception("session tracking failed")

            try:
                encounter_log.record_board(board)
                _attach_encounters(board)
            except Exception:
                app.logger.exception("encounter logging failed")

            board["appVersion"] = APP_VERSION
            _LAST_GOOD.update(board=board, at=now, notReady=False)
            return board
        except Exception as exc:
            if isinstance(exc, ClientNotReady):
                if not _LAST_GOOD.get("notReady"):
                    app.logger.info("live tracker waiting for sign-in: %s", exc)
                    _LAST_GOOD["notReady"] = True
            else:
                app.logger.exception("live tracker failed")

            if isinstance(cached, dict) and now - cached_at < _HOLD_SECS:
                return cached
            return _offline_payload(exc)


def _current_puuid() -> str | None:
    if _live_enabled():
        try:
            auth = LocalAuth()
            auth.headers()
            knowledge_store.set_last_local_puuid(auth.puuid)
            return auth.puuid
        except Exception:
            pass
    return knowledge_store.last_local_puuid()


def _refresh_encounter_history(owner: str | None) -> None:
    if not owner or not _live_enabled():
        return
    now = time.time()
    with _ENCOUNTER_BACKFILL_LOCK:
        if now - _ENCOUNTER_BACKFILL_AT.get(owner, 0) < 600:
            return
        _ENCOUNTER_BACKFILL_AT[owner] = now
    try:
        tracker = live_match.LiveMatch(LocalAuth())
        career = tracker.player_career(owner, count=10)
        encounter_log.backfill_career(owner, career.get("matches") or [])

        season = tracker.season_id()
        previous_season = tracker.prev_season_id()
        for teammate in (career.get("coPlayers") or [])[:6]:
            if int(teammate.get("sharedMatches") or 0) < 2:
                continue
            puuid = teammate.get("puuid")
            if not puuid:
                continue
            rank = tracker.rank_info(puuid, season, previous_season)
            tier = int(rank.get("tier") or 0)
            if tier <= 0:
                continue
            current = rank_from_tier(tier)
            peak = rank_from_tier(rank.get("peak") or tier)
            encounter_log.enrich_player(
                owner,
                puuid,
                {
                    "name": teammate.get("name"),
                    "rank": current["name"],
                    "peakRank": peak["name"],
                    "rankTier": current["tier"],
                    "peakTier": peak["tier"],
                    "rankColor": current["color"],
                    "winRate": rank.get("winRateAllGames"),
                },
            )
    except Exception:
        with _ENCOUNTER_BACKFILL_LOCK:
            _ENCOUNTER_BACKFILL_AT.pop(owner, None)
        app.logger.exception("encounter history backfill failed")


def _insights_payload(timezone_name: str | None = None) -> dict:
    owner = _current_puuid()
    if owner:
        try:
            auth = LocalAuth()
            auth.headers()
            threading.Thread(
                target=history.refresh,
                args=(auth, timezone_name),
                daemon=True,
                name=f"rr-refresh-{owner[:8]}",
            ).start()
        except Exception:
            app.logger.exception("RR history refresh failed")
    return history.payload(owner, timezone_name)


def _performance_payload(timezone_name: str | None = None, rich_limit: int = 20) -> dict:
    payload = _insights_payload(timezone_name)
    owner = (payload.get("account") or {}).get("puuid")
    if owner and _live_enabled():
        def enrich_recent() -> None:
            try:
                history.enrich(live_match.LiveMatch(LocalAuth()), owner, rich_limit)
            except Exception:
                app.logger.exception("performance enrichment failed")

        threading.Thread(
            target=enrich_recent,
            daemon=True,
            name=f"perf-enrich-{owner[:8]}",
        ).start()
    if owner:
        session_tracker.ensure_active(owner, payload.get("summary", {}))
    payload["sessions"] = session_tracker.list_for(owner)
    payload["matchMeta"] = match_meta.get_all(owner)
    payload["encounters"] = encounter_log.get_all(owner)
    return payload


def _inventory_payload() -> dict:
    if not _live_enabled():
        stored = knowledge_store.stored_inventory(_current_puuid())
        return stored or {
            "available": False,
            "retryable": True,
            "state": "OFFLINE",
            "error": "VALORANT is not running.",
        }
    auth = LocalAuth()
    owner = None
    try:
        data = inventory.snapshot(auth)
        knowledge_store.save_inventory(auth.puuid, data)
        return data
    except ClientNotReady:
        owner = getattr(auth, "puuid", None)
        return inventory.last_good(owner) or knowledge_store.stored_inventory(owner) or {
            "available": False,
            "retryable": True,
            "error": "The collection is still loading from Riot.",
        }
    except Exception:
        app.logger.exception("inventory snapshot failed")
        owner = getattr(auth, "puuid", None)
        return inventory.last_good(owner) or knowledge_store.stored_inventory(owner) or {
            "available": False,
            "retryable": True,
            "error": "The collection could not be read from the Riot client.",
        }


def _current_weapons(puuid: str) -> list:
    try:
        board = build_live()
        for player in board.get("players") or []:
            if player.get("puuid") == puuid:
                return player.get("weapons") or []
    except Exception:
        pass
    return []


@app.get("/api/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "valorant-tracker-core",
            "appVersion": APP_VERSION,
            # A lockfile proves the Riot client was detected, not that the
            # entitlements endpoint is ready; /api/state reports data readiness.
            "clientStatus": "detected" if _live_enabled() else "not_running",
            "officialKey": bool(os.getenv("RIOT_API_KEY")),
            "mode": "local-read-only",
            "region": "eu",
        }
    )


@app.get("/api/agents")
def agents():
    return jsonify({"agents": AGENTS, "count": len(AGENTS)})


@app.get("/api/settings")
def settings_get():
    with _SETTINGS_LOCK:
        return jsonify(_load_settings())


@app.post("/api/settings")
def settings_post():
    body = request.get_json(silent=True) or {}
    incoming = {key: value for key, value in body.items() if key in _SETTINGS_KEYS}
    with _SETTINGS_LOCK:
        merged = _load_settings()
        merged.update(incoming)
        try:
            _save_settings(merged)
        except Exception as exc:
            app.logger.exception("settings save failed")
            return jsonify({"ok": False, "message": str(exc), "settings": merged}), 500
    return jsonify({"ok": True, "settings": merged})


@app.get("/api/state")
def state():
    if not _live_enabled():
        return jsonify(
            {
                "available": False,
                "state": "OFFLINE",
                "stateLabel": STATES.get("OFFLINE", "Offline"),
                "source": "local",
            }
        )
    try:
        tracker = live_match.LiveMatch(LocalAuth())
        current = tracker.game_state(tracker._presences())
        return jsonify(
            {
                "available": True,
                "state": current,
                "stateLabel": STATES.get(current, current),
                "source": "local",
            }
        )
    except Exception as exc:
        return jsonify(_offline_payload(exc))


@app.get("/api/live")
def live():
    return jsonify(build_live())


@app.get("/api/encounters")
def encounters():
    owner = _current_puuid()
    _refresh_encounter_history(owner)
    scope = "all" if request.args.get("scope") == "all" else "current"
    players = encounter_log.get_all_accounts() if scope == "all" else encounter_log.get_all(owner)
    return jsonify(
        {
            "players": players,
            "accountCount": encounter_log.account_count(),
            "scope": scope,
            "activeAccount": owner,
        }
    )


@app.get("/api/encounters/<puuid>")
def encounter(puuid: str):
    return jsonify(encounter_log.get_one(_current_puuid(), puuid.strip()))


@app.get("/api/recap")
def recap():
    current = session_tracker.current_recap()
    if current:
        return jsonify({"available": True, "recap": current})
    return jsonify(
        {
            "available": False,
            "recap": None,
            "state": "OFFLINE" if not _live_enabled() else "MENUS",
        }
    )


@app.get("/api/sessions")
def sessions_get():
    return jsonify(session_tracker.list_for(_current_puuid()))


@app.post("/api/session/start")
def session_start():
    owner = _current_puuid()
    baseline = history.payload(owner).get("summary", {}) if owner else None
    goal = (request.get_json(silent=True) or {}).get("goal")
    return jsonify(session_tracker.start(owner, goal, baseline))


@app.post("/api/session/end")
def session_end():
    return jsonify(session_tracker.end(_current_puuid()))


@app.delete("/api/sessions/<session_id>")
def session_delete(session_id: str):
    return jsonify(session_tracker.delete(_current_puuid(), session_id.strip()))


@app.post("/api/session/reset")
def session_reset():
    body = request.get_json(silent=True) or {}
    return jsonify(session_tracker.reset(_current_puuid(), body.get("goal")))


@app.get("/api/insights")
def insights():
    return jsonify(_insights_payload(request.args.get("tz")))


@app.get("/api/performance")
def performance():
    try:
        rich_limit = max(1, min(50, int(request.args.get("richLimit", 20))))
    except (TypeError, ValueError):
        rich_limit = 20
    return jsonify(_performance_payload(request.args.get("tz"), rich_limit))


@app.get("/api/inventory")
def inventory_route():
    return jsonify(_inventory_payload())


@app.get("/api/matches/<match_id>/meta")
def match_meta_get(match_id: str):
    return jsonify(match_meta.get_one(_current_puuid(), match_id.strip()))


@app.put("/api/matches/<match_id>/meta")
def match_meta_update(match_id: str):
    body = request.get_json(silent=True) or {}
    return jsonify(match_meta.update(_current_puuid(), match_id.strip(), body))


@app.get("/api/match/<match_id>")
def match(match_id: str):
    match_id = match_id.strip()
    if not _live_enabled():
        stored = knowledge_store.get_match(match_id, request.args.get("subject"))
        return (jsonify(stored), 200) if stored else (
            jsonify({"available": False, "error": "Match not found."}), 404
        )
    try:
        data = live_match.LiveMatch(LocalAuth()).match_detail(
            match_id, request.args.get("subject")
        )
        if data.get("error"):
            return jsonify(data), 404
        return jsonify(data)
    except Exception as exc:
        app.logger.exception("match detail failed")
        stored = knowledge_store.get_match(match_id, request.args.get("subject"))
        return (jsonify(stored), 200) if stored else (
            jsonify({"available": False, "error": str(exc)}), 502
        )


@app.get("/api/profile/<puuid>")
def profile(puuid: str):
    puuid = puuid.strip()
    if len(puuid) < 6:
        return jsonify({"error": "A valid PUUID is required."}), 400
    try:
        count = max(1, min(20, int(request.args.get("count", 8))))
    except (TypeError, ValueError):
        count = 8
    if not _live_enabled():
        stored = knowledge_store.stored_profile(puuid, count)
        return (jsonify(stored), 200) if stored else (
            jsonify({"available": False, "error": "Player not found."}), 404
        )

    now = time.time()
    key = f"profile:{puuid}:{count}"
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return jsonify(cached[1])
    try:
        tracker = live_match.LiveMatch(LocalAuth())
        data = tracker.player_profile(puuid, count=count)
        data.update(source="local", live=True, available=True)
        data["weapons"] = _current_weapons(puuid)
        data["encounter"] = encounter_log.get_one(_current_puuid(), puuid)
    except Exception as exc:
        app.logger.exception("profile failed")
        stored = knowledge_store.stored_profile(puuid, count)
        return (jsonify(stored), 200) if stored else (
            jsonify({"available": False, "error": str(exc)}), 502
        )
    _CACHE[key] = (now, data)
    return jsonify(data)


@app.get("/api/debug/reveal")
def debug_reveal():
    if os.getenv("FLASK_DEBUG", "false").lower() != "true":
        return jsonify({"error": "Not found."}), 404
    if not _live_enabled():
        return jsonify({"error": "VALORANT is not running."}), 400
    try:
        return jsonify(live_match.LiveMatch(LocalAuth()).diagnose_reveal())
    except Exception as exc:
        app.logger.exception("read-only reveal diagnostic failed")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/queue")
def queue_get():
    return jsonify(read_party_state())


@app.get("/api/players")
def players_get():
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    return jsonify({"players": knowledge_store.list_players(request.args.get("q"), limit)})


@app.get("/api/players/<puuid>")
def player_get(puuid: str):
    player = knowledge_store.get_player(puuid.strip())
    return (jsonify(player), 200) if player else (jsonify({"error": "Player not found."}), 404)


@app.get("/api/matches")
def matches_get():
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    return jsonify({"matches": knowledge_store.list_matches(request.args.get("puuid"), limit)})


@app.get("/api/saved-players")
def saved_players_get():
    return jsonify({"players": knowledge_store.saved_players()})


@app.put("/api/saved-players/<puuid>")
def saved_player_put(puuid: str):
    puuid = puuid.strip()
    if len(puuid) < 6:
        return jsonify({"error": "A valid PUUID is required."}), 400
    body = request.get_json(silent=True) or {}
    return jsonify(knowledge_store.set_saved(puuid, True, str(body.get("note") or "")))


@app.delete("/api/saved-players/<puuid>")
def saved_player_delete(puuid: str):
    puuid = puuid.strip()
    if len(puuid) < 6:
        return jsonify({"error": "A valid PUUID is required."}), 400
    return jsonify(knowledge_store.set_saved(puuid, False))


@app.get("/")
def index():
    return jsonify(
        {
            "service": "VALORANT Tracker Core API",
            "mode": "local-read-only",
            "endpoints": [
                "/api/health",
                "/api/state",
                "/api/live",
                "/api/profile/<puuid>",
                "/api/match/<match_id>",
                "/api/performance",
                "/api/encounters",
                "/api/inventory",
                "/api/players",
                "/api/matches",
                "/api/saved-players",
                "/api/queue",
            ],
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("BACKEND_PORT", os.getenv("PORT", "5000")))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(
        f"[app] VALORANT Tracker Core API on http://127.0.0.1:{port} "
        f"(client={'ready' if _live_enabled() else 'offline'})",
        flush=True,
    )
    try:
        tracker_runtime.start()
        app.run(host="127.0.0.1", port=port, debug=debug)
    except OSError as exc:
        app.logger.error("TRACKER-BACKEND-001 could not bind 127.0.0.1:%s: %s", port, exc)
        raise SystemExit(1)
