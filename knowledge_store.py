from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path


_DATA_DIR = Path(__file__).resolve().parent / "data"
_PATH = _DATA_DIR / "valorant_tracker.sqlite3"
# ponytail: one process-wide writer lock; use per-account locks only if write throughput matters.
_LOCK = threading.RLock()
_OUTCOMES = {"Victory", "Defeat", "Draw", "Unresolved"}


def _now(value=None) -> int:
    return int(value or time.time())


def _connect() -> sqlite3.Connection:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def init(path: str | os.PathLike | None = None, *, migrate: bool = True) -> None:
    global _PATH
    if path is not None:
        _PATH = Path(path)
    with _LOCK, _connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS players (
                puuid TEXT PRIMARY KEY,
                riot_id TEXT,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                sources_json TEXT NOT NULL DEFAULT '[]',
                rank_tier INTEGER,
                rr INTEGER,
                peak_tier INTEGER,
                previous_tier INTEGER,
                level INTEGER,
                kd REAL,
                hs_pct REAL,
                win_rate REAL,
                stats_refreshed_at INTEGER,
                saved_at INTEGER,
                saved_note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS player_names (
                puuid TEXT NOT NULL REFERENCES players(puuid) ON DELETE CASCADE,
                riot_id TEXT NOT NULL,
                source TEXT NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                PRIMARY KEY (puuid, riot_id)
            );
            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                started_at INTEGER,
                map TEXT,
                mode TEXT,
                scores_json TEXT NOT NULL DEFAULT '{}',
                complete INTEGER NOT NULL DEFAULT 0,
                provenance TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS match_players (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                puuid TEXT NOT NULL REFERENCES players(puuid),
                team TEXT,
                riot_id TEXT,
                name_confirmed INTEGER NOT NULL DEFAULT 0,
                agent TEXT,
                party_id TEXT,
                kills INTEGER,
                deaths INTEGER,
                assists INTEGER,
                kd REAL,
                acs REAL,
                hs_pct REAL,
                shots_hit INTEGER,
                headshots INTEGER,
                rank_tier INTEGER,
                rr INTEGER,
                peak_tier INTEGER,
                level INTEGER,
                player_card TEXT,
                result TEXT NOT NULL DEFAULT 'Unresolved',
                result_exact INTEGER NOT NULL DEFAULT 0,
                is_local INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (match_id, puuid)
            );
            CREATE TABLE IF NOT EXISTS encounters (
                owner_puuid TEXT NOT NULL,
                player_puuid TEXT NOT NULL,
                match_key TEXT NOT NULL,
                match_id TEXT,
                side TEXT NOT NULL CHECK(side IN ('with', 'against')),
                result TEXT NOT NULL CHECK(result IN ('Victory','Defeat','Draw','Unresolved')),
                seen_at INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (owner_puuid, player_puuid, match_key)
            );
            CREATE TABLE IF NOT EXISTS pending_name_jobs (
                puuid TEXT PRIMARY KEY REFERENCES players(puuid) ON DELETE CASCADE,
                match_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                cycle_started_at INTEGER NOT NULL,
                next_attempt_at INTEGER,
                delayed_done INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS match_notes (
                owner_puuid TEXT NOT NULL,
                match_id TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                bookmarked INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (owner_puuid, match_id)
            );
            CREATE TABLE IF NOT EXISTS inventory_snapshots (
                puuid TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_match_players_puuid
                ON match_players(puuid, match_id);
            CREATE INDEX IF NOT EXISTS idx_matches_started
                ON matches(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_encounters_owner_player
                ON encounters(owner_puuid, player_puuid, seen_at);
            CREATE INDEX IF NOT EXISTS idx_name_jobs_due
                ON pending_name_jobs(status, next_attempt_at);
            PRAGMA user_version=1;
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(match_players)")}
        for name in ("shots_hit", "headshots"):
            if name not in columns:
                db.execute(f"ALTER TABLE match_players ADD COLUMN {name} INTEGER")
    if migrate:
        migrate_legacy(_PATH.parent)


def _json(value, fallback):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _meta_get(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(db: sqlite3.Connection, key: str, value: object) -> None:
    db.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def set_last_local_puuid(puuid: str | None) -> None:
    if not puuid:
        return
    with _LOCK, _connect() as db:
        _meta_set(db, "last_local_puuid", puuid)


def last_local_puuid() -> str | None:
    with _connect() as db:
        return _meta_get(db, "last_local_puuid")


def upsert_player(
    puuid: str | None,
    *,
    riot_id: str | None = None,
    confirmed: bool = False,
    source: str = "unknown",
    seen_at: int | None = None,
    fields: dict | None = None,
    db: sqlite3.Connection | None = None,
) -> None:
    if not puuid:
        return
    own_connection = db is None
    connection = db or _connect()
    now = _now(seen_at)
    try:
        row = connection.execute(
            "SELECT sources_json FROM players WHERE puuid=?", (puuid,)
        ).fetchone()
        sources = set(_json(row[0], [])) if row else set()
        if source:
            sources.add(source)
        values = fields or {}
        confirmed_name = riot_id.strip() if confirmed and riot_id and riot_id.strip() else None
        connection.execute(
            """
            INSERT INTO players(
                puuid,riot_id,first_seen,last_seen,sources_json,rank_tier,rr,
                peak_tier,previous_tier,level,kd,hs_pct,win_rate,stats_refreshed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(puuid) DO UPDATE SET
                riot_id=COALESCE(excluded.riot_id,players.riot_id),
                first_seen=MIN(players.first_seen,excluded.first_seen),
                last_seen=MAX(players.last_seen,excluded.last_seen),
                sources_json=excluded.sources_json,
                rank_tier=COALESCE(excluded.rank_tier,players.rank_tier),
                rr=COALESCE(excluded.rr,players.rr),
                peak_tier=COALESCE(excluded.peak_tier,players.peak_tier),
                previous_tier=COALESCE(excluded.previous_tier,players.previous_tier),
                level=COALESCE(excluded.level,players.level),
                kd=COALESCE(excluded.kd,players.kd),
                hs_pct=COALESCE(excluded.hs_pct,players.hs_pct),
                win_rate=COALESCE(excluded.win_rate,players.win_rate),
                stats_refreshed_at=CASE WHEN
                    excluded.rank_tier IS NOT NULL OR excluded.level IS NOT NULL OR
                    excluded.kd IS NOT NULL OR excluded.hs_pct IS NOT NULL OR
                    excluded.win_rate IS NOT NULL
                    THEN excluded.stats_refreshed_at ELSE players.stats_refreshed_at END
            """,
            (
                puuid,
                confirmed_name,
                now,
                now,
                json.dumps(sorted(sources), separators=(",", ":")),
                values.get("rankTier"),
                values.get("rr"),
                values.get("peakRankTier") or values.get("peakTier"),
                values.get("previousTier"),
                values.get("level"),
                values.get("kd"),
                values.get("hsPct"),
                values.get("winRate"),
                now,
            ),
        )
        if confirmed_name:
            connection.execute(
                """
                INSERT INTO player_names(puuid,riot_id,source,first_seen,last_seen)
                VALUES(?,?,?,?,?)
                ON CONFLICT(puuid,riot_id) DO UPDATE SET
                    source=excluded.source,last_seen=MAX(player_names.last_seen,excluded.last_seen)
                """,
                (puuid, confirmed_name, source, now, now),
            )
            connection.execute(
                "UPDATE pending_name_jobs SET status='complete',next_attempt_at=NULL,last_error=NULL "
                "WHERE puuid=?",
                (puuid,),
            )
        if own_connection:
            connection.commit()
    finally:
        if own_connection:
            connection.close()


def remember_board(board: dict) -> None:
    if not isinstance(board, dict) or board.get("source") != "local":
        return
    state = board.get("state")
    source = {"PREGAME": "current_pregame", "INGAME": "current_ingame"}.get(
        state, "current_lobby"
    )
    now = _now()
    with _LOCK, _connect() as db:
        owner = board.get("selfPuuid")
        if owner:
            _meta_set(db, "last_local_puuid", owner)
        for player in board.get("players") or []:
            if not isinstance(player, dict):
                continue
            upsert_player(
                player.get("puuid"),
                riot_id=player.get("name"),
                confirmed=bool(player.get("nameConfirmed")),
                source=source,
                seen_at=now,
                fields=player,
                db=db,
            )


def schedule_name(puuid: str | None, match_id: str | None = None, *, now=None,
                  db: sqlite3.Connection | None = None) -> None:
    if not puuid:
        return
    stamp = _now(now)
    own_connection = db is None
    connection = db or _connect()
    try:
        upsert_player(puuid, source="unresolved_name", seen_at=stamp, db=connection)
        known = connection.execute("SELECT riot_id FROM players WHERE puuid=?", (puuid,)).fetchone()
        if known and known[0]:
            if own_connection:
                connection.commit()
            return
        connection.execute(
            """
            INSERT INTO pending_name_jobs(
                puuid,match_id,status,attempts,cycle_started_at,next_attempt_at,delayed_done
            ) VALUES(?,?,'pending',0,?,?,0)
            ON CONFLICT(puuid) DO UPDATE SET
                match_id=COALESCE(excluded.match_id,pending_name_jobs.match_id),
                status=CASE WHEN pending_name_jobs.status='complete' THEN 'pending'
                            ELSE pending_name_jobs.status END,
                next_attempt_at=CASE WHEN pending_name_jobs.status IN ('complete','deferred')
                                     THEN excluded.next_attempt_at
                                     ELSE MIN(pending_name_jobs.next_attempt_at,excluded.next_attempt_at) END,
                cycle_started_at=CASE WHEN pending_name_jobs.status IN ('complete','deferred')
                                      THEN excluded.cycle_started_at
                                      ELSE pending_name_jobs.cycle_started_at END,
                attempts=CASE WHEN pending_name_jobs.status IN ('complete','deferred')
                              THEN 0 ELSE pending_name_jobs.attempts END,
                delayed_done=CASE WHEN pending_name_jobs.status IN ('complete','deferred')
                                  THEN 0 ELSE pending_name_jobs.delayed_done END
            """,
            (puuid, match_id, stamp, stamp),
        )
        if own_connection:
            connection.commit()
    finally:
        if own_connection:
            connection.close()


def due_name_jobs(now=None, limit: int = 20) -> list[dict]:
    stamp = _now(now)
    with _connect() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM pending_name_jobs WHERE status='pending' "
                "AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",
                (stamp, max(1, min(100, int(limit)))),
            )
        ]


def fail_name_job(puuid: str, error: object = "unresolved", *, now=None) -> None:
    stamp = _now(now)
    with _LOCK, _connect() as db:
        row = db.execute(
            "SELECT * FROM pending_name_jobs WHERE puuid=?", (puuid,)
        ).fetchone()
        if not row:
            return
        attempts = int(row["attempts"]) + 1
        age = stamp - int(row["cycle_started_at"])
        delayed = bool(row["delayed_done"])
        if age < 420:
            status, next_at, delayed_done = "pending", stamp + 30, delayed
        elif not delayed and age < 1200:
            status, next_at, delayed_done = "pending", int(row["cycle_started_at"]) + 1200, 0
        else:
            status, next_at, delayed_done = "deferred", None, 1
        db.execute(
            "UPDATE pending_name_jobs SET status=?,attempts=?,next_attempt_at=?,"
            "delayed_done=?,last_error=? WHERE puuid=?",
            (status, attempts, next_at, int(delayed_done), str(error)[:250], puuid),
        )


def reconsider_deferred_names(*, now=None) -> int:
    stamp = _now(now)
    with _LOCK, _connect() as db:
        cursor = db.execute(
            "UPDATE pending_name_jobs SET status='pending',next_attempt_at=?,"
            "cycle_started_at=?,attempts=0,delayed_done=1 "
            "WHERE status='deferred'",
            (stamp, stamp),
        )
        return cursor.rowcount


def is_match_complete(match_id: str) -> bool:
    with _connect() as db:
        row = db.execute("SELECT complete FROM matches WHERE match_id=?", (match_id,)).fetchone()
        return bool(row and row[0])


def save_match(payload: dict, *, owner: str | None = None,
               provenance: str = "match_detail") -> bool:
    match_id = str(payload.get("matchId") or "").strip()
    players = payload.get("players") or []
    if not match_id or not isinstance(players, list):
        return False
    now = _now(payload.get("startedAt") or payload.get("startMillis") and
               int(payload["startMillis"] / 1000))
    updated = _now()
    with _LOCK, _connect() as db:
        was_complete = db.execute(
            "SELECT complete FROM matches WHERE match_id=?", (match_id,)
        ).fetchone()
        complete = bool(players and payload.get("scores"))
        db.execute(
            """
            INSERT INTO matches(match_id,started_at,map,mode,scores_json,complete,provenance,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(match_id) DO UPDATE SET
                started_at=COALESCE(excluded.started_at,matches.started_at),
                map=COALESCE(excluded.map,matches.map),
                mode=COALESCE(excluded.mode,matches.mode),
                scores_json=CASE WHEN excluded.scores_json!='{}' THEN excluded.scores_json
                                 ELSE matches.scores_json END,
                complete=MAX(matches.complete,excluded.complete),
                provenance=CASE WHEN matches.complete THEN matches.provenance
                                ELSE excluded.provenance END,
                updated_at=excluded.updated_at
            """,
            (
                match_id,
                now,
                payload.get("map"),
                payload.get("mode"),
                json.dumps(payload.get("scores") or {}, separators=(",", ":")),
                int(complete),
                provenance,
                updated,
            ),
        )
        owner_team = None
        owner_result = "Unresolved"
        for player in players:
            puuid = player.get("puuid")
            if not puuid:
                continue
            result = player.get("result")
            if result not in _OUTCOMES:
                result = "Unresolved"
            confirmed = bool(player.get("nameConfirmed"))
            upsert_player(
                puuid,
                riot_id=player.get("name"),
                confirmed=confirmed,
                source=provenance,
                seen_at=now,
                fields=player,
                db=db,
            )
            db.execute(
                """
                INSERT INTO match_players(
                    match_id,puuid,team,riot_id,name_confirmed,agent,party_id,kills,deaths,
                    assists,kd,acs,hs_pct,shots_hit,headshots,rank_tier,rr,peak_tier,level,player_card,
                    result,result_exact,is_local
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(match_id,puuid) DO UPDATE SET
                    team=COALESCE(excluded.team,match_players.team),
                    riot_id=COALESCE(excluded.riot_id,match_players.riot_id),
                    name_confirmed=MAX(match_players.name_confirmed,excluded.name_confirmed),
                    agent=COALESCE(excluded.agent,match_players.agent),
                    party_id=COALESCE(excluded.party_id,match_players.party_id),
                    kills=COALESCE(excluded.kills,match_players.kills),
                    deaths=COALESCE(excluded.deaths,match_players.deaths),
                    assists=COALESCE(excluded.assists,match_players.assists),
                    kd=COALESCE(excluded.kd,match_players.kd),
                    acs=COALESCE(excluded.acs,match_players.acs),
                    hs_pct=COALESCE(excluded.hs_pct,match_players.hs_pct),
                    shots_hit=COALESCE(excluded.shots_hit,match_players.shots_hit),
                    headshots=COALESCE(excluded.headshots,match_players.headshots),
                    rank_tier=COALESCE(excluded.rank_tier,match_players.rank_tier),
                    rr=COALESCE(excluded.rr,match_players.rr),
                    peak_tier=COALESCE(excluded.peak_tier,match_players.peak_tier),
                    level=COALESCE(excluded.level,match_players.level),
                    player_card=COALESCE(excluded.player_card,match_players.player_card),
                    result=CASE WHEN excluded.result_exact THEN excluded.result
                                ELSE match_players.result END,
                    result_exact=MAX(match_players.result_exact,excluded.result_exact),
                    is_local=MAX(match_players.is_local,excluded.is_local)
                """,
                (
                    match_id, puuid, player.get("team"),
                    player.get("name") if confirmed else None, int(confirmed),
                    player.get("agent"), player.get("partyId"), player.get("kills"),
                    player.get("deaths"), player.get("assists"), player.get("kd"),
                    player.get("acs"), player.get("hsPct"), player.get("shotsHit"),
                    player.get("headshots"), player.get("rankTier"),
                    player.get("rr"), player.get("peakRankTier"), player.get("level"),
                    player.get("playerCard"), result, int(player.get("resultExact", result != "Unresolved")),
                    int(puuid == owner),
                ),
            )
            if not confirmed:
                schedule_name(puuid, match_id, now=updated, db=db)
            if puuid == owner:
                owner_team, owner_result = player.get("team"), result
        if owner:
            _meta_set(db, "last_local_puuid", owner)
            for player in players:
                puuid = player.get("puuid")
                if not puuid or puuid == owner or not player.get("team") or not owner_team:
                    continue
                side = "with" if player.get("team") == owner_team else "against"
                db.execute(
                    """
                    INSERT INTO encounters(
                        owner_puuid,player_puuid,match_key,match_id,side,result,seen_at,count
                    ) VALUES(?,?,?,?,?,?,?,1)
                    ON CONFLICT(owner_puuid,player_puuid,match_key) DO UPDATE SET
                        side=excluded.side,
                        result=CASE WHEN excluded.result!='Unresolved' THEN excluded.result
                                    ELSE encounters.result END,
                        seen_at=MAX(encounters.seen_at,excluded.seen_at)
                    """,
                    (owner, puuid, match_id, match_id, side, owner_result, now),
                )
        return not bool(was_complete and was_complete[0])


def record_encounters(board: dict, result: str | None) -> None:
    owner = board.get("selfPuuid")
    match_id = board.get("matchId")
    owner_team = board.get("selfTeam")
    if not owner or not match_id or match_id == "lobby" or not owner_team:
        return
    outcome = result if result in _OUTCOMES else "Unresolved"
    stamp = _now()
    with _LOCK, _connect() as db:
        for player in board.get("players") or []:
            puuid = player.get("puuid")
            if not puuid or puuid == owner or not player.get("team"):
                continue
            upsert_player(puuid, source="local_encounter", seen_at=stamp, fields=player, db=db)
            side = "with" if player.get("team") == owner_team else "against"
            db.execute(
                """
                INSERT INTO encounters(owner_puuid,player_puuid,match_key,match_id,side,result,seen_at,count)
                VALUES(?,?,?,?,?,?,?,1)
                ON CONFLICT(owner_puuid,player_puuid,match_key) DO UPDATE SET
                    side=excluded.side,
                    result=CASE WHEN excluded.result!='Unresolved' THEN excluded.result
                                ELSE encounters.result END,
                    seen_at=MAX(encounters.seen_at,excluded.seen_at)
                """,
                (owner, puuid, match_id, match_id, side, outcome, stamp),
            )


def record_career(owner: str | None, matches: list[dict] | None) -> int:
    if not owner or not isinstance(matches, list):
        return 0
    changed = 0
    for match in matches:
        match_id = match.get("matchId")
        if not match_id:
            continue
        owner_team = match.get("team") or "Owner"
        players = [{"puuid": owner, "team": owner_team, "result": match.get("result"),
                    "resultExact": bool(match.get("resultExact")), "isSelf": True}]
        for teammate in match.get("teammates") or []:
            players.append({**teammate, "team": owner_team,
                            "nameConfirmed": bool(teammate.get("nameConfirmed")),
                            "result": match.get("result"),
                            "resultExact": bool(match.get("resultExact"))})
        payload = {
            "matchId": match_id, "map": match.get("map"), "mode": match.get("mode"),
            "startMillis": match.get("startMillis"), "scores": match.get("scores") or {},
            "players": players,
        }
        changed += int(save_match(payload, owner=owner, provenance="local_history"))
    return changed


def _player_row(db: sqlite3.Connection, puuid: str) -> dict | None:
    row = db.execute("SELECT * FROM players WHERE puuid=?", (puuid,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["sources"] = _json(out.pop("sources_json"), [])
    out["saved"] = out.pop("saved_at") is not None
    out["nameHistory"] = [
        dict(item)
        for item in db.execute(
            "SELECT riot_id AS riotId,source,first_seen AS firstSeen,last_seen AS lastSeen "
            "FROM player_names WHERE puuid=? ORDER BY last_seen DESC",
            (puuid,),
        )
    ]
    aliases = {
        "riot_id": "riotId", "first_seen": "firstSeen", "last_seen": "lastSeen",
        "rank_tier": "rankTier", "peak_tier": "peakTier",
        "previous_tier": "previousTier", "hs_pct": "hsPct",
        "win_rate": "winRate", "stats_refreshed_at": "statsRefreshedAt",
        "saved_note": "savedNote",
    }
    for old, new in aliases.items():
        out[new] = out.pop(old)
    return out


def get_player(puuid: str) -> dict | None:
    with _connect() as db:
        return _player_row(db, puuid)


def list_players(query: str | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(500, int(limit)))
    with _connect() as db:
        if query:
            rows = db.execute(
                "SELECT puuid FROM players WHERE puuid LIKE ? OR riot_id LIKE ? "
                "ORDER BY last_seen DESC LIMIT ?",
                (f"%{query}%", f"%{query}%", limit),
            )
        else:
            rows = db.execute("SELECT puuid FROM players ORDER BY last_seen DESC LIMIT ?", (limit,))
        return [_player_row(db, row[0]) for row in rows]


def set_saved(puuid: str, saved: bool, note: str = "") -> dict:
    stamp = _now()
    with _LOCK, _connect() as db:
        upsert_player(puuid, source="saved_player", seen_at=stamp, db=db)
        db.execute(
            "UPDATE players SET saved_at=?,saved_note=? WHERE puuid=?",
            (stamp if saved else None, note.strip()[:500] if saved else "", puuid),
        )
        return _player_row(db, puuid)


def saved_players() -> list[dict]:
    with _connect() as db:
        return [
            _player_row(db, row[0])
            for row in db.execute(
                "SELECT puuid FROM players WHERE saved_at IS NOT NULL ORDER BY saved_at DESC"
            )
        ]


def _stored_match(db: sqlite3.Connection, match_id: str, subject=None) -> dict | None:
    match = db.execute("SELECT * FROM matches WHERE match_id=?", (match_id,)).fetchone()
    if not match:
        return None
    players = []
    for row in db.execute(
        "SELECT * FROM match_players WHERE match_id=? ORDER BY COALESCE(acs,-1) DESC",
        (match_id,),
    ):
        player = dict(row)
        player.update({
            "name": player.pop("riot_id") or f"Player-{player['puuid'][:4].upper()}",
            "nameConfirmed": bool(player.pop("name_confirmed")),
            "partyId": player.pop("party_id"),
            "hsPct": player.pop("hs_pct"),
            "rankTier": player.pop("rank_tier"),
            "peakRankTier": player.pop("peak_tier"),
            "playerCard": player.pop("player_card"),
            "resultExact": bool(player.pop("result_exact")),
            "isSubject": player["puuid"] == subject,
        })
        player.pop("match_id")
        player.pop("is_local")
        players.append(player)
    result = next((p["result"] for p in players if p["isSubject"]), "Unresolved")
    return {
        "matchId": match_id,
        "map": match["map"],
        "mode": match["mode"],
        "startedAt": match["started_at"],
        "scores": _json(match["scores_json"], {}),
        "result": result,
        "resultExact": result != "Unresolved",
        "players": players,
        "complete": bool(match["complete"]),
        "source": "stored",
        "live": False,
    }


def get_match(match_id: str, subject=None) -> dict | None:
    with _connect() as db:
        return _stored_match(db, match_id, subject)


def list_matches(puuid: str | None = None, limit: int = 20) -> list[dict]:
    limit = max(1, min(200, int(limit)))
    with _connect() as db:
        if puuid:
            ids = db.execute(
                "SELECT m.match_id FROM matches m JOIN match_players p USING(match_id) "
                "WHERE p.puuid=? ORDER BY m.started_at DESC LIMIT ?", (puuid, limit)
            )
        else:
            ids = db.execute("SELECT match_id FROM matches ORDER BY started_at DESC LIMIT ?", (limit,))
        return [_stored_match(db, row[0], puuid) for row in ids]


def stored_profile(puuid: str, limit: int = 8) -> dict | None:
    player = get_player(puuid)
    if not player:
        return None
    return {
        **player,
        "puuid": puuid,
        "matches": list_matches(puuid, limit),
        "source": "stored",
        "live": False,
        "available": False,
    }


def career_match(match_id: str, puuid: str) -> dict | None:
    detail = get_match(match_id, puuid)
    if not detail or not detail.get("complete"):
        return None
    subject = next((player for player in detail["players"] if player["puuid"] == puuid), None)
    if not subject:
        return None
    teammates = [
        {key: player.get(key) for key in (
            "puuid", "name", "nameConfirmed", "agent", "level", "kills", "deaths",
            "assists", "acs", "kd", "hsPct",
        )}
        for player in detail["players"]
        if player["puuid"] != puuid and player.get("team") == subject.get("team")
    ]
    scores = detail.get("scores") or {}
    return {
        "matchId": match_id,
        "map": detail.get("map"),
        "mode": detail.get("mode"),
        "startMillis": int(detail.get("startedAt") or 0) * 1000,
        "result": subject.get("result"),
        "resultExact": bool(subject.get("resultExact")),
        "team": subject.get("team"),
        "score": scores.get(subject.get("team")),
        "opponentScore": next((value for team, value in scores.items()
                               if team != subject.get("team")), None),
        "agent": subject.get("agent"),
        "kills": subject.get("kills") or 0,
        "deaths": subject.get("deaths") or 0,
        "assists": subject.get("assists") or 0,
        "kd": subject.get("kd"),
        "acs": subject.get("acs"),
        "hsPct": subject.get("hsPct"),
        "partySize": sum(player.get("partyId") == subject.get("partyId")
                         for player in detail["players"])
                     if subject.get("partyId") else 1,
        "scores": scores,
        "teammates": teammates,
    }


def _encounter_rows(owner: str | None) -> list[dict]:
    if not owner:
        return []
    with _connect() as db:
        rows = db.execute(
            """
            SELECT e.player_puuid,
                   SUM(CASE WHEN side='with' THEN count ELSE 0 END) withCount,
                   SUM(CASE WHEN side='against' THEN count ELSE 0 END) againstCount,
                   MIN(seen_at) firstEncounter,MAX(seen_at) lastEncounter,
                   p.riot_id,p.rank_tier,p.peak_tier,p.level,p.kd,p.win_rate
            FROM encounters e LEFT JOIN players p ON p.puuid=e.player_puuid
            WHERE e.owner_puuid=? GROUP BY e.player_puuid
            ORDER BY lastEncounter DESC
            """,
            (owner,),
        ).fetchall()
        output = []
        for row in rows:
            item = {
                "puuid": row["player_puuid"], "name": row["riot_id"],
                "withCount": row["withCount"], "againstCount": row["againstCount"],
                "firstEncounter": row["firstEncounter"], "lastSeen": row["lastEncounter"],
                "rankTier": row["rank_tier"], "peakTier": row["peak_tier"],
                "level": row["level"], "kd": row["kd"], "winRate": row["win_rate"],
            }
            item["timeline"] = [
                {
                    "matchId": event["match_id"], "side": event["side"],
                    "result": event["result"].lower(), "at": event["seen_at"],
                    "count": event["count"],
                }
                for event in db.execute(
                    "SELECT match_id,side,result,seen_at,count FROM encounters "
                    "WHERE owner_puuid=? AND player_puuid=? ORDER BY seen_at DESC",
                    (owner, row["player_puuid"]),
                )
            ]
            for side, suffix in (("with", "With"), ("against", "Against")):
                counts = {key: 0 for key in _OUTCOMES}
                match_ids = []
                for detail in db.execute(
                    "SELECT result,SUM(count) total,match_id FROM encounters "
                    "WHERE owner_puuid=? AND player_puuid=? AND side=? "
                    "GROUP BY result,match_id",
                    (owner, row["player_puuid"], side),
                ):
                    counts[detail["result"]] += detail["total"]
                    if detail["match_id"]:
                        match_ids.append(detail["match_id"])
                item.update({
                    f"wins{suffix}": counts["Victory"],
                    f"losses{suffix}": counts["Defeat"],
                    f"draws{suffix}": counts["Draw"],
                    f"unresolved{suffix}": counts["Unresolved"],
                    f"matchIds{suffix}": sorted(set(match_ids)),
                })
                decided = counts["Victory"] + counts["Defeat"]
                item[f"{side}Decided"] = decided
                item[f"{side}WinRate"] = round(100 * counts["Victory"] / decided, 1) if decided else None
            output.append(item)
        return output


def encounters(owner: str | None, limit: int = 200) -> list[dict]:
    return _encounter_rows(owner)[:max(0, int(limit))]


def encounter(owner: str | None, puuid: str) -> dict | None:
    return next((row for row in _encounter_rows(owner) if row["puuid"] == puuid), None)


def all_encounters(limit: int = 200) -> list[dict]:
    merged: dict[str, dict] = {}
    with _connect() as db:
        owners = [row[0] for row in db.execute("SELECT DISTINCT owner_puuid FROM encounters")]
    for owner in owners:
        for row in _encounter_rows(owner):
            target = merged.get(row["puuid"])
            if target is None:
                target = {**row, "accountsSeen": []}
                merged[row["puuid"]] = target
            else:
                for key in (
                    "withCount", "againstCount", "winsWith", "lossesWith", "drawsWith",
                    "unresolvedWith", "winsAgainst", "lossesAgainst", "drawsAgainst",
                    "unresolvedAgainst",
                ):
                    target[key] = int(target.get(key) or 0) + int(row.get(key) or 0)
            target["accountsSeen"].append(owner)
    return sorted(merged.values(), key=lambda row: -(row["withCount"] + row["againstCount"]))[:limit]


def account_count() -> int:
    with _connect() as db:
        return db.execute("SELECT COUNT(DISTINCT owner_puuid) FROM encounters").fetchone()[0]


def update_match_note(owner: str | None, match_id: str, payload: object) -> dict:
    if not owner or not match_id:
        return {"ok": False, "message": "An active account and match are required."}
    body = payload if isinstance(payload, dict) else {}
    note = str(body.get("note") or "").strip()[:500]
    raw_tags = body.get("tags") or []
    tags = []
    if isinstance(raw_tags, (list, tuple)):
        for raw in raw_tags:
            tag = str(raw).strip()[:24]
            if tag and tag.lower() not in {item.lower() for item in tags}:
                tags.append(tag)
            if len(tags) == 5:
                break
    bookmarked = bool(body.get("bookmarked"))
    stamp = _now()
    with _LOCK, _connect() as db:
        if note or tags or bookmarked:
            db.execute(
                """
                INSERT INTO match_notes(owner_puuid,match_id,note,tags_json,bookmarked,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(owner_puuid,match_id) DO UPDATE SET
                note=excluded.note,tags_json=excluded.tags_json,
                bookmarked=excluded.bookmarked,updated_at=excluded.updated_at
                """,
                (owner, match_id, note, json.dumps(tags), int(bookmarked), stamp),
            )
        else:
            db.execute("DELETE FROM match_notes WHERE owner_puuid=? AND match_id=?", (owner, match_id))
    return {"ok": True, "matchId": match_id,
            "meta": {"note": note, "tags": tags, "bookmarked": bookmarked, "updatedAt": stamp}}


def match_note(owner: str | None, match_id: str) -> dict:
    if not owner:
        return {"note": "", "tags": [], "bookmarked": False}
    with _connect() as db:
        row = db.execute(
            "SELECT * FROM match_notes WHERE owner_puuid=? AND match_id=?", (owner, match_id)
        ).fetchone()
        if not row:
            return {"note": "", "tags": [], "bookmarked": False}
        return {"note": row["note"], "tags": _json(row["tags_json"], []),
                "bookmarked": bool(row["bookmarked"]), "updatedAt": row["updated_at"]}


def all_match_notes(owner: str | None) -> dict:
    if not owner:
        return {}
    with _connect() as db:
        return {
            row["match_id"]: {"note": row["note"], "tags": _json(row["tags_json"], []),
                              "bookmarked": bool(row["bookmarked"]),
                              "updatedAt": row["updated_at"]}
            for row in db.execute("SELECT * FROM match_notes WHERE owner_puuid=?", (owner,))
        }


def save_inventory(puuid: str | None, payload: dict) -> None:
    if not puuid or not isinstance(payload, dict) or not payload.get("available"):
        return
    clean = {key: payload.get(key) for key in (
        "available", "totalVp", "usdApprox", "wallet", "counts", "tiers", "at"
    ) if key in payload}
    for key in ("top", "recent"):
        clean[key] = [
            {field: item.get(field) for field in ("name", "icon", "vp", "tier")
             if field in item and not str(item.get(field) or "").startswith("data:")}
            for item in (payload.get(key) or []) if isinstance(item, dict)
        ]
    with _LOCK, _connect() as db:
        db.execute(
            "INSERT INTO inventory_snapshots(puuid,payload_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(puuid) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (puuid, json.dumps(clean, separators=(",", ":")), _now()),
        )


def stored_inventory(puuid: str | None) -> dict | None:
    if not puuid:
        return None
    with _connect() as db:
        row = db.execute("SELECT * FROM inventory_snapshots WHERE puuid=?", (puuid,)).fetchone()
        if not row:
            return None
        return {**_json(row["payload_json"], {}), "source": "stored", "live": False,
                "stale": True, "storedAt": row["updated_at"]}


def party_groups(players: list[dict], confirmed: list[dict] | None = None) -> list[dict]:
    groups = []
    covered = set()
    for party in confirmed or []:
        members = sorted(set(party.get("members") or []))
        if len(members) > 1:
            groups.append({"team": next((p.get("team") for p in players if p.get("puuid") in members), None),
                           "members": members, "confidence": "confirmed",
                           "partyId": party.get("id"), "evidenceCount": 0,
                           "evidenceMatchIds": []})
            covered.update(members)
    by_team: dict[str, list[str]] = {}
    for player in players:
        if player.get("puuid") and player.get("puuid") not in covered and player.get("team"):
            by_team.setdefault(player["team"], []).append(player["puuid"])
    with _connect() as db:
        for team, puuids in by_team.items():
            edges = {}
            for index, left in enumerate(puuids):
                for right in puuids[index + 1:]:
                    rows = db.execute(
                        """
                        SELECT a.match_id FROM match_players a JOIN match_players b USING(match_id)
                        JOIN matches m USING(match_id)
                        WHERE a.puuid=? AND b.puuid=? AND a.team=b.team AND m.complete=1
                        ORDER BY m.started_at DESC LIMIT 8
                        """,
                        (left, right),
                    ).fetchall()
                    if rows:
                        edges[(left, right)] = [row[0] for row in rows]
            remaining = set(puuids)
            while remaining:
                component = {remaining.pop()}
                changed = True
                while changed:
                    changed = False
                    for pair in edges:
                        if component.intersection(pair) and not set(pair).issubset(component):
                            component.update(pair)
                            remaining.difference_update(pair)
                            changed = True
                evidence = sorted({mid for pair, mids in edges.items()
                                   if set(pair).issubset(component) for mid in mids})
                if len(component) > 1 and evidence:
                    groups.append({"team": team, "members": sorted(component),
                                   "confidence": "possible" if len(evidence) >= 2 else "weak",
                                   "evidenceCount": len(evidence), "evidenceMatchIds": evidence})
    return groups


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrate_legacy(data_dir: str | os.PathLike) -> None:
    directory = Path(data_dir)
    for name, importer in (
        ("encounters.json", _import_encounters),
        ("rr_history.json", _import_history),
        ("match_meta.json", _import_match_meta),
    ):
        path = directory / name
        if not path.is_file():
            continue
        try:
            fingerprint = _fingerprint(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = f"legacy:{name}"
        with _LOCK, _connect() as db:
            if _meta_get(db, key) == fingerprint:
                continue
            importer(db, raw)
            _meta_set(db, key, fingerprint)


def _import_history(db: sqlite3.Connection, raw: object) -> None:
    for owner, account in ((raw or {}).get("accounts") or {}).items() if isinstance(raw, dict) else []:
        upsert_player(owner, source="legacy_rr_history", db=db)
        _meta_set(db, "last_local_puuid", owner)
        for point in (account or {}).get("points") or []:
            match_id = point.get("matchId")
            if not match_id:
                continue
            db.execute(
                """
                INSERT INTO matches(match_id,started_at,map,mode,scores_json,complete,provenance,updated_at)
                VALUES(?,?,?,?,?,0,'legacy_rr_history',?)
                ON CONFLICT(match_id) DO UPDATE SET
                    started_at=COALESCE(matches.started_at,excluded.started_at),
                    map=COALESCE(matches.map,excluded.map),mode=COALESCE(matches.mode,excluded.mode)
                """,
                (match_id, point.get("ts"), point.get("map"), point.get("mode"),
                 json.dumps(point.get("scores") or {}), _now()),
            )


def _import_match_meta(db: sqlite3.Connection, raw: object) -> None:
    for owner, matches in ((raw or {}).get("accounts") or {}).items() if isinstance(raw, dict) else []:
        for match_id, meta in (matches or {}).items():
            db.execute(
                """
                INSERT INTO match_notes(owner_puuid,match_id,note,tags_json,bookmarked,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(owner_puuid,match_id) DO NOTHING
                """,
                (owner, match_id, str(meta.get("note") or "")[:500],
                 json.dumps(meta.get("tags") or []), int(bool(meta.get("bookmarked"))),
                 int(meta.get("updatedAt") or time.time())),
            )


def _import_encounters(db: sqlite3.Connection, raw: object) -> None:
    accounts = (raw or {}).get("accounts") if isinstance(raw, dict) else {}
    for owner, account in (accounts or {}).items():
        _meta_set(db, "last_local_puuid", owner)
        for puuid, source in ((account or {}).get("players") or {}).items():
            current = db.execute("SELECT riot_id FROM players WHERE puuid=?", (puuid,)).fetchone()
            legacy_name = None if current and current[0] else source.get("name")
            upsert_player(puuid, riot_id=legacy_name, confirmed=bool(legacy_name),
                          source="legacy_encounter", seen_at=source.get("lastSeen"), fields=source, db=db)
            known = {key: 0 for key in ("winsWith", "lossesWith", "drawsWith", "unresolvedWith",
                                              "winsAgainst", "lossesAgainst", "drawsAgainst", "unresolvedAgainst")}
            outcomes = source.get("resultByMatch") or {}
            timeline = {item.get("matchId"): item for item in source.get("timeline") or [] if item.get("matchId")}
            for match_id, info in outcomes.items():
                side = info.get("side") or (timeline.get(match_id) or {}).get("side")
                result = info.get("result") or "Unresolved"
                if side not in ("with", "against"):
                    continue
                suffix = "With" if side == "with" else "Against"
                prefix = {"Victory": "wins", "Defeat": "losses", "Draw": "draws"}.get(result, "unresolved")
                known[prefix + suffix] += 1
                db.execute(
                    "INSERT OR IGNORE INTO encounters(owner_puuid,player_puuid,match_key,match_id,side,result,seen_at,count) "
                    "VALUES(?,?,?,?,?,?,?,1)",
                    (owner, puuid, str(match_id), str(match_id), side,
                     result if result in _OUTCOMES else "Unresolved",
                     int((timeline.get(match_id) or {}).get("at") or source.get("lastSeen") or time.time())),
                )
            for side, suffix in (("with", "With"), ("against", "Against")):
                for result, prefix in (("Victory", "wins"), ("Defeat", "losses"),
                                       ("Draw", "draws"), ("Unresolved", "unresolved")):
                    key = prefix + suffix
                    deficit = max(0, int(source.get(key) or 0) - known[key])
                    if deficit:
                        db.execute(
                            "INSERT OR IGNORE INTO encounters(owner_puuid,player_puuid,match_key,match_id,side,result,seen_at,count) "
                            "VALUES(?,?,?,NULL,?,?,?,?)",
                            (owner, puuid, f"legacy:{side}:{result}", side, result,
                             int(source.get("lastSeen") or time.time()), deficit),
                        )


init()
