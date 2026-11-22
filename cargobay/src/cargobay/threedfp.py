"""The only code that talks to 3dfp. Tracks the monthly request quota in the Store."""

import asyncio
import json
import time
from datetime import datetime, timezone

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=8)
_MONTHLY_QUOTA = 1000


class ApiError(Exception):
    """status 0 = network / deadline / paginated / bad_response."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _paginated(body: dict, n: int) -> bool:
    for d in (body, body.get("meta")):
        if not isinstance(d, dict):
            continue
        total = d.get("total")
        if d.get("next") or d.get("has_more"):
            return True
        if isinstance(total, int) and not isinstance(total, bool) and total > n:
            return True
    return False


class Client:
    def __init__(self, session, base, key, store, clock=time.time):
        self._session = session
        self._base = base.rstrip("/")
        self._key = key
        self._store = store
        self._clock = clock

    def _month(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m")

    def quota_remaining(self) -> int:
        q = self._store.quota()
        if q.month == self._month() and q.remaining is not None:
            return q.remaining
        used = q.used if q.month == self._month() else 0
        return _MONTHLY_QUOTA - used

    def _record(self, header: str | None) -> None:
        month = self._month()
        q = self._store.quota()
        if q.month != month:
            q = type(q)(month, None, 0, False)
        remaining = q.remaining
        try:
            remaining = int(header) if header is not None else remaining
        except ValueError:
            pass
        self._store.set_quota(month, remaining, q.used + 1, q.warned)

    async def _request(self, method: str, path: str, body: dict | None = None):
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        try:
            async with self._session.request(
                method, self._base + path, json=body, headers=headers, timeout=_TIMEOUT
            ) as resp:
                self._record(resp.headers.get("X-RateLimit-Remaining"))
                raw = await resp.read()
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise ApiError(0, "network", type(e).__name__) from e
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if status >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                raise ApiError(
                    status, str(err.get("code") or f"http_{status}"), str(err.get("message") or "")
                )
            raise ApiError(status, f"http_{status}")
        if not isinstance(data, dict):
            raise ApiError(0, "bad_response", "body is not a JSON object")
        return data

    async def list_spools(self) -> list[dict]:
        body = await self._request("GET", "/my/spools")
        data = body.get("data")
        if not isinstance(data, list):
            raise ApiError(0, "bad_response", "missing data list")
        if _paginated(body, len(data)):
            raise ApiError(0, "paginated", "3dfp list is paginated; refusing partial data")
        return data

    async def patch_spool(self, short_code: str, body: dict) -> dict:
        out = await self._request("PATCH", f"/my/spools/{short_code}", body)
        spool = out.get("spool")
        return spool if isinstance(spool, dict) else out

