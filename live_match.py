from __future__ import annotations

import base64
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import knowledge_store
import riot_client
import valapi
from agents import UUID_TO_NAME, resolve_agent
from vconstants import (GAMEMODES, party_color, rank_from_tier,
                        map_name_from_path, STATES)

def _mode_label(queue: str) -> str:
    pass
    if not queue:
        return "Custom"
    return GAMEMODES.get(queue.lower(), queue.replace("_", " ").title())


def match_outcome(teams: dict, team_id: str | None) -> str | None:
    """Return a conservative subject-relative result for a completed match."""
    if not team_id or not isinstance(teams, dict) or team_id not in teams:
        return None
    mine = teams.get(team_id) or {}
    opponents = [team for key, team in teams.items() if key != team_id]
    if not opponents:
        return None
    if mine.get("won") is True:
        return "Victory"
    if any(team.get("won") is True for team in opponents):
        return "Defeat"

    def score(team: dict) -> int | float | None:
        for key in ("roundsWon", "numPoints"):
            value = team.get(key)
            if isinstance(value, (int, float)):
                return value
        return None

    own_score = score(mine)
    opponent_scores = [score(team) for team in opponents]
    if own_score is None or any(value is None for value in opponent_scores):
        return None
    best_opponent = max(opponent_scores)
    if own_score > 0 and own_score == best_opponent:
        return "Draw"
    if own_score > best_opponent:
        return "Victory"
    if own_score < best_opponent:
        return "Defeat"
    return None


def _outcome_token(result: str | None) -> str:
    return {"Victory": "W", "Defeat": "L", "Draw": "D"}.get(result, "U")

BEFORE_ASCENDANT = {
    "0df5adb9-4dcb-6899-1306-3e9860661dd3", "3f61c772-4560-cd3f-5d3f-a7ab5abda6b3",
    "0530b9c4-4980-f2ee-df5d-09864cd00542", "46ea6166-4573-1128-9cea-60a15640059b",
    "fcf2c8f4-4324-e50b-2e23-718e4a3ab046", "97b6e739-44cc-ffa7-49ad-398ba502ceb0",
    "ab57ef51-4e59-da91-cc8d-51a5a2b9b8ff", "52e9749a-429b-7060-99fe-4595426a0cf7",
    "71c81c67-4fae-ceb1-844c-aab2bb8710fa", "2a27e5d2-4d30-c9e2-b15a-93b8909a442c",
    "4cb622e1-4244-6da3-7276-8daaf1c01be2", "a16955a5-4ad0-f761-5e9e-389df1c892fb",
    "97b39124-46ce-8b55-8fd1-7cbf7ffe173f", "573f53ac-41a5-3a7d-d9ce-d6a6298e5704",
    "d929bc38-4ab6-7da4-94f0-ee84f8ac141e", "3e47230a-463c-a301-eb7d-67bb60357d4f",
    "808202d6-4f2b-a8ff-1feb-b3a0590ad79f",
}

_CACHE: dict[str, dict] = {}

_MATCH_META: dict[str, dict] = {}

_LOBBY_CACHE: dict = {"key": None, "at": 0.0, "board": None}

_LAST_BOARD: dict = {"board": None, "at": 0.0}
_HOLD_SECS = 90.0

_ACCT_CACHE: dict[str, str | None] = {}

_ROUTING = {"na": "americas", "latam": "americas", "br": "americas",
            "eu": "europe", "ap": "asia", "kr": "asia"}

_CONTENT_CACHE: dict = {"seasons": None, "at": 0.0}

_LEVEL_CACHE: dict[str, int] = {}

_KD_FILL_LOCK = threading.Lock()
_KD_FILLING: set[str] = set()

_KD_CACHE: dict[str, tuple[tuple, tuple, int]] = {}
_KD_CACHE_MAX = 300

_MIDS_CACHE: dict[str, tuple[list[str], bool, float]] = {}
_MIDS_TTL = 60.0

_RANK_CACHE: dict[str, tuple[dict, str]] = {}
_RR_CACHE: dict[str, tuple] = {}


_CACHE_WRITE_LOCK = threading.Lock()

def _cache_put(cache: dict, cap: int, key, value) -> None:
    pass
    with _CACHE_WRITE_LOCK:
        while len(cache) >= cap:
            cache.pop(next(iter(cache)), None)
        cache[key] = value

_QUEUE_CACHE: dict = {"at": 0.0, "data": None}

def _log(msg: str) -> None:
    if os.getenv("TRACKER_QUIET"):
        return
    print(f"[live_match] {msg}", flush=True)

def _is_throttled(resp) -> bool:
    pass
    return isinstance(resp, dict) and resp.get("status") == 429

def _fallback_name(puuid: str) -> str:
    pass
    return f"Player-{(puuid or '????')[:4].upper()}"

def smurf_signals(*, level, peak_tier, rank_tier, kd, win_rate, games) -> list[str]:
    pass
    reasons: list[str] = []
    lvl = level or 0
    if lvl <= 0:
        return reasons
    if lvl < 60 and (peak_tier or 0) >= 20:
        reasons.append(f"Lvl {lvl}, peak {rank_from_tier(peak_tier)['name']}")
    if kd is not None and kd >= 1.35 and lvl < 80:
        reasons.append(f"K/D {kd} at lvl {lvl}")
    if win_rate is not None and win_rate >= 62 and (games or 0) >= 15 and lvl < 100:
        reasons.append(f"{win_rate}% WR")
    return reasons

def form_streak(form: list) -> dict | None:
    pass
    if not form or form[0] not in ("W", "L"):
        return None
    t, n = form[0], 1
    for r in form[1:]:
        if r != t:
            break
        n += 1
    return {"type": t, "count": n}

def compute_smurf(*, level, peak_tier, rank_tier, kd, win_rate, games) -> tuple[bool, list[str]]:
    pass
    reasons = smurf_signals(level=level, peak_tier=peak_tier, rank_tier=rank_tier,
                            kd=kd, win_rate=win_rate, games=games)
    if not reasons:
        return False, []
    flagged = ((level or 0) < 60 and len(reasons) >= 1) or len(reasons) >= 2
    return flagged, reasons

def assemble_player(*, puuid, name, name_hidden, team, is_self, agent_id,
                    rank_tier, rr, leaderboard, peak_tier, prev_tier,
                    win_rate, games, kd, hs, level, level_hidden, party,
                    skin=None, peak_act=None, rr_earned=None,
                    player_card=None, title=None, weapons=None,
                    selection=None, smurf=False, smurf_reasons=None,
                    intel=None, name_confirmed=False, name_source=None,
                    season_win_rate=None, season_games=0, season_wins=0) -> dict:
    pass
    agent = resolve_agent(agent_id or "") or {}
    rank = rank_from_tier(rank_tier)
    peak = rank_from_tier(peak_tier)
    prev = rank_from_tier(prev_tier)
    intel = intel or {}
    exact_recent_rate = intel.get("winRate")
    display_rate = exact_recent_rate if exact_recent_rate is not None else win_rate
    display_games = intel.get("decidedGames") if exact_recent_rate is not None else games
    display_basis = "recent_decided_matches" if exact_recent_rate is not None else (
        "season_all_games" if win_rate is not None else None
    )
    return {
        "puuid": puuid,
        "name": name,
        "nameConfirmed": bool(name_confirmed),
        "nameSource": name_source,
        "nameHidden": bool(name_hidden),
        "team": team,
        "isSelf": bool(is_self),
        "title": title,
        "playerCard": player_card,
        "agent": agent.get("name") or (agent_id and "Unknown") or None,
        "agentId": agent.get("uuid"),
        "agentPortrait": agent.get("portrait"),
        "agentArt": agent.get("fullPortrait"),
        "agentColor": agent.get("color", "#8B978F"),
        "role": agent.get("role"),
        "selection": selection,
        "rankTier": rank["tier"],
        "rank": rank["name"],
        "rankColor": rank["color"],
        "rankGroup": rank["group"],
        "rankIcon": valapi.rank_icon(rank["tier"]),
        "rr": rr,
        "rrEarned": rr_earned,
        "leaderboard": leaderboard or 0,
        "peakRankTier": peak["tier"],
        "peakRank": peak["name"],
        "peakColor": peak["color"],
        "peakIcon": valapi.rank_icon(peak["tier"]),
        "peakAct": peak_act,
        "previousRank": prev["name"],
        "winRate": display_rate,
        "winRateBasis": display_basis,
        "games": display_games or 0,
        "winRateGames": display_games or 0,
        "seasonWinRate": season_win_rate,
        "seasonGames": season_games,
        "seasonWins": season_wins,
        "kd": kd,
        "hsPct": hs,
        "skin": skin,
        "weapons": weapons or [],
        "level": level,
        "levelHidden": bool(level_hidden),
        "party": party,
        "smurf": bool(smurf),
        "smurfReasons": smurf_reasons or [],

        "topAgents": intel.get("topAgents") or [],
        "form": intel.get("form") or [],
        "streak": intel.get("streak"),
        "formScope": intel.get("formScope"),
        "recentDecidedGames": intel.get("decidedGames"),
        "recentDraws": intel.get("draws"),
        "recentUnresolved": intel.get("unresolved"),
        "mapWins": intel.get("mapWins") or {},
    }

class LiveMatch:
    def __init__(self, auth):
        self.auth = auth
        self.auth.headers()
        self.self_puuid = self.auth.puuid
        self._content = None

    def _presences(self) -> list:
        return riot_client.chat_presences(self.auth)

    @staticmethod
    def _decode_private(private):
        if not private or "{" in str(private):
            return {"isValid": False}
        try:
            decoded = json.loads(base64.b64decode(str(private)).decode("utf-8"))
            return decoded if isinstance(decoded, dict) else {"isValid": False}
        except Exception:
            return {"isValid": False}

    def game_state(self, presences) -> str:
        for p in presences:
            if p.get("puuid") != self.self_puuid:
                continue
            if p.get("product") == "league_of_legends":
                continue
            priv = self._decode_private(p.get("private"))
            if "matchPresenceData" in priv:
                return priv["matchPresenceData"].get("sessionLoopState", "MENUS")
            return priv.get("sessionLoopState", "MENUS")
        return "MENUS"

    def party_map(self, puuids, presences) -> dict:
        pass
        parties: dict[str, list] = {}
        for p in presences:
            if p.get("puuid") not in puuids:
                continue
            priv = self._decode_private(p.get("private"))
            if not priv.get("isValid"):
                continue
            if "partyPresenceData" in priv:
                size = priv["partyPresenceData"].get("partySize", 0)
                pid = priv["partyPresenceData"].get("partyId", "")
            else:
                size = priv.get("partySize", 0)
                pid = priv.get("partyId", "")
            if size > 1 and pid:
                parties.setdefault(pid, []).append(p["puuid"])
        return {pid: m for pid, m in parties.items() if len(m) > 1}

    def party_members(self, presences) -> list:
        pass
        def _fields(priv):
            data = priv.get("partyPresenceData", priv)
            pid = data.get("partyId", "")
            player = priv.get("playerPresenceData", priv)
            return pid, player.get("accountLevel", 0)

        my_party = None
        for p in presences:
            if p.get("puuid") == self.self_puuid:
                priv = self._decode_private(p.get("private"))
                if priv.get("isValid"):
                    my_party = _fields(priv)[0]
                break
        if not my_party:
            return [{"puuid": self.self_puuid, "level": 0, "incognito": False}]

        members = []
        for p in presences:
            priv = self._decode_private(p.get("private"))
            if not priv.get("isValid"):
                continue
            pid, level = _fields(priv)
            if pid == my_party:
                members.append({"puuid": p["puuid"], "level": level,
                                "incognito": False})
        return members or [{"puuid": self.self_puuid, "level": 0, "incognito": False}]

    def reveal_names(self, puuids) -> dict:
        pass
        names: dict[str, str] = {}
        if not puuids:
            return names

        def _ingest(rows):
            if not isinstance(rows, list):
                return
            for entry in rows:
                if not isinstance(entry, dict):
                    continue
                subj = entry.get("Subject")
                game, tag = entry.get("GameName") or "", entry.get("TagLine") or ""
                if subj and game.strip():
                    riot_id = f"{game}#{tag}" if tag else game
                    names[subj] = riot_id
                    knowledge_store.upsert_player(
                        subj, riot_id=riot_id, confirmed=True, source="name_service"
                    )

        try:
            res = self.auth.name_service(puuids)
            if isinstance(res, dict) and res.get("errorCode"):
                res = self.auth.name_service(puuids, refresh=True)
            _ingest(res)
        except Exception:
            pass

        missing = [p for p in puuids if p not in names]
        if missing and len(missing) <= 3:
            for puuid in missing:
                try:
                    _ingest(self.auth.name_service([puuid]))
                except Exception:
                    pass
        return names

    def reveal_via_account_api(self, puuid: str) -> str | None:
        pass
        if puuid in _ACCT_CACHE:
            return _ACCT_CACHE[puuid]
        key = os.getenv("RIOT_API_KEY", "").strip()
        if not key:
            return None
        cluster = _ROUTING.get(self.auth.shard, "americas")
        name = None
        try:
            r = requests.get(
                f"https://{cluster}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}",
                headers={"X-Riot-Token": key}, timeout=8)
            if r.ok:
                j = r.json()
                gn, tl = j.get("gameName"), j.get("tagLine")
                if gn:
                    name = f"{gn}#{tl}" if tl else gn
            elif r.status_code in (401, 403):
                _log("account-v1 rejected the key (check RIOT_API_KEY)")
        except Exception as e:
            _log(f"account-v1 lookup error: {e}")
        _ACCT_CACHE[puuid] = name
        if name:
            knowledge_store.upsert_player(
                puuid, riot_id=name, confirmed=True, source="account_api"
            )
        return name

    def resolve_identity(self, puuid, name_service, ident):
        pass
        name = name_service.get(puuid)
        source = "name_service" if name else None
        if not name:
            name = self.reveal_via_account_api(puuid)
            source = "account_api" if name else None
        level = ident.get("AccountLevel", 0) or 0
        level_hidden = ident.get("HideAccountLevel", False)
        return name or _fallback_name(puuid), level, level_hidden, bool(name), source

    def match_score(self, presences) -> dict | None:
        pass
        for p in presences:
            if p.get("puuid") != self.self_puuid:
                continue
            priv = self._decode_private(p.get("private"))
            data = priv.get("matchPresenceData", priv)
            ally = data.get("partyOwnerMatchScoreAllyTeam")
            enemy = data.get("partyOwnerMatchScoreEnemyTeam")
            if ally is None and enemy is None:

                ally = priv.get("partyOwnerMatchScoreAllyTeam")
                enemy = priv.get("partyOwnerMatchScoreEnemyTeam")
            if ally is None and enemy is None:
                return None
            ally, enemy = int(ally or 0), int(enemy or 0)
            return {"ally": ally, "enemy": enemy, "round": ally + enemy + 1}
        return None

    def loadouts(self, state, match_id) -> dict:
        pass
        path = (f"/core-game/v1/matches/{match_id}/loadouts" if state == "INGAME"
                else f"/pregame/v1/matches/{match_id}/loadouts")
        out: dict[str, list] = {}
        try:
            ld = self.auth.glz_get(path)
            for entry in ld.get("Loadouts", []):
                subj = (entry.get("Subject") or "").lower()
                loadout = entry.get("Loadout", entry) if state == "INGAME" else entry
                items = (loadout or {}).get("Items", {}) or {}

                if not items and isinstance(loadout, dict):
                    items = ((loadout.get("Loadout") or {}).get("Items", {}) or {})
                if subj and items:
                    out[subj] = valapi.loadout_weapons(items)
        except Exception:
            pass
        return out

    def _current_players(self, state):
        pass
        if state == "INGAME":
            cg = self.auth.glz_get(f"/core-game/v1/players/{self.self_puuid}")
            mid = cg.get("MatchID")
            if not mid:
                return None
            match = self.auth.glz_get(f"/core-game/v1/matches/{mid}")
            players = match.get("Players", [])
            queue = (match.get("MatchmakingData") or {}).get("QueueID", "")
            return players, mid, match.get("MapID", ""), queue
        if state == "PREGAME":
            pg = self.auth.glz_get(f"/pregame/v1/players/{self.self_puuid}")
            mid = pg.get("MatchID")
            if not mid:
                return None
            match = self.auth.glz_get(f"/pregame/v1/matches/{mid}")
            ally = match.get("AllyTeam") or {}
            players = []
            for p in ally.get("Players", []):
                p = dict(p)
                p["TeamID"] = ally.get("TeamID", "Blue")
                players.append(p)
            return players, mid, match.get("MapID", ""), match.get("QueueID", "")
        return None

    def _seasons(self):

        now = time.time()
        if _CONTENT_CACHE["seasons"] is not None and now - _CONTENT_CACHE["at"] < 3600:
            return _CONTENT_CACHE["seasons"]
        try:
            data = requests.get(
                f"https://shared.{self.auth.shard}.a.pvp.net/content-service/v3/content",
                headers=self.auth.headers(), timeout=8).json()
            seasons = data.get("Seasons", []) if isinstance(data, dict) else []
            if seasons:
                _CONTENT_CACHE["seasons"] = seasons
                _CONTENT_CACHE["at"] = now
            return seasons or (_CONTENT_CACHE["seasons"] or [])
        except Exception:
            return _CONTENT_CACHE["seasons"] or []

    def season_id(self) -> str | None:
        for s in self._seasons():
            if s.get("IsActive") and s.get("Type") == "act":
                return s["ID"]
        return None

    def prev_season_id(self) -> str | None:
        seasons = self._seasons()
        current = next((s for s in seasons if s.get("IsActive") and s.get("Type") == "act"), None)
        if not current:
            return None
        for s in seasons:
            if s.get("Type") == "act" and s.get("EndTime") == current.get("StartTime"):
                return s["ID"]
        return None

    def _fresh_mids(self, puuid):
        hit = _MIDS_CACHE.get(puuid)
        if hit and time.time() - hit[2] < _MIDS_TTL:
            return hit[0], hit[1], False
        mids: list[str] = []
        is_comp = False
        throttled = False
        for queue in ("competitive", "unrated", "swiftplay", ""):
            q = f"&queue={queue}" if queue else ""
            hist = self.auth.pd_get(
                f"/match-history/v1/history/{puuid}?startIndex=0&endIndex=10{q}",
                retries=3)
            throttled = throttled or _is_throttled(hist)
            entries = (hist or {}).get("History", []) if isinstance(hist, dict) else []
            mids = [e["MatchID"] for e in entries if e.get("MatchID")]
            if mids or throttled:
                is_comp = bool(mids) and queue == "competitive"
                break
        if not throttled:
            _cache_put(_MIDS_CACHE, _KD_CACHE_MAX, puuid, (mids, is_comp, time.time()))
        return mids, is_comp, throttled

    def rank_info(self, puuid, season, prev_season=None):
        out = {"tier": 0, "rr": 0, "lb": 0, "peak": 0,
               "wins": 0, "games": 0, "winRateAllGames": None,
               "winRateBasis": "season_all_games", "prev": 0,
               "peak_season": season, "ok": False}
        try:
            hit = _RANK_CACHE.get(puuid)
            if hit:
                mids, is_comp, _ = self._fresh_mids(puuid)
                rank_key = mids[0] if (mids and is_comp) else "nocomp"
                if hit[1] is None:
                    _cache_put(_RANK_CACHE, _KD_CACHE_MAX, puuid, (hit[0], rank_key))
                    return hit[0]
                if hit[1] == rank_key:
                    return hit[0]
            else:
                mhit = _MIDS_CACHE.get(puuid)
                if mhit and time.time() - mhit[2] < _MIDS_TTL:
                    rank_key = mhit[0][0] if (mhit[0] and mhit[1]) else "nocomp"
                else:
                    rank_key = None
            if riot_client.held_secs("/mmr/") > 0:
                return out
            r = self.auth.pd_get(f"/mmr/v1/players/{puuid}")
            if not isinstance(r, dict) or "QueueSkills" not in r:
                return out
            out["ok"] = True
            si = (((r.get("QueueSkills") or {}).get("competitive") or {})
                  .get("SeasonalInfoBySeasonID")) or {}
            cur = si.get(season, {}) if season else {}
            out["tier"] = cur.get("CompetitiveTier", 0) or 0
            out["rr"] = cur.get("RankedRating", 0) or 0
            out["lb"] = cur.get("LeaderboardRank", 0) or 0

            if prev_season:
                out["prev"] = (si.get(prev_season, {}) or {}).get("CompetitiveTier", 0) or 0
            peak = out["tier"]
            for s, info in si.items():
                for t in (info.get("WinsByTier") or {}):
                    ti = int(t)
                    if s in BEFORE_ASCENDANT and ti > 20:
                        ti += 3
                    if ti > peak:
                        peak = ti
                        out["peak_season"] = s
            out["peak"] = peak
            wins = cur.get("NumberOfWinsWithPlacements", 0) or 0
            games = cur.get("NumberOfGames", 0) or 0
            out["wins"] = wins
            out["games"] = games
            out["winRateAllGames"] = round(wins / games * 100) if games else None
            _cache_put(_RANK_CACHE, _KD_CACHE_MAX, puuid, (out, rank_key))
        except Exception:
            pass
        return out

    def act_episode(self, season_id):
        pass
        if not season_id:
            return None
        label = valapi.season_label(season_id)
        if label:
            return label

        seasons = self._seasons()
        act = ep = None
        for s in seasons:
            if (s.get("Type") or "").lower() == "episode":
                ep = s
            if s.get("ID", "").lower() == season_id.lower():
                act = s
                break
        if not act:
            return None
        num = valapi._act_number(act.get("Name"))
        ep_label = valapi._episode_label((ep or {}).get("Name"))
        if ep_label and num is not None:
            return f"{ep_label} Act {num}"
        if num is not None:
            return f"Act {num}"
        return (act.get("Name") or "").title() or None

    def level_from_history(self, puuid: str) -> int:
        pass
        if puuid in _LEVEL_CACHE:
            return _LEVEL_CACHE[puuid]
        level = 0
        try:
            hist = self.auth.pd_get(
                f"/match-history/v1/history/{puuid}?startIndex=0&endIndex=1")
            entries = (hist or {}).get("History", []) if isinstance(hist, dict) else []
            mid = entries[0].get("MatchID") if entries else None
            if mid:
                md = self.auth.pd_get(f"/match-details/v1/matches/{mid}")
                pl = next((x for x in (md.get("players") or [])
                           if x.get("subject") == puuid), None)
                level = int((pl or {}).get("accountLevel", 0) or 0)
        except Exception:
            level = 0
        if level > 0:
            _LEVEL_CACHE[puuid] = level
        return level

    def kd_hs(self, puuid, count=3):
        pass
        try:
            rr_earned = None

            mids_all, is_comp, throttled = self._fresh_mids(puuid)
            mids = mids_all[:count]
            if not mids:
                return None, None, rr_earned, ("throttled" if throttled else "empty"), None
            cached = _KD_CACHE.get(puuid)
            if (cached and cached[2] >= count
                    and list(cached[1])[:count] == mids):
                return cached[0]

            def fetch_detail(mid):
                return self.ingest_match(
                    mid, puuid, local_owner=puuid == self.self_puuid,
                    provenance="local_history" if puuid == self.self_puuid else "profile_history",
                )

            kills = deaths = hits = heads = used = 0
            agent_counts: dict[str, int] = {}
            form: list[str] = []
            map_wins: dict[str, dict] = {}

            with ThreadPoolExecutor(max_workers=min(3, len(mids))) as ex:
                details = list(ex.map(fetch_detail, mids))
            for md in details:
                if md == "throttled":
                    throttled = True
                    continue
                if not md:
                    continue
                for pl in md.get("players", []):
                    if pl.get("puuid") == puuid:
                        kills += pl.get("kills", 0) or 0
                        deaths += pl.get("deaths", 0) or 0
                        hits += pl.get("shots_hit", 0) or 0
                        heads += pl.get("headshots", 0) or 0
                        used += 1

                        aname = pl.get("agent")
                        if aname:
                            agent_counts[aname] = agent_counts.get(aname, 0) + 1
                        outcome = pl.get("result") or "Unresolved"
                        form.append(_outcome_token(outcome))
                        mapn = md.get("map") or "Unknown"
                        counts = map_wins.setdefault(
                            mapn, {"wins": 0, "losses": 0, "draws": 0,
                                   "unresolved": 0})
                        key = {"Victory": "wins", "Defeat": "losses",
                               "Draw": "draws"}.get(outcome, "unresolved")
                        counts[key] += 1
                        break
            if used == 0:

                return None, None, rr_earned, ("throttled" if throttled else "empty"), None
            kd = round(kills / deaths, 2) if deaths else float(kills)
            hs = round(heads / hits * 100) if hits else None
            decided = sum(token in ("W", "L") for token in form)
            wins = form.count("W")
            intel = {
                "topAgents": [{"agent": a, "games": n} for a, n in
                              sorted(agent_counts.items(), key=lambda x: -x[1])[:3]],
                "form": form,
                "streak": form_streak(form),
                "mapWins": map_wins,
                "winRate": round(100 * wins / decided) if decided else None,
                "decidedGames": decided,
                "draws": form.count("D"),
                "unresolved": form.count("U"),
                "formScope": "competitive" if is_comp else "recent_available_queue",
            }
            result = (kd, hs, rr_earned, "ok", intel)
            if not throttled and used == len(mids):
                _cache_put(_KD_CACHE, _KD_CACHE_MAX, puuid, (result, tuple(mids), count))
            return result
        except Exception:
            return None, None, None, "error", None

    def _spawn_kd_fill(self, match_id, puuids, season, prev_season) -> None:
        pass
        with _KD_FILL_LOCK:
            if match_id in _KD_FILLING:
                return
            _KD_FILLING.add(match_id)

        def _run():
            try:
                def _fill_one(puuid):
                    cache_key = f"{match_id}:{puuid}"
                    entry = _CACHE.get(cache_key)
                    if entry is None or entry.get("kd_done"):
                        return
                    entry["kd_tries"] = entry.get("kd_tries", 0) + 1
                    kd, hs, _, status, intel = self.kd_hs(puuid, count=5)
                    if kd is None:
                        _log(f"kd-fill {puuid[:8]} status={status} "
                             f"tries={entry['kd_tries']}")
                    if kd is not None:
                        entry["kd"], entry["hs"] = kd, hs
                        entry["intel"] = intel
                        entry["kd_done"] = True
                    elif status == "empty":

                        entry["kd_done"] = True
                    elif status == "throttled":

                        pass
                    elif entry["kd_tries"] >= 6:

                        entry["kd_done"] = True

                def _top_up(puuid):
                    entry = _CACHE.get(f"{match_id}:{puuid}")
                    if entry is None or entry.get("kd_full") or entry.get("kd") is None:
                        return
                    entry["kd_full"] = True
                    mids, is_comp, _ = self._fresh_mids(puuid)
                    rr_key = mids[0] if (mids and is_comp) else "nocomp"
                    hit = _RR_CACHE.get(puuid)
                    if hit and hit[1] == rr_key:
                        entry["rr_earned"] = hit[0]
                    else:
                        cu = self.auth.pd_get(
                            f"/mmr/v1/players/{puuid}/competitiveupdates"
                            f"?startIndex=0&endIndex=1&queue=competitive", retries=1)
                        m = cu.get("Matches", []) if isinstance(cu, dict) else []
                        if m:
                            entry["rr_earned"] = m[0].get("RankedRatingEarned")
                        if isinstance(cu, dict) and not _is_throttled(cu):
                            _cache_put(_RR_CACHE, _KD_CACHE_MAX, puuid,
                                       (entry.get("rr_earned"), rr_key))
                    kd, hs, _, status, intel = self.kd_hs(puuid, count=5)
                    if kd is not None:
                        entry["kd"], entry["hs"] = kd, hs
                        entry["intel"] = intel

                with ThreadPoolExecutor(max_workers=8) as ex:
                    list(ex.map(_fill_one, puuids))
                with ThreadPoolExecutor(max_workers=4) as ex:
                    list(ex.map(_top_up, puuids))
            finally:
                with _KD_FILL_LOCK:
                    _KD_FILLING.discard(match_id)

        threading.Thread(target=_run, daemon=True,
                         name=f"kd-fill-{match_id[:8]}").start()

    def build_scoreboard(self, include_stats=True) -> dict:
        presences = self._presences()
        state = self.game_state(presences)

        if state == "MENUS":

            _LAST_BOARD["board"] = None

            board = dict(self.build_lobby(presences, include_stats=include_stats))
            board["queue"] = self.queue_status()
            return board
        if state not in ("INGAME", "PREGAME"):
            held = self._held_board()
            return held or {"state": state, "stateLabel": STATES.get(state, state),
                            "source": "local", "players": [], "teams": {}, "parties": []}

        current = self._current_players(state)
        if not current:

            held = self._held_board()
            return held or {"state": "MENUS", "stateLabel": STATES["MENUS"],
                            "source": "local", "players": [], "teams": {}, "parties": []}

        raw_players, match_id, map_id, queue = current
        puuids = [p["Subject"] for p in raw_players]

        if match_id not in _MATCH_META:
            _MATCH_META.clear()
            _MATCH_META[match_id] = {}
        meta = _MATCH_META[match_id]

        names = meta.get("names") or {}
        missing_names = [p for p in puuids if p not in names]
        if missing_names and meta.get("name_tries", 0) < 8:
            meta["name_tries"] = meta.get("name_tries", 0) + 1
            names = {**names, **self.reveal_names(missing_names)}
            meta["names"] = names
        if not meta.get("loadouts"):
            ld = self.loadouts(state, match_id)
            if ld:
                meta["loadouts"] = ld
            weapons_by_puuid = ld
        else:
            weapons_by_puuid = meta["loadouts"]

        pmap = self.party_map(puuids, presences)
        party_lookup = {}
        parties_out = []
        for idx, (pid, members) in enumerate(pmap.items()):
            color = party_color(idx)
            parties_out.append({"id": pid, "color": color, "number": idx + 1,
                                "size": len(members), "members": members})
            for m in members:
                party_lookup[m] = {"id": pid, "color": color, "number": idx + 1}

        season = self.season_id()
        prev_season = self.prev_season_id()
        self_team = next((p["TeamID"] for p in raw_players
                          if p["Subject"] == self.self_puuid), "Blue")

        uncached_kd: list[str] = []

        def fetch_player(p):
            puuid = p["Subject"]
            ident = p.get("PlayerIdentity", {}) or {}
            cache_key = f"{match_id}:{puuid}"
            cached = _CACHE.get(cache_key)
            if cached is None:
                rk = self.rank_info(puuid, season, prev_season)

                cached = {"rk": rk, "prev": rk.get("prev", 0),
                          "kd": None, "hs": None, "rr_earned": None,
                          "kd_done": False}
                if not rk.get("ok"):

                    cached["rank_at"] = time.time()
                _CACHE[cache_key] = cached
                if include_stats:
                    uncached_kd.append(puuid)
            else:
                if (not cached["rk"].get("ok")
                        and time.time() - cached.get("rank_at", 0.0) > 20.0):

                    rk = self.rank_info(puuid, season, prev_season)
                    if rk.get("ok"):
                        cached["rk"], cached["prev"] = rk, rk.get("prev", 0)
                    else:
                        cached["rank_at"] = time.time()
                if include_stats and not cached.get("kd_done"):
                    uncached_kd.append(puuid)
            name, level, level_hidden, name_confirmed, name_source = self.resolve_identity(
                puuid, names, ident
            )

            if (level or 0) <= 0:
                recovered = self.level_from_history(puuid)
                if recovered > 0:
                    level = recovered
            return puuid, cached, name, level, level_hidden, name_confirmed, name_source

        with ThreadPoolExecutor(max_workers=min(6, len(raw_players) or 1)) as ex:
            resolved = {r[0]: r[1:] for r in ex.map(fetch_player, raw_players)}

        if uncached_kd:
            self._spawn_kd_fill(match_id, uncached_kd, season, prev_season)

        players = []
        for p in raw_players:
            puuid = p["Subject"]
            ident = p.get("PlayerIdentity", {}) or {}
            cached, name, level, level_hidden, name_confirmed, name_source = resolved[puuid]
            if name == _fallback_name(puuid):

                agent_meta = resolve_agent(p.get("CharacterID", "") or "") or {}
                if state != "PREGAME" and agent_meta.get("name"):
                    name = agent_meta["name"]
                else:
                    name = f"Player {len(players) + 1}"
            rk = cached["rk"]
            weapons = weapons_by_puuid.get(puuid.lower(), [])
            vandal = next((w["skin"] for w in weapons
                           if w["weapon"] == "Vandal" and w.get("skin")), None)
            smurf, smurf_reasons = compute_smurf(
                level=level, peak_tier=rk["peak"], rank_tier=rk["tier"],
                kd=cached["kd"],
                win_rate=((cached.get("intel") or {}).get("winRate")
                          if (cached.get("intel") or {}).get("decidedGames", 0) >= 15
                          else rk.get("winRateAllGames")),
                games=((cached.get("intel") or {}).get("decidedGames")
                       if (cached.get("intel") or {}).get("decidedGames", 0) >= 15
                       else rk["games"]))
            players.append(assemble_player(
                puuid=puuid,
                name=name,
                name_hidden=ident.get("Incognito", False),
                team=p.get("TeamID", "Blue"),
                is_self=(puuid == self.self_puuid),
                agent_id=p.get("CharacterID", ""),
                selection=p.get("CharacterSelectionState") if state == "PREGAME" else None,
                rank_tier=rk["tier"], rr=rk["rr"], leaderboard=rk["lb"],
                peak_tier=rk["peak"], prev_tier=cached["prev"],
                win_rate=rk.get("winRateAllGames"), games=rk["games"],
                kd=cached["kd"], hs=cached["hs"],
                level=level,
                level_hidden=level_hidden,
                party=party_lookup.get(puuid),
                skin=vandal,
                weapons=weapons,
                peak_act=self.act_episode(rk.get("peak_season")),
                rr_earned=cached.get("rr_earned"),
                intel=cached.get("intel"),
                player_card=valapi.player_card(ident.get("PlayerCardID")),
                title=valapi.title_text(ident.get("PlayerTitleID")),
                smurf=smurf, smurf_reasons=smurf_reasons,
                name_confirmed=name_confirmed, name_source=name_source,
                season_win_rate=rk.get("winRateAllGames"),
                season_games=rk.get("games", 0), season_wins=rk.get("wins", 0),
            ))

        map_name = map_name_from_path(map_id)
        score = self.match_score(presences) if state == "INGAME" else None
        if score and (queue or "").lower() in ("deathmatch", "hurm"):
            score["round"] = None
        board = finalize(players, state=state, source="local", self_team=self_team,
                         map_name=map_name, queue=queue, match_id=match_id,
                         parties=parties_out, map_splash=valapi.map_splash(map_name),
                         score=score)
        board["riotRequests"] = self.auth.req_count
        board["partyGroups"] = knowledge_store.party_groups(players, parties_out)

        _LAST_BOARD["board"] = board
        _LAST_BOARD["at"] = time.time()
        return board

    def _held_board(self):
        pass
        b = _LAST_BOARD.get("board")
        if b and (time.time() - _LAST_BOARD.get("at", 0.0)) < _HOLD_SECS:
            return b
        return None

    def queue_status(self) -> dict:
        pass
        now = time.time()
        if _QUEUE_CACHE["data"] is not None and now - _QUEUE_CACHE["at"] < 3.0:
            return _QUEUE_CACHE["data"]
        from riot_client import party_snapshot
        try:
            snap = party_snapshot(self.auth)
        except Exception:
            snap = {"available": False}
        if snap.get("throttled") and _QUEUE_CACHE["data"]:
            return _QUEUE_CACHE["data"]
        snap.pop("throttled", None)
        _QUEUE_CACHE.update(at=now, data=snap)
        return snap

    def diagnose_reveal(self, max_players=2, max_matches=8) -> dict:
        pass
        presences = self._presences()
        state = self.game_state(presences)
        current = self._current_players(state)
        if not current:
            return {"state": state, "error": "Not in a pre-game/in-game match."}
        raw_players, _, _, _ = current
        puuids = [p["Subject"] for p in raw_players]
        names = self.reveal_names(puuids)

        targets = [p["Subject"] for p in raw_players
                   if (p.get("PlayerIdentity", {}) or {}).get("Incognito")
                   and p["Subject"] != self.self_puuid]

        report = []
        for puuid in targets[:max_players]:
            entry = {"puuid": puuid[:8], "nameService": names.get(puuid), "matches": []}
            try:
                hist = self.auth.pd_get(
                    f"/match-history/v1/history/{puuid}?startIndex=0&endIndex={max_matches}")
                for m in (hist.get("History") or [])[:max_matches]:
                    mid = m.get("MatchID")
                    if not mid:
                        continue
                    md = self.auth.pd_get(f"/match-details/v1/matches/{mid}")
                    pl = next((x for x in (md.get("players") or [])
                               if x.get("subject") == puuid), None)
                    gn = (pl or {}).get("gameName") or ""
                    entry["matches"].append({
                        "queue": m.get("QueueID") or "?",
                        "namePresent": bool(gn.strip()),
                        "name": (f"{gn}#{(pl or {}).get('tagLine', '')}" if gn.strip() else None),
                        "level": (pl or {}).get("accountLevel"),
                    })
            except Exception as e:
                entry["error"] = str(e)
            entry["nameEverPresent"] = any(x["namePresent"] for x in entry["matches"])
            report.append(entry)

        if not targets:
            verdict = "no Incognito players in this match to test"
        elif any(e["nameEverPresent"] for e in report):
            verdict = "baked per-match — deeper history search CAN reveal names"
        else:
            verdict = ("dynamic on current status — match history canNOT reveal names; "
                       "account-v1 (RIOT_API_KEY) is the only path")
        return {"state": state, "incognitoCount": len(targets),
                "verdict": verdict, "report": report}

    def build_lobby(self, presences, include_stats=False) -> dict:
        pass
        members = self.party_members(presences)
        puuids = [m["puuid"] for m in members]

        key = tuple(sorted(puuids))
        now = time.time()
        if (_LOBBY_CACHE["board"] is not None and _LOBBY_CACHE["key"] == key
                and now - _LOBBY_CACHE["at"] < 20):
            return _LOBBY_CACHE["board"]

        names = self.reveal_names(puuids)

        season = self.season_id()
        prev_season = self.prev_season_id()
        multi = len(members) > 1
        party = {"id": "lobby", "color": party_color(0), "number": 1,
                 "size": len(members)} if multi else None

        def fetch_member(m):
            puuid = m["puuid"]
            rk = self.rank_info(puuid, season, prev_season)
            kd = hs = intel = None
            if include_stats:
                kd, hs, _, _, intel = self.kd_hs(puuid, count=5)

            level = m.get("level", 0) or 0
            if level <= 0:
                level = self.level_from_history(puuid)
            return m, rk, kd, hs, level, intel

        with ThreadPoolExecutor(max_workers=min(6, len(members) or 1)) as ex:
            fetched = list(ex.map(fetch_member, members))

        players = []
        for m, rk, kd, hs, lvl, intel in fetched:
            puuid = m["puuid"]
            ident = {"AccountLevel": lvl, "HideAccountLevel": False,
                     "Incognito": m.get("incognito", False)}
            name, level, level_hidden, name_confirmed, name_source = self.resolve_identity(
                puuid, names, ident
            )
            smurf, smurf_reasons = compute_smurf(
                level=level, peak_tier=rk["peak"], rank_tier=rk["tier"],
                kd=kd,
                win_rate=(intel.get("winRate") if intel and intel.get("decidedGames", 0) >= 15
                          else rk.get("winRateAllGames")),
                games=(intel.get("decidedGames") if intel and intel.get("decidedGames", 0) >= 15
                       else rk["games"]))
            players.append(assemble_player(
                puuid=puuid, name=name, name_hidden=False, team="Blue",
                is_self=(puuid == self.self_puuid), agent_id="",
                rank_tier=rk["tier"], rr=rk["rr"], leaderboard=rk["lb"],
                peak_tier=rk["peak"], prev_tier=rk.get("prev", 0),
                win_rate=rk.get("winRateAllGames"), games=rk["games"], kd=kd, hs=hs,
                intel=intel,
                level=level, level_hidden=level_hidden,
                party=party, peak_act=self.act_episode(rk.get("peak_season")),
                smurf=smurf, smurf_reasons=smurf_reasons,
                name_confirmed=name_confirmed, name_source=name_source,
                season_win_rate=rk.get("winRateAllGames"),
                season_games=rk.get("games", 0), season_wins=rk.get("wins", 0),
            ))

        parties_out = [{**party, "members": puuids}] if party else []
        board = finalize(players, state="MENUS", source="local", self_team="Blue",
                         map_name=None, queue="Lobby", match_id="lobby",
                         parties=parties_out)
        board["riotRequests"] = self.auth.req_count
        board["partyGroups"] = knowledge_store.party_groups(players, parties_out)
        _LOBBY_CACHE.update(key=key, at=now, board=board)
        return board

    def ingest_match(self, match_id: str, subject: str | None = None, *,
                     local_owner: bool = False,
                     provenance: str = "match_detail") -> dict | None:
        stored = knowledge_store.get_match(match_id, subject)
        if stored and stored.get("complete"):
            return stored
        md = self.auth.pd_get(f"/match-details/v1/matches/{match_id}")
        if not isinstance(md, dict) or "players" not in md:
            return stored
        info = md.get("matchInfo") or {}
        teams = {team.get("teamId"): team for team in md.get("teams") or []
                 if team.get("teamId")}
        rounds = sum(int(team.get("roundsWon") or 0) for team in teams.values()) \
            or len(md.get("roundResults") or []) or 1
        hits, heads = {}, {}
        for round_result in md.get("roundResults") or []:
            for player_stats in round_result.get("playerStats") or []:
                player_id = player_stats.get("subject")
                for damage in player_stats.get("damage") or []:
                    hits[player_id] = hits.get(player_id, 0) + sum(
                        int(damage.get(key) or 0)
                        for key in ("legshots", "bodyshots", "headshots")
                    )
                    heads[player_id] = heads.get(player_id, 0) + int(
                        damage.get("headshots") or 0
                    )
        raw_players = md.get("players") or []
        names = self.reveal_names([player.get("subject") for player in raw_players])
        players = []
        for raw in raw_players:
            puuid = raw.get("subject")
            if not puuid:
                continue
            stats = raw.get("stats") or {}
            identity = raw.get("PlayerIdentity") or raw.get("playerIdentity") or {}
            game_name = str(raw.get("gameName") or "").strip()
            tag_line = str(raw.get("tagLine") or "").strip()
            embedded = f"{game_name}#{tag_line}" if game_name and tag_line else game_name or None
            confirmed_name = names.get(puuid) or embedded
            if embedded and puuid not in names:
                knowledge_store.upsert_player(
                    puuid, riot_id=embedded, confirmed=True, source="match_detail"
                )
            agent = resolve_agent(raw.get("characterId") or "") or {}
            total_hits = hits.get(puuid, 0)
            kills, deaths = int(stats.get("kills") or 0), int(stats.get("deaths") or 0)
            result = match_outcome(teams, raw.get("teamId"))
            players.append({
                "puuid": puuid,
                "name": confirmed_name or _fallback_name(puuid),
                "nameConfirmed": bool(confirmed_name),
                "team": raw.get("teamId"),
                "agent": agent.get("name", "Unknown"),
                "partyId": raw.get("partyId"),
                "kills": kills,
                "deaths": deaths,
                "assists": int(stats.get("assists") or 0),
                "kd": round(kills / deaths, 2) if deaths else float(kills),
                "acs": round(int(stats.get("score") or 0) / rounds),
                "hsPct": round(100 * heads.get(puuid, 0) / total_hits) if total_hits else None,
                "shotsHit": total_hits,
                "headshots": heads.get(puuid, 0),
                "level": raw.get("accountLevel") or identity.get("AccountLevel") or 0,
                "playerCard": valapi.player_card(
                    identity.get("PlayerCardID") or raw.get("playerCard") or raw.get("playerCardId")
                ),
                "result": result or "Unresolved",
                "resultExact": result is not None,
            })
        payload = {
            "matchId": match_id,
            "startedAt": int((info.get("gameStartMillis") or 0) / 1000) or None,
            "map": map_name_from_path(info.get("mapId") or ""),
            "mode": _mode_label(info.get("queueID") or info.get("queueId") or ""),
            "scores": {team_id: int(team.get("roundsWon") or 0)
                       for team_id, team in teams.items()},
            "players": players,
        }
        owner = self.self_puuid if local_owner else None
        knowledge_store.save_match(payload, owner=owner, provenance=provenance)
        return knowledge_store.get_match(match_id, subject)

    def player_career(self, puuid: str, count: int = 8) -> dict:
        pass
        try:
            hist = self.auth.pd_get(
                f"/match-history/v1/history/{puuid}?startIndex=0&endIndex={count}")
            entries = hist.get("History", []) or [] if isinstance(hist, dict) else []
        except Exception:
            entries = []
        mids = [h["MatchID"] for h in entries if h.get("MatchID")]

        def fetch_detail(mid):
            try:
                stored = knowledge_store.career_match(mid, puuid)
                if stored:
                    return stored
                self.ingest_match(
                    mid,
                    puuid,
                    local_owner=puuid == self.self_puuid,
                    provenance="local_history" if puuid == self.self_puuid else "profile_history",
                )
                return knowledge_store.career_match(mid, puuid)
            except Exception:
                return None

        matches, mate_puuids = [], set()
        if mids:
            with ThreadPoolExecutor(max_workers=min(4, len(mids))) as ex:
                for row in ex.map(fetch_detail, mids):
                    if row:
                        matches.append(row)
                mate_puuids.update(m["puuid"] for m in row["teammates"] if m.get("puuid"))

        names = self.reveal_names(list(mate_puuids)) if mate_puuids else {}
        for row in matches:
            for mate in row["teammates"]:
                if names.get(mate["puuid"]):
                    mate["name"] = names[mate["puuid"]]
                    mate["nameConfirmed"] = True

        updates = {}
        if any((row.get("mode") or "").lower() == "competitive" for row in matches):
            try:
                cu = self.auth.pd_get(
                    f"/mmr/v1/players/{puuid}/competitiveupdates"
                    f"?startIndex=0&endIndex={min(20, max(10, count))}&queue=competitive")
                for update in (cu or {}).get("Matches", []) or []:
                    if update.get("MatchID"):
                        updates[update["MatchID"]] = update
            except Exception:
                updates = {}
        for row in matches:
            update = updates.get(row.get("matchId"))
            if not update:
                continue
            tier = update.get("TierAfterUpdate")
            rank = rank_from_tier(tier or 0)
            row.update({
                "rrDelta": update.get("RankedRatingEarned"),
                "tierAfter": tier,
                "rrAfter": update.get("RankedRatingAfterUpdate"),
                "rankAfter": rank.get("name"),
                "rankColor": rank.get("color"),
                "rankIcon": valapi.rank_icon(tier or 0) if tier else None,
            })

        return {"source": "local", "puuid": puuid, "matches": matches,
                **_career_summary(matches)}

    def player_profile(self, puuid: str, count: int = 8) -> dict:
        career = self.player_career(puuid, count=count)
        season = self.season_id()
        previous_season = self.prev_season_id()
        rank = self.rank_info(puuid, season, previous_season)
        current = rank_from_tier(rank.get("tier") or 0)
        peak = rank_from_tier(rank.get("peak") or 0)
        previous = rank_from_tier(rank.get("prev") or 0)
        names = self.reveal_names([puuid])
        confirmed_id = names.get(puuid) or self.reveal_via_account_api(puuid)
        riot_id = confirmed_id or _fallback_name(puuid)
        averages = career.get("averages") or {}
        profile = {
            **career,
            "riotId": riot_id,
            "level": self.level_from_history(puuid),
            "currentRank": current["name"],
            "rankTier": current["tier"],
            "rankColor": current["color"],
            "rankIcon": valapi.rank_icon(current["tier"]),
            "rr": rank.get("rr") or 0,
            "leaderboard": rank.get("lb") or 0,
            "peakRank": peak["name"],
            "peakTier": peak["tier"],
            "peakColor": peak["color"],
            "peakIcon": valapi.rank_icon(peak["tier"]),
            "peakAct": self.act_episode(rank.get("peak_season")),
            "previousRank": previous["name"],
            "previousTier": previous["tier"],
            "previousColor": previous["color"],
            "previousIcon": valapi.rank_icon(previous["tier"]),
            "winRate": averages.get("winRate"),
            "winRateBasis": "career_decided_matches",
            "seasonGames": rank.get("games") or 0,
            "seasonWins": rank.get("wins") or 0,
            "winRateAllGames": rank.get("winRateAllGames"),
        }
        knowledge_store.upsert_player(
            puuid, riot_id=confirmed_id, confirmed=bool(confirmed_id), source="profile",
            fields={
                "rankTier": current["tier"], "rr": rank.get("rr") or 0,
                "peakTier": peak["tier"], "previousTier": previous["tier"],
                "level": profile["level"], "kd": averages.get("kd"),
                "hsPct": averages.get("hsPct"), "winRate": averages.get("winRate"),
            },
        )
        return profile

    def match_detail(self, match_id: str, subject: str | None = None) -> dict:
        detail = self.ingest_match(
            match_id,
            subject,
            local_owner=subject == self.self_puuid,
            provenance="local_history" if subject == self.self_puuid else "match_detail",
        )
        if not detail:
            return {"error": "Match details unavailable."}
        season = self.season_id()
        prev_season = self.prev_season_id()
        for player in detail.get("players") or []:
            rank = self.rank_info(player.get("puuid"), season, prev_season)
            rank_meta = rank_from_tier(rank.get("tier") or 0)
            peak_meta = rank_from_tier(rank.get("peak") or 0)
            player.update({
                "rankTier": rank_meta["tier"], "rank": rank_meta["name"],
                "rankColor": rank_meta["color"],
                "rankIcon": valapi.rank_icon(rank_meta["tier"]),
                "rr": rank.get("rr") or 0, "leaderboard": rank.get("lb") or 0,
                "peakRankTier": peak_meta["tier"], "peakRank": peak_meta["name"],
                "peakColor": peak_meta["color"],
                "peakIcon": valapi.rank_icon(peak_meta["tier"]),
            })
            knowledge_store.upsert_player(player.get("puuid"), source="match_rank",
                                          fields=player)
        players = detail.get("players") or []
        subject_team = next((player.get("team") for player in players
                             if player.get("isSubject")), None)
        if players:
            players[0]["isMatchMvp"] = True
        team_mvp = next((player for player in players
                         if player.get("team") == subject_team), None)
        if team_mvp:
            team_mvp["isTeamMvp"] = True
        team_stats = {}
        for team_id in set(player.get("team") for player in players if player.get("team")):
            team_players = [player for player in players if player.get("team") == team_id]
            rated = [player.get("rankTier") for player in team_players
                     if (player.get("rankTier") or 0) > 0]
            avg_tier = round(sum(rated) / len(rated)) if rated else 0
            avg_rank = rank_from_tier(avg_tier)
            team_stats[team_id] = {"avgRankTier": avg_tier, "avgRank": avg_rank["name"],
                                   "avgRankColor": avg_rank["color"],
                                   "rankIcon": valapi.rank_icon(avg_tier) if avg_tier else None}
        detail.update(mapSplash=valapi.map_splash(detail.get("map")),
                      teamStats=team_stats, source="local", live=True)
        knowledge_store.save_match(
            detail,
            owner=self.self_puuid if subject == self.self_puuid else None,
            provenance="local_history" if subject == self.self_puuid else "match_detail",
        )
        return detail

def _career_summary(matches: list) -> dict:
    n = len(matches)
    if not n:
        return {"averages": {"games": 0, "decidedGames": 0, "wins": 0,
                             "losses": 0, "draws": 0, "unresolved": 0,
                             "winRate": None, "kd": 0, "kills": 0,
                             "deaths": 0, "assists": 0, "hsPct": 0},
                "coPlayers": [], "agentPool": [], "mapStats": []}
    wins = sum(1 for m in matches if m.get("result") == "Victory")
    losses = sum(1 for m in matches if m.get("result") == "Defeat")
    draws = sum(1 for m in matches if m.get("result") == "Draw")
    unresolved = n - wins - losses - draws
    decided = wins + losses
    k = sum(m["kills"] for m in matches)
    d = sum(m["deaths"] for m in matches)
    a = sum(m["assists"] for m in matches)
    hs = [m["hsPct"] for m in matches if m.get("hsPct") is not None]

    seen: dict[str, dict] = {}
    for m in matches:
        for mate in m["teammates"]:
            pid = mate.get("puuid")
            if not pid:
                continue
            e = seen.setdefault(pid, {"puuid": pid, "name": mate.get("name"),
                                      "sharedMatches": 0, "agents": set()})
            e["sharedMatches"] += 1
            e["name"] = mate.get("name") or e["name"]
            if mate.get("agent"):
                e["agents"].add(mate["agent"])
    co_players = sorted(
        ({"puuid": e["puuid"], "name": e["name"], "sharedMatches": e["sharedMatches"],
          "agents": sorted(e["agents"]), "isParty": e["sharedMatches"] >= 2}
         for e in seen.values()),
        key=lambda x: x["sharedMatches"], reverse=True)[:6]

    def _tally(key):
        out: dict[str, dict] = {}
        for m in matches:
            name = m.get(key)
            if not name or name == "Unknown":
                continue
            tally = out.setdefault(name, {"games": 0, "wins": 0, "losses": 0,
                                          "draws": 0, "unresolved": 0})
            tally["games"] += 1
            outcome_key = {"Victory": "wins", "Defeat": "losses",
                           "Draw": "draws"}.get(m.get("result"), "unresolved")
            tally[outcome_key] += 1
        return out

    def _row(label: str, name: str, tally: dict) -> dict:
        decisive = tally["wins"] + tally["losses"]
        return {
            label: name,
            **tally,
            "decidedGames": decisive,
            "winRate": round(100 * tally["wins"] / decisive) if decisive else None,
        }

    agent_pool = [
        {**_row("agent", agent, tally),
         "portrait": (resolve_agent(agent) or {}).get("portrait"),
         "color": (resolve_agent(agent) or {}).get("color", "#8B978F")}
        for agent, tally in sorted(_tally("agent").items(),
                                   key=lambda item: -item[1]["games"])
    ][:5]
    map_stats = [
        _row("map", map_name, tally)
        for map_name, tally in sorted(_tally("map").items(),
                                      key=lambda item: -item[1]["games"])
    ]

    return {
        "averages": {
            "games": n, "decidedGames": decided, "wins": wins,
            "losses": losses, "draws": draws, "unresolved": unresolved,
            "winRate": round(100 * wins / decided) if decided else None,
            "kills": round(k / n, 1), "deaths": round(d / n, 1), "assists": round(a / n, 1),
            "kd": round(k / d, 2) if d else float(k),
            "hsPct": round(sum(hs) / len(hs)) if hs else None,
        },
        "coPlayers": co_players,
        "agentPool": agent_pool,
        "mapStats": map_stats,
    }

def _team_stats(team_players: list) -> dict:
    pass
    ranked = [p["rankTier"] for p in team_players if (p.get("rankTier") or 0) > 0]
    kds = [p["kd"] for p in team_players if p.get("kd") is not None]
    wrs = [p["winRate"] for p in team_players if p.get("winRate") is not None]
    avg_tier = sum(ranked) / len(ranked) if ranked else 0
    rank_meta = rank_from_tier(round(avg_tier)) if ranked else rank_from_tier(0)
    return {
        "avgRankTier": round(avg_tier, 2),
        "avgRank": rank_meta["name"],
        "rankColor": rank_meta["color"],
        "rankIcon": valapi.rank_icon(round(avg_tier)) if ranked else None,
        "avgKd": round(sum(kds) / len(kds), 2) if kds else None,
        "avgWinRate": round(sum(wrs) / len(wrs)) if wrs else None,
        "smurfCount": sum(1 for p in team_players if p.get("smurf")),
        "size": len(team_players),
    }

def _win_prob(self_stats: dict, enemy_stats: dict) -> int:
    pass
    prob = 50.0
    prob += (self_stats["avgRankTier"] - enemy_stats["avgRankTier"]) * 5
    self_kd = self_stats["avgKd"]
    enemy_kd = enemy_stats["avgKd"]
    if self_kd is not None and enemy_kd is not None:
        prob += (self_kd - enemy_kd) * 20
    return max(5, min(95, round(prob)))

def finalize(players, *, state, source, self_team, map_name, queue, match_id,
             parties, map_splash=None, score=None):
    pass

    for p in players:
        mw = p.pop("mapWins", None) or {}
        counts = (mw.get(map_name) or {}) if map_name else {}
        if isinstance(counts, list):
            counts = {"wins": counts[0], "losses": max(0, counts[1] - counts[0]),
                      "draws": 0, "unresolved": 0}
        wins = int(counts.get("wins") or 0)
        losses = int(counts.get("losses") or 0)
        draws = int(counts.get("draws") or 0)
        unresolved = int(counts.get("unresolved") or 0)
        decided = wins + losses
        games = decided + draws + unresolved
        p["mapWinRate"] = ({"winRate": round(100 * wins / decided)
                            if decided else None, "games": games,
                            "decidedGames": decided, "wins": wins,
                            "losses": losses, "draws": draws,
                            "unresolved": unresolved} if games else None)
    players.sort(key=lambda x: (x["team"] != self_team, -x["rankTier"], -(x["level"] or 0)))
    teams = {}
    for p in players:
        teams.setdefault(p["team"], []).append(p)

    team_stats = {tid: _team_stats(tp) for tid, tp in teams.items()}

    win_prob = None
    if state == "INGAME" and len(team_stats) == 2 and self_team in team_stats:
        enemy_team = next(t for t in team_stats if t != self_team)
        win_prob = _win_prob(team_stats[self_team], team_stats[enemy_team])

    locked = sum(1 for p in players if p.get("selection") == "locked")

    side = ({"Red": "Attacker", "Blue": "Defender"}.get(self_team)
            if state in ("INGAME", "PREGAME") else None)
    return {
        "state": state,
        "stateLabel": STATES.get(state, state),
        "source": source,
        "map": map_name,
        "mapSplash": map_splash,
        "mode": _mode_label(queue),
        "matchId": match_id,
        "selfTeam": self_team,
        "side": side,
        "players": players,
        "teams": teams,
        "teamStats": team_stats,
        "winProb": win_prob,
        "parties": parties,
        "score": score,
        "lockProgress": {"locked": locked, "total": len(players)} if state == "PREGAME" else None,
    }
