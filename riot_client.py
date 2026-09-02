from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime

import requests
import urllib3

from vconstants import GAMEMODES


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_CLIENT_VERSION: str | None = None

REGION = "eu"


def _log(msg: str) -> None:
    if os.getenv("TRACKER_QUIET"):
        return
    print(f"[riot_client] {msg}", flush=True)


_RIOT_RATE_LOCK = threading.Lock()
try:
    _RIOT_MAX_RPS = max(0.0, float(os.getenv("RIOT_MAX_RPS", "10")))
except ValueError:
    _RIOT_MAX_RPS = 10.0
_RIOT_BURST = _RIOT_MAX_RPS if _RIOT_MAX_RPS > 0 else 1.0
_RIOT_BUCKET = {"tokens": _RIOT_BURST, "at": 0.0}

_MMR_BURST = 24.0
_MMR_RPS = 0.4
_MMR_BUCKET = {"tokens": _MMR_BURST, "at": 0.0}
_HOLD_UNTIL = {"mmr": 0.0, "other": 0.0}


def _family(endpoint: str) -> str:
    return "mmr" if endpoint.startswith("/mmr/") else "other"


def held_secs(endpoint: str) -> float:
    with _RIOT_RATE_LOCK:
        return max(0.0, _HOLD_UNTIL[_family(endpoint)] - time.time())


def _set_hold(endpoint: str, seconds: float) -> None:
    family = _family(endpoint)
    with _RIOT_RATE_LOCK:
        _HOLD_UNTIL[family] = max(_HOLD_UNTIL[family], time.time() + seconds)


def _take_token(bucket: dict, burst: float, rps: float) -> None:
    while True:
        with _RIOT_RATE_LOCK:
            now = time.time()
            if bucket["at"] == 0.0:
                bucket["at"] = now
            bucket["tokens"] = min(
                burst, bucket["tokens"] + (now - bucket["at"]) * rps
            )
            bucket["at"] = now
            if bucket["tokens"] >= 1.0:
                bucket["tokens"] -= 1.0
                return
            wait = (1.0 - bucket["tokens"]) / rps
        time.sleep(wait)


def _riot_throttle(endpoint: str = "") -> None:
    if _RIOT_MAX_RPS <= 0:
        return
    while True:
        wait = held_secs(endpoint)
        if wait <= 0:
            break
        time.sleep(wait)
    if _family(endpoint) == "mmr":
        _take_token(_MMR_BUCKET, _MMR_BURST, _MMR_RPS)
    _take_token(_RIOT_BUCKET, _RIOT_BURST, _RIOT_MAX_RPS)


class ClientNotReady(Exception):
    pass


class LocalAuth:
    def __init__(self):
        self.lockfile = self._get_lockfile()
        detected = self._get_region()
        if detected[0] != REGION:
            raise ClientNotReady(
                f"VALORANT Tracker Core is EU-only; Riot client shard is {detected[0]}"
            )
        self.region = [REGION, ["eu-1", REGION]]
        self.pd_url = f"https://pd.{self.region[0]}.a.pvp.net"
        self.glz_url = (
            f"https://glz-{self.region[1][0]}.{self.region[1][1]}.a.pvp.net"
        )
        self.shard = self.region[0]
        self._headers: dict | None = None
        self.puuid = ""
        self.req_count = 0

    @staticmethod
    def available() -> bool:
        path = os.path.join(
            os.getenv("LOCALAPPDATA", ""), r"Riot Games\Riot Client\Config\lockfile"
        )
        return os.path.isfile(path)

    @staticmethod
    def _get_lockfile() -> dict:
        path = os.path.join(
            os.getenv("LOCALAPPDATA", ""), r"Riot Games\Riot Client\Config\lockfile"
        )
        with open(path, encoding="utf-8") as stream:
            keys = ["name", "PID", "port", "password", "protocol"]
            return dict(zip(keys, stream.read().split(":")))

    @staticmethod
    def _get_region():
        path = os.path.join(
            os.getenv("LOCALAPPDATA", ""), r"VALORANT\Saved\Logs\ShooterGame.log"
        )
        pd_region = glz_region = None
        with open(path, "r", encoding="utf8") as stream:
            for line in stream:
                if ".a.pvp.net/account-xp/v1/" in line:
                    pd_region = line.split(".a.pvp.net/account-xp/v1/")[0].split(".")[-1]
                elif "https://glz" in line:
                    glz_region = [
                        line.split("https://glz-")[1].split(".")[0],
                        line.split("https://glz-")[1].split(".")[1],
                    ]
                if pd_region and glz_region:
                    if pd_region == "pbe":
                        return ["na", ["na-1", "na"]]
                    return [pd_region, glz_region]
        raise ClientNotReady("could not detect region from ShooterGame.log")

    def _local_headers(self) -> dict:
        encoded = base64.b64encode(
            ("riot:" + self.lockfile["password"]).encode()
        ).decode()
        return {"Authorization": "Basic " + encoded}

    def _client_version(self) -> str:
        global _CLIENT_VERSION
        if _CLIENT_VERSION:
            return _CLIENT_VERSION
        try:
            data = requests.get(
                f"https://127.0.0.1:{self.lockfile['port']}/chat/v4/presences",
                headers=self._local_headers(),
                verify=False,
                timeout=5,
            ).json()
            for presence in (data or {}).get("presences", []) or []:
                if presence.get("product") != "valorant" or not presence.get("private"):
                    continue
                try:
                    private = json.loads(
                        base64.b64decode(str(presence["private"])).decode("utf-8")
                    )
                except Exception:
                    continue
                version = (
                    (private.get("partyPresenceData") or {}).get("partyClientVersion")
                    or private.get("partyClientVersion")
                )
                if version:
                    _CLIENT_VERSION = version
                    return version
        except Exception as exc:
            _log(f"local client-version lookup failed: {exc}")
        try:
            response = requests.get("https://valorant-api.com/v1/version", timeout=6)
            response.raise_for_status()
            version = (response.json().get("data") or {}).get("riotClientVersion")
            if version:
                _CLIENT_VERSION = version
                return version
        except Exception as exc:
            _log(f"valorant-api client-version lookup failed: {exc}")
        try:
            path = os.path.join(
                os.getenv("LOCALAPPDATA", ""), r"VALORANT\Saved\Logs\ShooterGame.log"
            )
            with open(path, "r", encoding="utf8") as stream:
                for line in stream:
                    if "CI server version:" in line:
                        _CLIENT_VERSION = line.split("CI server version: ", 1)[1].strip()
                        if _CLIENT_VERSION:
                            return _CLIENT_VERSION
        except Exception:
            pass
        raise ClientNotReady("VALORANT client version is unavailable")

    def headers(self, refresh: bool = False) -> dict:
        if self._headers and not refresh:
            return self._headers
        response = requests.get(
            f"https://127.0.0.1:{self.lockfile['port']}/entitlements/v1/token",
            headers=self._local_headers(),
            verify=False,
            timeout=5,
        )
        try:
            entitlements = response.json()
        except ValueError:
            entitlements = None
        required = ("subject", "accessToken", "token")
        if not isinstance(entitlements, dict) or not all(
            key in entitlements for key in required
        ):
            raise ClientNotReady(
                f"entitlements not ready (HTTP {response.status_code})"
            )
        self.puuid = entitlements["subject"]
        self._headers = {
            "Authorization": f"Bearer {entitlements['accessToken']}",
            "X-Riot-Entitlements-JWT": entitlements["token"],
            "X-Riot-ClientPlatform": (
                "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjog"
                "IldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5"
                "MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVua25vd24iDQp9"
            ),
            "X-Riot-ClientVersion": self._client_version(),
            "User-Agent": "ShooterGame/13 Windows/10.0.19043.1.256.64bit",
        }
        return self._headers

    @staticmethod
    def _json(response: requests.Response):
        try:
            return response.json()
        except ValueError:
            if response.status_code == 429:
                return {"errorCode": "RATE_LIMITED", "status": 429}
            return {}

    def glz_get(self, endpoint: str) -> dict:
        _riot_throttle(endpoint)
        self.req_count += 1
        response = requests.get(
            self.glz_url + endpoint, headers=self.headers(), timeout=8
        )
        return self._json(response)

    def pd_get(self, endpoint: str, refresh: bool = False, retries: int = 0) -> dict:
        backoff = 3.0
        for attempt in range(retries + 1):
            _riot_throttle(endpoint)
            self.req_count += 1
            response = requests.get(
                self.pd_url + endpoint, headers=self.headers(refresh), timeout=8
            )
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    retry_after = 0.0
                _set_hold(endpoint, retry_after or backoff)
                if attempt < retries:
                    backoff += 3.0
                    continue
                return {"errorCode": "RATE_LIMITED", "status": 429}
            return self._json(response)
        return {"errorCode": "RATE_LIMITED", "status": 429}

    def name_service(self, puuids: list[str], refresh: bool = False) -> list[dict] | dict:
        endpoint = "/name-service/v2/players"
        _riot_throttle(endpoint)
        self.req_count += 1
        response = requests.put(
            self.pd_url + endpoint,
            headers=self.headers(refresh),
            json=puuids,
            timeout=8,
        )
        return self._json(response)

    def local_get(self, endpoint: str) -> dict:
        return requests.get(
            f"https://127.0.0.1:{self.lockfile['port']}{endpoint}",
            headers=self._local_headers(),
            verify=False,
            timeout=5,
        ).json()


def chat_presences(auth: LocalAuth) -> list[dict]:
    data = auth.local_get("/chat/v4/presences")
    return [
        dict(presence)
        for presence in ((data or {}).get("presences", []) or [])
        if isinstance(presence, dict)
    ]


def _iso_to_epoch(value: str | None) -> float | None:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.year >= 2000 else None
    except Exception:
        return None


_PARTY_LOGGED = False
_QUEUE_STARTED: float | None = None


def _self_presence_private(auth: LocalAuth) -> dict | None:
    try:
        presences = chat_presences(auth)
    except Exception:
        return None
    for presence in presences:
        if presence.get("puuid") != auth.puuid or not presence.get("private"):
            continue
        if presence.get("product") not in (None, "valorant"):
            continue
        try:
            private = json.loads(
                base64.b64decode(str(presence["private"])).decode("utf-8")
            )
            return private if isinstance(private, dict) else None
        except Exception:
            return None
    return None


def party_snapshot(auth: LocalAuth) -> dict:
    global _PARTY_LOGGED, _QUEUE_STARTED
    private = _self_presence_private(auth)
    if not private:
        return {"available": False}
    presence = private.get("partyPresenceData") or {}
    party_id = presence.get("partyId") or private.get("partyId")
    if not party_id:
        return {"available": False}

    def label(queue_id: str) -> str:
        return GAMEMODES.get(queue_id, queue_id.replace("_", " ").title())

    state = presence.get("partyState") or "DEFAULT"
    queue_id = (
        private.get("queueId")
        or (private.get("matchPresenceData") or {}).get("queueId")
        or ""
    ).lower()
    snapshot = {
        "available": True,
        "partyId": party_id,
        "queueId": queue_id or None,
        "queueName": label(queue_id) if queue_id else None,
        "eligible": [],
        "state": state,
        "inQueue": "MATCHMAKING" in state,
        "queuedAt": None,
        "partySize": presence.get("partySize") or private.get("partySize") or 1,
        "isOwner": bool(presence.get("isPartyOwner", True)),
        "allReady": True,
    }

    party = auth.glz_get(f"/parties/v1/parties/{party_id}")
    if isinstance(party, dict) and party.get("Members"):
        if not _PARTY_LOGGED:
            _PARTY_LOGGED = True
            _log(f"party payload keys: {sorted(party.keys())}")
        members = party.get("Members") or []
        mine = next(
            (member for member in members if member.get("Subject") == auth.puuid), {}
        )
        glz_queue_id = (
            (party.get("MatchmakingData") or {}).get("QueueID") or ""
        ).lower()
        if glz_queue_id:
            snapshot["queueId"] = glz_queue_id
            snapshot["queueName"] = label(glz_queue_id)
        if party.get("State"):
            snapshot["state"] = party["State"]
            snapshot["inQueue"] = "MATCHMAKING" in party["State"]
        snapshot["eligible"] = [
            {"id": queue, "name": label(queue)}
            for queue in (party.get("EligibleQueues") or [])
        ]
        snapshot["queuedAt"] = _iso_to_epoch(party.get("QueueEntryTime"))
        snapshot["partySize"] = len(members)
        if "IsOwner" in mine:
            snapshot["isOwner"] = bool(mine.get("IsOwner"))
        snapshot["allReady"] = all(
            bool(member.get("IsReady", True)) for member in members
        )
    elif isinstance(party, dict) and party.get("status") == 429:
        snapshot["throttled"] = True

    if snapshot["inQueue"]:
        now = time.time()
        queued_at = snapshot["queuedAt"]
        if queued_at and queued_at <= now:
            _QUEUE_STARTED = queued_at
        elif _QUEUE_STARTED is None:
            _QUEUE_STARTED = now
        snapshot["queuedAt"] = _QUEUE_STARTED
        snapshot["queueElapsed"] = max(0, round(now - _QUEUE_STARTED, 1))
    else:
        _QUEUE_STARTED = None
        snapshot["queuedAt"] = None
    return snapshot


def read_party_state() -> dict:
    if not LocalAuth.available():
        return {
            "available": False,
            "state": "OFFLINE",
            "inQueue": False,
            "message": "VALORANT is not running.",
        }
    try:
        auth = LocalAuth()
        auth.headers()
        return party_snapshot(auth)
    except Exception as exc:
        return {
            "available": False,
            "state": "OFFLINE",
            "inQueue": False,
            "message": str(exc),
        }
