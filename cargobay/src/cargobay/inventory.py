"""Cached view of the 3dfp inventory, rendered as Spoolman objects.

Requests are always answered from the cache (never awaiting 3dfp). The cache is
replaced by a rate-limited background refresh and persisted as a snapshot.
"""

import asyncio
import dataclasses
import logging
import re
import time
from datetime import UTC, datetime
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


_SLOT = re.compile(r"Printer: Bambu P2S, AMS: 1-[1-4]")
_EXTRA_KEY = re.compile(r"[a-z0-9_]+")


def is_slot_location(s: str | None) -> bool:
    return isinstance(s, str) and _SLOT.fullmatch(s) is not None


def slot_location(n: int) -> str:
    return f"Printer: Bambu P2S, AMS: 1-{n}"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v \
        and abs(v) != float("inf")


def _last_used(v) -> str:
    if not isinstance(v, str):
        raise Refused(400, "last_used must be a string")
    try:
        if re.fullmatch(r"\d{4}-\d\d-\d\d", v):
            datetime.strptime(v, "%Y-%m-%d")
            return f"{v}T00:00:00Z"
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise Refused(400, "last_used is not an ISO date") from None
    if v.endswith("+00:00"):
        return v[: -len("+00:00")] + "Z"
    return v


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
        self._gen = 0  # bumped by every applied write; refresh drops stale lists
        self._locks: dict[int, asyncio.Lock] = {}
        self.on_seen: list[Callable[[int], None]] = []
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
        gen = self._gen
        try:
            raw = await self.client.list_spools()
        except ApiError as e:
            log.warning("3dfp refresh failed, keeping cache: %s", e)
            return
        except Exception:
            log.exception("3dfp refresh crashed, keeping cache")
            return
        if gen != self._gen:
            log.info("dropping 3dfp list: a write landed while it was in flight")
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

    def id_for_short_code(self, short_code: str) -> int | None:
        """Cached spool id for a 3dfp short_code; never mints a new id."""
        for i, (_, r) in self._cache.items():
            if r.short_code == short_code:
                return i
        return None

    def spool(self, spool_id: int) -> dict | None:
        hit = self._cache.get(spool_id)
        return self._render(spool_id, hit[1]) if hit else None

    def spools(self, allow_archived: bool) -> list[dict]:
        return [self._render(i, r) for i, (_, r) in self._cache.items()
                if allow_archived or not r.archived]

    def _match(self, uid: str) -> int | None:
        """Spool id for a scanned uid: a native tag, else a Bambu tray UUID."""
        norm = normalise_uid(uid)
        if not norm:
            return None
        owner = self.store.tag_owner(norm)
        if owner is not None and owner in self._cache:
            return owner
        for i, (_, r) in self._cache.items():
            if r.bambu_tray_uuid and r.bambu_tray_uuid == norm:
                return i
        return None

    def find_by_tag(self, uid: str, allow_archived: bool = False) -> dict | None:
        i = self._match(uid)
        s = self.spool(i) if i is not None else None
        return s if s and (allow_archived or not s["archived"]) else None

    # tags
    def scan(self, uid: str) -> dict:
        """Read-only lookup; archived spools match. A miss may start a bounded refresh."""
        i = self._match(uid)
        if i is None:
            self.maybe_refresh("scan_miss")
            return {"matched_spool_id": None}
        for cb in list(self.on_seen):
            try:
                cb(i)
            except Exception:
                log.exception("on_seen callback failed")
        return {"matched_spool_id": i, "spool": self.spool(i)}

    def link(self, spool_id: int, uid: str, fmt: str = "ntag") -> dict:
        self._require(spool_id)
        norm = normalise_uid(uid) if isinstance(uid, str) else ""
        if not norm or len(norm) > 64:
            raise Refused(400, "uid must be 1-64 hex digits")
        owner = self.store.tag_owner(norm)
        if owner is not None and owner != spool_id:
            raise Refused(409, f"Tag {norm} is already linked to spool {owner}.",
                          spool_id=owner)
        self.store.link_tag(spool_id, norm, fmt if isinstance(fmt, str) and fmt else "ntag")
        return self.spool(spool_id)

    def unlink(self, spool_id: int, uid: str) -> bool:
        self._require(spool_id)
        return self.store.unlink_tag(spool_id, normalise_uid(uid))

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

    # writes
    def bambu_loaded(self, spool_id: int) -> bool:
        r = self.remote(spool_id)
        if r is None or not r.is_bambu or not r.bambu_tray_uuid:
            return False
        for tray in self.trays().values():
            uid = normalise_uid(str(tray.get("tray_uuid") or ""))
            if uid and set(uid) != {"0"} and uid == r.bambu_tray_uuid:
                return True
        return False

    def _lock(self, spool_id: int) -> asyncio.Lock:
        return self._locks.setdefault(spool_id, asyncio.Lock())

    async def patch(self, spool_id: int, body: dict) -> dict:
        self._require(spool_id)
        async with self._lock(spool_id):
            return await self._patch(spool_id, body)

    async def _patch(self, spool_id: int, body: dict) -> dict:
        r = self._require(spool_id)
        if not isinstance(body, dict):
            raise Refused(400, "body must be a JSON object")
        remote: dict = {}
        local: dict = {}
        extra: dict[str, str] = {}
        weight = None
        if "remaining_weight" in body:
            w = body["remaining_weight"]
            if not _num(w) or w < 0:
                raise Refused(400, "remaining_weight must be a non-negative number")
            weight = w
        archived = body.get("archived")
        if "archived" in body and not isinstance(archived, bool):
            raise Refused(400, "archived must be a boolean")
        for key in ("initial_weight", "spool_weight"):
            if key in body:
                if not _num(body[key]) or body[key] < 0:
                    raise Refused(400, f"{key} must be a non-negative number")
                local[key] = body[key]
        if "last_used" in body:
            local["last_used"] = _last_used(body["last_used"])
        if "extra" in body:
            e = body["extra"]
            if not isinstance(e, dict) or not all(
                    _EXTRA_KEY.fullmatch(k) and isinstance(v, str) for k, v in e.items()):
                raise Refused(400, "extra must map [a-z0-9_] keys to JSON-encoded strings")
            extra = dict(e)
        if "location" in body:
            loc = body["location"]
            if loc is not None and not isinstance(loc, str):
                raise Refused(400, "location must be a string or null")
            if is_slot_location(loc):
                raise Refused(409, "slot locations are set by the printer binding")
            remote["location"] = loc
        if archived is True:
            weight = 0
        elif archived is False and (weight is None or round(weight) <= 0):
            raise Refused(409, "unarchive needs a remaining_weight above 0")
        if weight is not None:
            if self.bambu_loaded(spool_id):
                raise Refused(409, "reel is loaded in the AMS; the printer owns its weight",
                              spool_id)
            grams = round(weight)
            remote["remaining_grams"] = grams
            if "initial_weight" not in local and grams > self._initial(spool_id):
                local["initial_weight"] = grams
        await self._commit(spool_id, r, remote, local, extra)
        return self.spool(spool_id)

    async def measure(self, spool_id: int, gross) -> dict:
        self._require(spool_id)
        if not _num(gross) or gross < 0:
            raise Refused(400, "weight must be a non-negative number")
        async with self._lock(spool_id):
            return await self._measure(spool_id, gross)

    async def _measure(self, spool_id: int, gross) -> dict:
        r = self._require(spool_id)
        if self.bambu_loaded(spool_id):
            raise Refused(409, "reel is loaded in the AMS; the printer owns its weight",
                          spool_id)
        tare = (self.spool(spool_id) or {}).get("spool_weight") or 0
        net = round(max(gross - tare, 0))
        local: dict = {"last_used": self._now_iso()}
        if net > self._initial(spool_id):
            local["initial_weight"] = net
        await self._commit(spool_id, r, {"remaining_grams": net}, local, {})
        for cb in list(self.on_seen):
            try:
                cb(spool_id)
            except Exception:
                log.exception("on_seen callback failed")
        return self.spool(spool_id)

    async def set_location(self, spool_id: int, location: str | None, *,
                           internal: bool = False) -> None:
        r = self._require(spool_id)
        if not internal and is_slot_location(location):
            raise Refused(409, "slot locations are set by the printer binding")
        async with self._lock(spool_id):
            try:
                await self._commit(spool_id, r, {"location": location}, {}, {})
            except Upstream as e:
                cause = e.__cause__
                if not (isinstance(cause, ApiError) and cause.code == "invalid_location"
                        and location is not None
                        and location == self.options.store_location):
                    raise
                await self._commit(spool_id, r, {"location": None}, {}, {})

    def _require(self, spool_id: int) -> Remote:
        r = self.remote(spool_id)
        if r is None:
            raise Refused(404, "no such spool")
        return r

    def _initial(self, spool_id: int) -> float:
        v = self.store.local(spool_id).initial_weight
        return v if v is not None else FULL_WEIGHT

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _commit(self, spool_id: int, r: Remote, remote: dict, local: dict,
                      extra: dict[str, str]) -> None:
        """One 3dfp PATCH (if any), then local fields and the cache; all or nothing."""
        if remote:
            try:
                await self.client.patch_spool(r.short_code, remote)
            except ApiError as e:
                raise Upstream(f"3dfp refused the write: {e.message or e.code}") from e
        if local:
            self.store.set_local(spool_id, **local)
        if extra:
            self.store.merge_extra(spool_id, extra)
        if remote:
            self._gen += 1
            old_raw, old = self._cache[spool_id]
            raw = {**old_raw, **remote}
            try:
                new = to_remote(raw)
            except (KeyError, TypeError, AttributeError):
                log.exception("could not re-map spool after a 3dfp write; patching cache")
                grams = remote.get("remaining_grams", old.remaining_grams)
                loc = remote.get("location", old.location)
                new = dataclasses.replace(
                    old, remaining_grams=grams, archived=grams == 0,
                    location=loc if loc and loc.lower() != "shelf" else None)
            self._cache[spool_id] = (raw, new)
            self.store.save_snapshot([i for i, _ in self._cache.values()],
                                     self.store.snapshot_ts())
        for cb in list(self.on_change):
            try:
                cb()
            except Exception:
                log.exception("on_change callback failed")
