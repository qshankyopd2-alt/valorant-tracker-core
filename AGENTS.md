# VALORANT Tracker Core

## Scope

- Maintain a local, EU-only, read-only VALORANT data backend exposed as REST JSON on `127.0.0.1`.
- Preserve the proven request flow: Riot lockfile, entitlements, PD/GLZ, presence, match history/detail, and MMR.
- Keep the backend independent of any frontend, product name, or UI framework.

## Boundaries

- Riot operations read state only. Local writes for settings, sessions, notes, metadata, and SQLite persistence are allowed.
- Keep this repository limited to the REST backend, read-only Riot state, and local persistence. Build interfaces and additional transports in separate repositories.
- Keep Riot credentials and headers transient. Never persist or log lockfile passwords, access tokens, entitlement tokens, API keys, or `Authorization` headers.
- Treat `PUUID` as player identity. Persist Riot IDs only when confirmed by Name Service, Account API, or match detail.

## Ownership

- `riot_client.py`: local authentication and bounded Riot requests.
- `live_match.py`: live state, roster enrichment, profiles, and match ingestion.
- `knowledge_store.py`: durable SQLite knowledge and idempotency.
- `tracker_runtime.py`: presence transitions, startup reconciliation, and name retries.
- `app.py`: thin REST JSON adapter for any future UI.

## Graphify workflow

Use the hosted Graphify MCP at `https://api.graphify.com/mcp` with repository `qshankyopd2-alt/valorant-tracker-core`. Do not create or use a local `graphify-out/` graph for this repository.

- Before answering a codebase question, planning a change, or editing files, use the Graphify MCP for orientation before raw file search.
- Run `recall` before acting to recover durable decisions and gotchas. Run `memories_about` when starting work on a specific file or symbol.
- Prefer `query_graph` for scoped context. Use the exact graph tools for callers, callees, traces, references, imports/exports, file neighbors, impact, and linked tests when those relationships matter.
- Verify graph findings against the current source and runnable tests before making or reporting a change; the graph is static evidence, not runtime verification.
- Record durable decisions and constraints with `remember` so later agents do not have to rediscover them.
- After pushed code changes, verify that Graphify reflects the new repository HEAD before treating its index as current.

## Change workflow

1. Trace callers before removing or changing shared Riot behavior; the cleaned local contracts remain authoritative when source behavior falls outside this scope.
2. When comparison with the original Scout flow is necessary, use the source repository and pinned baseline commit documented in `README.md`; do not assume it is present in the active Graphify workspace.
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
