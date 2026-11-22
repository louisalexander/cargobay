"""Scan-then-load binding of third-party spools to AMS trays (design §5.3).

A binding comes only from a scan (or measure) followed by a load, or from an explicit
bind call. RFID trays are never touched; reconnects never bind.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Literal

from .config import Options
from .ha import HA, HAError, slot_of, tray_entities
from .inventory import Inventory, Refused, Upstream, slot_location
from .profiles import Profiles
from .store import Store, normalise_uid

log = logging.getLogger(__name__)

SLOTS = range(1, 5)


def real_uuid(v) -> bool:
    uid = normalise_uid(str(v or ""))
    return bool(uid) and set(uid) != {"0"}


def _live(st: dict | None) -> bool:
    return st is not None and st.get("state") not in ("unavailable", "unknown")


def _attrs(st: dict | None) -> dict:
    a = (st or {}).get("attributes")
    return a if isinstance(a, dict) else {}


def _gone(st: dict) -> bool:
    """A live tray state that no longer holds a third-party spool."""
    a = _attrs(st)
    return a.get("empty") is True or real_uuid(a.get("tray_uuid"))


class Binder:
    def __init__(self, store: Store, inventory: Inventory, ha: HA, profiles: Profiles,
                 options: Options, clock: Callable[[], float] = time.time):
        self.store = store
        self.inventory = inventory
        self.ha = ha
        self.profiles = profiles
        self.options = options
        self._clock = clock
        self._trays: dict[int, dict] = {}
        self._pending: tuple[int, float] | None = None
        self._lock = asyncio.Lock()
        inventory.on_seen.append(self.spool_seen)

    # ---- state ------------------------------------------------------------

    def trays(self) -> dict[int, dict]:
        return {n: dict(a) for n, a in self._trays.items()}

    def pending(self) -> tuple[int, float] | None:
        p = self._pending
        if p is not None and self._clock() - p[1] <= self.options.bind_window_s:
            return p
        return None

    def spool_seen(self, spool_id: int) -> None:
        """A scan hit or a measure: arm the pending scan unless that is unsafe."""
        now = self._clock()
        if spool_id in self.store.bindings().values():
            return
        last = self.store.last_unbound(spool_id)
        if last is not None and now - last <= self.options.bind_window_s:
            return  # just pulled from a tray: weighing it must not arm the next load
        self._pending = (spool_id, now)

    def _track(self, slot: int, st: dict | None) -> None:
        if _live(st):
            self._trays[slot] = dict(_attrs(st))
        else:
            self._trays.pop(slot, None)

    # ---- HA callbacks -----------------------------------------------------

    async def on_states(self, states: dict[str, dict]) -> None:
        """(Re)connect: refresh tray state and reconcile bindings. Never binds."""
        async with self._lock:
            for n in SLOTS:
                self._track(n, states.get(tray_entities[n - 1]))
            for slot, spool_id in self.store.bindings().items():
                st = states.get(tray_entities[slot - 1])
                if _live(st) and _gone(st):
                    log.info("reconcile: slot %d no longer holds spool %d", slot, spool_id)
                    self.store.clear_binding(slot)
                    self.store.mark_unbound(spool_id, self._clock())

    async def on_change(self, entity_id: str, old: dict | None, new: dict | None) -> None:
        try:
            slot = slot_of(entity_id)
        except ValueError:
            return
        async with self._lock:
            self._track(slot, new)
            if not _live(new):
                return
            if slot in self.store.bindings() and _gone(new):
                emptied = _attrs(new).get("empty") is True
                await self._unbind(slot, shelve=emptied)
                return
            if (_live(old) and _attrs(old).get("empty") is True
                    and _attrs(new).get("empty") is False
                    and not real_uuid(_attrs(new).get("tray_uuid"))):
                await self._loaded(slot)

    async def _loaded(self, slot: int) -> None:
        p = self.pending()
        self._pending = None
        if p is not None:
            try:
                await self._bind(p[0], slot)
                return
            except HAError as e:
                log.error("could not bind spool %d to slot %d: %s", p[0], slot, e)
                return
            except Refused as e:
                log.warning("pending spool %d not bindable: %s", p[0], e.message)
        await self._fire("cargobay_unbound_load", {"slot": slot})

    # ---- bind / unbind ----------------------------------------------------

    async def bind(self, spool_id: int, slot: int,
                   source: Literal["scan", "manual"] = "manual") -> dict:
        async with self._lock:
            log.info("%s bind: spool %d -> slot %d", source, spool_id, slot)
            return await self._bind(spool_id, slot, source)

    async def _bind(self, spool_id: int, slot: int,
                    source: Literal["scan", "manual"] = "scan") -> dict:
        if slot not in SLOTS:
            raise Refused(400, "slot must be 1-4")
        r = self.inventory.remote(spool_id)
        if r is None:
            raise Refused(404, "no such spool")
        if source == "manual" and r.is_bambu:
            raise Refused(409, "Bambu reels are tracked by their RFID tags", spool_id)
        tray = self._trays.get(slot)
        if tray is None:
            raise Refused(409, f"slot {slot} is not visible in Home Assistant")
        if real_uuid(tray.get("tray_uuid")):
            raise Refused(409, f"slot {slot} holds an RFID reel")
        if tray.get("empty") is not False:
            raise Refused(409, f"slot {slot} is not loaded")
        # one spool per tray, one tray per spool (set_binding is INSERT OR REPLACE);
        # released only once the printer took the profile, so an HA failure keeps them
        release = {s: sid for s, sid in self.store.bindings().items()
                   if s == slot or sid == spool_id}
        profile = self.profiles.for_remote(r)
        if profile is None:
            await self.ha.fire_event("cargobay_profile_missing",
                                     {"slot": slot, "material": r.material})
        else:
            await self.ha.call_service("bambu_lab", "set_filament",
                                       {"entity_id": tray_entities[slot - 1], **profile.data()})
        for s in release:
            self.store.clear_binding(s)
        displaced = release.get(slot)
        if displaced is not None and displaced != spool_id:
            self.store.mark_unbound(displaced, self._clock())
            await self._fire("cargobay_unbound", {
                "slot": slot, "spool_id": displaced,
                "short_code": self.store.short_code(displaced)})
        try:
            await self.inventory.set_location(spool_id, slot_location(slot), internal=True)
            written = True
        except (Upstream, Refused) as e:
            log.error("bound spool %d to slot %d but 3dfp location failed: %s",
                      spool_id, slot, e.message)
            written = False
        self.store.set_binding(slot, spool_id)
        await self._fire("cargobay_bound", {
            "slot": slot, "spool_id": spool_id, "short_code": r.short_code,
            "name": f"{r.brand} {r.name}".strip(), "location_written": written})
        return {"slot": slot, "spool_id": spool_id, "location_written": written}

    async def unbind(self, slot: int, *, shelve: bool) -> dict:
        async with self._lock:
            return await self._unbind(slot, shelve=shelve)

    async def unbind_manual(self, slot: int) -> dict:
        """Dashboard unbind: shelve only if the tray reports empty right now."""
        async with self._lock:
            shelve = self._trays.get(slot, {}).get("empty") is True
            return await self._unbind(slot, shelve=shelve)

    async def _unbind(self, slot: int, *, shelve: bool) -> dict:
        if slot not in SLOTS:
            raise Refused(400, "slot must be 1-4")
        spool_id = self.store.bindings().get(slot)
        if spool_id is None:
            return {"slot": slot, "spool_id": None}
        self.store.clear_binding(slot)
        self.store.mark_unbound(spool_id, self._clock())
        self._pending = None  # a swap restarts the sequence
        await self._fire("cargobay_unbound", {
            "slot": slot, "spool_id": spool_id,
            "short_code": self.store.short_code(spool_id)})
        if shelve:
            try:
                await self.inventory.set_location(spool_id, self.options.store_location,
                                                  internal=True)
            except (Upstream, Refused) as e:
                log.error("could not shelve spool %d: %s", spool_id, e.message)
        return {"slot": slot, "spool_id": spool_id}

    async def _fire(self, event_type: str, data: dict) -> None:
        try:
            await self.ha.fire_event(event_type, data)
        except HAError as e:
            log.warning("could not fire %s: %s", event_type, e)
