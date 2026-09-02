# VALORANT Tracker Core

## Scope

- Maintain a local, EU-only, read-only VALORANT data backend exposed as REST JSON on `127.0.0.1`.
- Preserve the proven request flow: Riot lockfile, entitlements, PD/GLZ, presence, match history/detail, and MMR.
- Keep the backend independent of any frontend, product name, or UI framework.

## Boundaries

- Riot operations read state only. Local writes for settings, sessions, notes, metadata, and SQLite persistence are allowed.
- Keep game-control, Discord, remote/cloud channels, WebSocket bridges, demo data, offline-presence manipulation, telemetry, and UI code outside this repository.
- Keep Riot credentials and headers transient. Never persist or log lockfile passwords, access tokens, entitlement tokens, API keys, or `Authorization` headers.
- Treat `PUUID` as player identity. Persist Riot IDs only when confirmed by Name Service, Account API, or match detail.

## Ownership

- `riot_client.py`: local authentication and bounded Riot requests.
- `live_match.py`: live state, roster enrichment, profiles, and match ingestion.
- `knowledge_store.py`: durable SQLite knowledge and idempotency.
- `tracker_runtime.py`: presence transitions, startup reconciliation, and name retries.
- `app.py`: thin REST JSON adapter for any future UI.

## Change workflow

1. Before modifying runtime code or removing a module, query Graphify repository `qshankyopd2-alt/Valorant-Scout` to locate the inherited flow and its dependencies.
2. Trace callers before removing or changing shared Riot behavior; the cleaned local contracts remain authoritative when the Scout source contains excluded features.
3. Preserve explicit `Victory`, `Defeat`, `Draw`, and unresolved outcomes.
4. Add one focused regression check for non-trivial behavior, then run the full validation below.

## Validation

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
python -c "import app,knowledge_store,tracker_runtime,live_match,encounter_log,history,inventory,match_meta,session_tracker,riot_client; print('imports-ok')"
```

A passing mocked test does not prove a live PREGAME-to-INGAME flow. Require a real VALORANT test before claiming live behavior is verified.

Before any `git commit` or `git push`, run `@ponytail-review`; repeat it after the commit before pushing.
