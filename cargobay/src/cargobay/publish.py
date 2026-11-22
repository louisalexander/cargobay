"""Publishes Cargo Bay's HA sensors (from the cache only) and the quota-low alert."""

import asyncio
import logging
import time

from .ha import HA, HAError
from .inventory import Inventory
from .store import Store

log = logging.getLogger(__name__)

SLOTS = range(1, 5)
QUOTA_WARN_BELOW = 100


class Publisher:
    def __init__(self, ha: HA, inventory: Inventory, store: Store,
                 min_interval: float = 1.0):
        self.ha = ha
        self.inventory = inventory
        self.store = store
        self._min_interval = min_interval
        self._last = float("-inf")
        self._task: asyncio.Task | None = None

    async def publish(self) -> None:
        """Push both sensors. HA trouble is logged; the next schedule() retries."""
        self._last = time.monotonic()
        try:
            await self._publish()
        except HAError as e:
            log.warning("could not publish sensors: %s", e)

    async def _publish(self) -> None:
        items = []
        for s in self.inventory.spools(False):
            r = self.inventory.remote(s["id"])
            if r is None or r.is_bambu:
                continue
            items.append({"id": s["id"], "short_code": r.short_code,
                          "name": f"{r.brand} {r.name}".strip(),
                          "color_hex": r.color_hex, "grams": r.remaining_grams})
        await self.ha.set_state("sensor.cargobay_spools", str(len(items)),
                                {"spools": items, "friendly_name": "Cargo Bay spools"})
        bindings = self.store.bindings()
        for n in SLOTS:
            spool_id = bindings.get(n)
            r = self.inventory.remote(spool_id) if spool_id is not None else None
            state = f"{r.brand} {r.name}".strip() if r else "none"
            await self.ha.set_state(
                f"sensor.cargobay_tray_{n}", state,
                {"spool_id": spool_id if r else None,
                 "short_code": r.short_code if r else None,
                 "friendly_name": f"Cargo Bay tray {n}"})

    def schedule(self) -> None:
        """Sync-callback friendly: at most one publish a second, bursts coalesced."""
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._debounced())

    async def _debounced(self) -> None:
        wait = self._last + self._min_interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        await self.publish()

    async def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class QuotaWatch:
    """Fires cargobay_quota_low once per month when 3dfp requests run low."""

    def __init__(self, ha: HA, store: Store):
        self.ha = ha
        self.store = store
        self._tasks: set[asyncio.Task] = set()

    def __call__(self, remaining: int) -> None:
        q = self.store.quota()
        if remaining >= QUOTA_WARN_BELOW or q.warned:
            return
        self.store.set_quota(q.month, q.remaining, q.used, True)
        task = asyncio.get_running_loop().create_task(self._fire(remaining))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fire(self, remaining: int) -> None:
        try:
            await self.ha.fire_event("cargobay_quota_low", {"remaining": remaining})
        except HAError as e:
            log.warning("could not fire cargobay_quota_low: %s", e)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
