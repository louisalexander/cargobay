"""Cached view of the 3dfp inventory, rendered as Spoolman objects.

Requests are always answered from the cache (never awaiting 3dfp). The cache is
replaced by a rate-limited background refresh and persisted as a snapshot.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Literal

from .config import Options
from .mapping import Remote, to_remote
from .store import Store, normalise_uid
from .threedfp import ApiError, Client

log = logging.getLogger(__name__)

LIST_MAX_AGE_S = 6 * 3600
SCAN_MISS_MIN_GAP_S = 600
QUOTA_FLOOR = 100
SLOT_PREFIX = "Printer:"
FULL_WEIGHT = 1000
LOCATION_MAX_BYTES = 47


class Refused(Exception):
    def __init__(self, status: int, message: str, spool_id: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.spool_id = spool_id


class Upstream(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def cut_bytes(s: str, limit: int = LOCATION_MAX_BYTES) -> str:
    return s.encode()[:limit].decode(errors="ignore")


class Inventory:
    def __init__(self, store: Store, client: Client, options: Options,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.client = client
        self.options = options
        self._clock = clock
        self._cache: dict[int, tuple[dict, Remote]] = {}
        self._task: asyncio.Task | None = None
        self._last_attempt = 0.0
        self.on_change: list[Callable[[], None]] = []
        self.trays: Callable[[], dict[int, dict]] = lambda: {}

    # lifecycle
    async def start(self) -> None:
        snap = self.store.snapshot()
        if snap is not None:
            self._load(snap)
            if self.client.quota_remaining() < QUOTA_FLOOR:
                return
        self._begin_refresh()

    async def refresh_done(self) -> None:
        task = self._task
        if task is not None:
            await task

    def maybe_refresh(self, reason: Literal["list", "scan_miss"]) -> None:
        now = self._clock()
        if self.client.quota_remaining() < QUOTA_FLOOR:
            return
        if now - self._last_attempt <= SCAN_MISS_MIN_GAP_S:
            return  # also stops a failing 3dfp from being retried on every request
        if reason == "list" and now - self.store.snapshot_ts() <= LIST_MAX_AGE_S:
            return
        self._begin_refresh()

    def _begin_refresh(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._last_attempt = self._clock()
        self._task = asyncio.get_running_loop().create_task(self._refresh())

    async def _refresh(self) -> None:
        try:
            raw = await self.client.list_spools()
        except ApiError as e:
            log.warning("3dfp refresh failed, keeping cache: %s", e)
            return
        except Exception:
            log.exception("3dfp refresh crashed, keeping cache")
            return
        self.store.save_snapshot(raw, self._clock())
        self._load(raw)
        for cb in list(self.on_change):
            try:
                cb()
            except Exception:
                log.exception("on_change callback failed")

    def _load(self, raw: list[dict]) -> None:
        cache = {}
        for item in raw:
            try:
                remote = to_remote(item)
            except (KeyError, TypeError, AttributeError):
                log.warning("skipping unmappable 3dfp spool")
                continue
            cache[self.store.spool_id(remote.short_code)] = (item, remote)
        self._cache = cache

    # reads
    def remote(self, spool_id: int) -> Remote | None:
        hit = self._cache.get(spool_id)
        return hit[1] if hit else None

    def spool(self, spool_id: int) -> dict | None:
        hit = self._cache.get(spool_id)
        return self._render(spool_id, hit[1]) if hit else None

    def spools(self, allow_archived: bool) -> list[dict]:
        return [self._render(i, r) for i, (_, r) in self._cache.items()
                if allow_archived or not r.archived]

    def find_by_tag(self, uid: str, allow_archived: bool = False) -> dict | None:
        owner = self.store.tag_owner(normalise_uid(uid))
        s = self.spool(owner) if owner is not None else None
        return s if s and (allow_archived or not s["archived"]) else None

    def _render(self, spool_id: int, r: Remote) -> dict:
        local = self.store.local(spool_id)
        out: dict = {
            "id": spool_id,
            "archived": r.archived,
            "extra": self.store.extra(spool_id),
            "tags": [{"uid": u, "format": f} for u, f in self.store.tags(spool_id)],
            "initial_weight": local.initial_weight
            if local.initial_weight is not None else FULL_WEIGHT,
        }
        if r.remaining_grams is not None:
            out["remaining_weight"] = r.remaining_grams
        tare = local.spool_weight if local.spool_weight is not None else r.empty_spool_weight
        if tare is not None:
            out["spool_weight"] = tare
        if local.last_used:
            out["last_used"] = local.last_used
        if r.location is not None:
            out["location"] = cut_bytes(r.location)
        filament: dict = {
            "id": self.store.filament_id(r.filament_key),
            "name": r.name,
            "material": r.material,
            "weight": FULL_WEIGHT,
        }
        if r.color_hex is not None:
            filament["color_hex"] = r.color_hex
        if r.brand:
            filament["vendor"] = {"id": self.store.vendor_id(r.brand), "name": r.brand}
        out["filament"] = filament
        return out

    def locations(self) -> list[str]:
        out = [self.options.store_location, *self.options.extra_locations]
        out += [r.location for _, r in self._cache.values() if r.location]
        seen: set[str] = set()
        res = []
        for loc in out:
            loc = cut_bytes(loc)
            if loc and not loc.startswith(SLOT_PREFIX) and loc not in seen:
                seen.add(loc)
                res.append(loc)
        return res

    def field_list(self) -> list[dict]:
        keys = ["last_dried"]
        for i in self._cache:
            for k in self.store.extra(i):
                if k not in keys:
                    keys.append(k)
        return [{"key": k, "name": k, "field_type": "text"} for k in keys]
