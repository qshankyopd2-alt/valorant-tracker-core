from __future__ import annotations

import knowledge_store


def record_board(board: dict | None) -> None:
    if isinstance(board, dict):
        knowledge_store.remember_board(board)


def record_result(board: dict | None, result: str | None) -> None:
    if isinstance(board, dict):
        knowledge_store.record_encounters(board, result)


def backfill_career(owner: str | None, matches: list[dict] | None) -> int:
    return knowledge_store.record_career(owner, matches)


def enrich_player(owner: str | None, puuid: str | None, fields: dict | None) -> None:
    del owner
    knowledge_store.upsert_player(puuid, source="rank_enrichment", fields=fields or {})


def get_all(owner: str | None, limit: int = 200) -> list[dict]:
    return knowledge_store.encounters(owner, limit)


def account_count() -> int:
    return knowledge_store.account_count()


def get_all_accounts(limit: int = 200) -> list[dict]:
    return knowledge_store.all_encounters(limit)


def get_one(owner: str | None, puuid: str) -> dict | None:
    return knowledge_store.encounter(owner, puuid)


def encounter_for(owner: str | None, puuid: str) -> dict | None:
    row = get_one(owner, puuid)
    if not row:
        return None
    return {key: row.get(key) for key in (
        "withCount", "againstCount", "winsWith", "lossesWith", "drawsWith",
        "unresolvedWith", "winsAgainst", "lossesAgainst", "drawsAgainst",
        "unresolvedAgainst", "withWinRate", "againstWinRate", "withDecided",
        "againstDecided",
    )}
