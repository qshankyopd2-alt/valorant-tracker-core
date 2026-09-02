from __future__ import annotations

import threading
import time

import knowledge_store
import session_tracker
from live_match import LiveMatch
from riot_client import ClientNotReady, LocalAuth


_STOP = threading.Event()
_STARTED = False
_LOCK = threading.Lock()
_LAST_STATE = None
_LAST_REFRESH = 0.0


def reconcile(live_match: LiveMatch, maximum: int = 100) -> int:
    imported = 0
    owner = live_match.self_puuid
    knowledge_store.set_last_local_puuid(owner)
    for start in range(0, min(100, max(0, int(maximum))), 20):
        history = live_match.auth.pd_get(
            f"/match-history/v1/history/{owner}?startIndex={start}&endIndex={start + 20}"
        )
        entries = (history or {}).get("History") or [] if isinstance(history, dict) else []
        match_ids = [row.get("MatchID") for row in entries if row.get("MatchID")]
        if not match_ids or all(knowledge_store.is_match_complete(mid) for mid in match_ids):
            break
        for match_id in match_ids:
            if not knowledge_store.is_match_complete(match_id):
                imported += bool(live_match.ingest_match(
                    match_id, owner, local_owner=True,
                    provenance="startup_reconciliation",
                ))
        if len(entries) < 20:
            break
    return int(imported)


def resolve_due_names(live_match: LiveMatch, now=None) -> int:
    jobs = knowledge_store.due_name_jobs(now=now)
    if not jobs:
        return 0
    puuids = [job["puuid"] for job in jobs]
    names = live_match.reveal_names(puuids)
    resolved = 0
    for job in jobs:
        puuid = job["puuid"]
        name = names.get(puuid) or live_match.reveal_via_account_api(puuid)
        if name:
            knowledge_store.upsert_player(
                puuid, riot_id=name, confirmed=True, source="name_job", seen_at=now
            )
            resolved += 1
            continue
        match_id = job.get("match_id")
        if match_id:
            live_match.ingest_match(match_id, puuid, provenance="name_job_evidence")
            if (knowledge_store.get_player(puuid) or {}).get("riotId"):
                resolved += 1
                continue
        knowledge_store.fail_name_job(puuid, now=now)
    return resolved


def poll_once(auth_factory=LocalAuth, *, now=None) -> dict | None:
    global _LAST_STATE, _LAST_REFRESH
    clock = time.time() if now is None else float(now)
    try:
        live_match = LiveMatch(auth_factory())
        presences = live_match._presences()
        state = live_match.game_state(presences)
        stale = state in ("PREGAME", "INGAME") and clock - _LAST_REFRESH >= 30
        if state != _LAST_STATE or stale:
            board = live_match.build_scoreboard(include_stats=state in ("PREGAME", "INGAME"))
            knowledge_store.remember_board(board)
            session_tracker.observe(board, live_match)
            session_tracker.attach(board)
            _LAST_REFRESH = clock
        else:
            board = None
        _LAST_STATE = state
        return board
    except ClientNotReady:
        return None
    except Exception:
        return None


def _presence_loop() -> None:
    while not _STOP.wait(2):
        poll_once()


def _job_loop() -> None:
    first = True
    while not _STOP.is_set():
        try:
            live_match = LiveMatch(LocalAuth())
            if first:
                knowledge_store.reconsider_deferred_names()
                reconcile(live_match)
                first = False
            resolve_due_names(live_match)
        except Exception:
            pass
        _STOP.wait(5)


def start() -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
        _STOP.clear()
        threading.Thread(target=_presence_loop, daemon=True, name="tracker-presence").start()
        threading.Thread(target=_job_loop, daemon=True, name="tracker-jobs").start()
