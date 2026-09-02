# VALORANT Tracker Core

A reusable, local REST JSON backend for building different VALORANT tracker interfaces without rebuilding Riot data access.

## What it keeps

- Riot lockfile and entitlement authentication
- PD/GLZ, presence, match history/detail, and MMR reads
- Live PREGAME and INGAME rosters
- Rank, RR, peak/previous rank, K/D, HS%, win rate, inventory, skins, cards, titles, and match data
- Persistent players, name history, matches, encounters, saved players, sessions, and metadata in SQLite
- Offline reads from stored data

## What it excludes

Frontend code, Discord RPC, Ably/remote mode, WebSocket bridges, telemetry, demo data, offline-presence manipulation, Instalock, Dodge, queue control, and Scout branding.

`chat_presences()` is retained because it reads Riot presence state; it is not a messaging feature. The Riot lockfile is also required for local authentication.

## Run

```powershell
python -m pip install -r requirements.txt
python app.py
```

The API binds to `127.0.0.1:5000` by default. Open `http://127.0.0.1:5000/` to list the integration endpoints.

## Source and license

The Riot request flow is derived from [Valorant Scout](https://github.com/qshankyopd2-alt/Valorant-Scout), baseline commit `ab466b7da5c44412567baa5b82a5c9d890b71a10`, and substantially cleaned for a reusable read-only backend.

Licensed under GNU GPL v3.0. See `LICENSE`.
