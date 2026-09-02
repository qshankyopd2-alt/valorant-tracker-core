from __future__ import annotations

import knowledge_store


def get_all(puuid: str | None) -> dict:
    return knowledge_store.all_match_notes(puuid)


def get_one(puuid: str | None, match_id: str) -> dict:
    return knowledge_store.match_note(puuid, match_id)


def update(puuid: str | None, match_id: str, payload: object) -> dict:
    return knowledge_store.update_match_note(puuid, match_id, payload)
