import asyncio
import contextlib

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from cargobay.api import make_app
from cargobay.binder import Binder
from cargobay.ha import HA
from cargobay.inventory import Refused
from cargobay.profiles import Profiles, load_table
from fakes import fake_ha
from fakes.fake_3dfp import FAIL_ONCE, REQUESTS

ZERO = "0" * 32
REAL = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
TAG = "04A1B2C3D4"
SHELF = "Shelf: Office Supply Closet"


def tray(n, loaded=False, uuid=ZERO, state=None):
    if state is None:
        state = "Generic PLA" if loaded else "Empty"
    return fake_ha.tray_state(n, state, empty=not loaded, tray_uuid=uuid, type="PLA",
                              color="#1A1A1AFF", slot=n)


def entity(n):
    return f"sensor.laforge_ams_2_pro_tray_{n}"


class Rig:
    def __init__(self, env, app, server, session, ha, binder):
        self.env, self.app, self.server, self.session = env, app, server, session
        self.ha, self.binder = ha, binder
        self.done = 0
        self.cond = asyncio.Condition()
        self.task: asyncio.Task | None = None

    @property
    def calls(self):
        return self.app[fake_ha.CALLS]

    @property
    def events(self):
        return self.app[fake_ha.EVENTS]

    @property
    def reqs(self):
        return self.env.fake[REQUESTS]

    def patches(self):
        return [(p, b) for m, p, b in self.reqs if m == "PATCH"]

    async def _tick(self):
        async with self.cond:
            self.done += 1
            self.cond.notify_all()

    async def on_states(self, states):
        await self.binder.on_states(states)
        await self._tick()

    async def on_change(self, eid, old, new):
        await self.binder.on_change(eid, old, new)
        await self._tick()

    async def settle(self, n):
        async with self.cond:
            await asyncio.wait_for(self.cond.wait_for(lambda: self.done >= n), 5)

    async def start(self):
        self.task = asyncio.create_task(self.ha.run(self.on_states, self.on_change))
        await self.settle(1)
        self.reqs.clear()  # drop the startup list call

    async def push(self, n, new):
        old = self.app[fake_ha.STATES].get(entity(n))
        target = self.done + 1
        await fake_ha.push_change(self.app, entity(n), old, new)
        await self.settle(target)

    async def load(self, n):
        await self.push(n, tray(n, loaded=True))

    async def empty(self, n):
        await self.push(n, tray(n))

    def scan(self, spool_id):
        self.env.inventory.link(spool_id, TAG)
        assert self.env.inventory.scan(TAG)["matched_spool_id"] == spool_id
        self.env.store.unlink_tag(spool_id, TAG)


async def make_rig(env, states=None):
    states = states or {entity(n): tray(n) for n in range(1, 5)}
    app = fake_ha.make_app(token="T", states=states)
    server = TestServer(app)
    await server.start_server()
    session = aiohttp.ClientSession()

    async def fast_sleep(_d):
        await asyncio.sleep(0.01)

    ha = HA(session, ws_url=str(server.make_url(fake_ha.WS_PATH)).replace("http", "ws"),
            rest_url=str(server.make_url("/api")).rstrip("/"), token="T", sleep=fast_sleep)
    binder = Binder(env.store, env.inventory, ha, Profiles(load_table(), []), env.options,
                    clock=lambda: env.now[0])
    env.inventory.trays = binder.trays
    return Rig(env, app, server, session, ha, binder)


async def close_rig(rig):
    if rig.task:
        rig.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rig.task
    await rig.session.close()
    await rig.server.close()


@pytest.fixture
async def rig(env):
    r = await make_rig(env)
    await r.start()
    yield r
    await close_rig(r)


def bound_events(rig):
    return [d for t, d in rig.events if t == "cargobay_bound"]


async def test_scan_then_load_binds(rig):
    rig.scan(1)
    assert rig.binder.pending()[0] == 1
    await rig.load(2)
    assert rig.calls == [("bambu_lab", "set_filament", {
        "entity_id": entity(2), "tray_info_idx": "GFL99", "tray_type": "PLA",
        "tray_color": "1A1A1AFF", "nozzle_temp_min": 190, "nozzle_temp_max": 240})]
    assert rig.patches() == [("/my/spools/acmePLA01",
                              {"location": "Printer: Bambu P2S, AMS: 1-2"})]
    assert rig.events == [("cargobay_bound", {
        "slot": 2, "spool_id": 1, "short_code": "acmePLA01",
        "name": "Acme PLA Basic Jet Black", "location_written": True})]
    assert rig.env.store.bindings() == {2: 1}
    assert rig.binder.pending() is None  # consumed
    assert rig.binder.trays()[2]["empty"] is False


async def test_load_without_scan_asks(rig):
    await rig.load(2)
    assert rig.events == [("cargobay_unbound_load", {"slot": 2})]
    assert rig.calls == [] and rig.reqs == []


async def test_rfid_load_ignored(rig):
    rig.scan(1)
    await rig.push(2, tray(2, loaded=True, uuid=REAL))
    assert rig.events == [] and rig.calls == [] and rig.reqs == []
    assert rig.binder.pending()[0] == 1  # still armed for a third-party load


async def test_pending_expires(rig):
    rig.scan(1)
    rig.env.now[0] += 301
    assert rig.binder.pending() is None
    await rig.load(2)
    assert rig.events == [("cargobay_unbound_load", {"slot": 2})]
    assert rig.calls == [] and rig.reqs == []


async def test_reconnect_glitch_not_a_load(rig):
    rig.scan(1)
    await rig.push(2, tray(2, state="unavailable"))
    await rig.load(2)
    await rig.push(3, tray(3, state="unknown"))
    await rig.push(3, tray(3, loaded=True))
    assert rig.events == [] and rig.calls == [] and rig.reqs == []
    assert rig.env.store.bindings() == {}


async def test_missing_empty_attribute_not_a_load(rig):
    rig.scan(1)
    await rig.push(2, fake_ha.tray_state(2, "Generic PLA", tray_uuid=ZERO))
    await rig.load(2)  # old had no `empty`: unknown, not a load
    assert rig.events == [] and rig.calls == []


async def test_swap_never_binds_pulled_spool(rig):
    rig.scan(1)
    await rig.load(2)
    assert rig.env.store.bindings() == {2: 1}
    rig.reqs.clear()
    rig.events.clear()
    rig.calls.clear()
    rig.env.now[0] += 10
    await rig.empty(2)
    assert rig.patches() == [("/my/spools/acmePLA01", {"location": SHELF})]
    assert rig.events == [("cargobay_unbound", {"slot": 2, "spool_id": 1,
                                                "short_code": "acmePLA01"})]
    assert rig.env.store.bindings() == {}
    rig.env.now[0] += 10
    await rig.env.inventory.measure(1, 900)  # weigh the pulled spool A
    assert rig.binder.pending() is None
    await rig.load(2)
    assert rig.events[-1] == ("cargobay_unbound_load", {"slot": 2})
    assert rig.env.store.bindings() == {} and rig.calls == []


async def test_unload_clears_earlier_pending(rig):
    rig.scan(1)
    await rig.load(2)
    rig.scan(3)  # spool 3 armed while 1 sits in slot 2
    await rig.empty(2)
    assert rig.binder.pending() is None
    rig.scan(3)  # re-scan after the unload re-arms
    assert rig.binder.pending()[0] == 3


async def test_bound_spool_scan_does_not_arm(rig):
    rig.scan(1)
    await rig.load(2)
    rig.scan(1)
    assert rig.binder.pending() is None


async def test_shelve_falls_back_to_null(rig):
    rig.scan(1)
    await rig.load(2)
    rig.reqs.clear()
    rig.env.fake[FAIL_ONCE]["/my/spools/acmePLA01"] = (422, "invalid_location")
    await rig.empty(2)
    assert rig.patches() == [("/my/spools/acmePLA01", {"location": SHELF}),
                             ("/my/spools/acmePLA01", {"location": None})]
    assert rig.env.store.bindings() == {}


async def test_rfid_replaces_bound_tray_no_write(rig):
    rig.scan(1)
    await rig.load(2)
    rig.reqs.clear()
    rig.calls.clear()
    await rig.push(2, tray(2, loaded=True, uuid=REAL))
    assert rig.env.store.bindings() == {}
    assert rig.reqs == [] and rig.calls == []
    assert rig.events[-1] == ("cargobay_unbound", {"slot": 2, "spool_id": 1,
                                                   "short_code": "acmePLA01"})


async def test_one_spool_one_tray(rig):
    rig.scan(1)
    await rig.load(2)
    out = await rig.binder.bind(1, 3, "manual")
    assert out["slot"] == 3 and out["spool_id"] == 1
    assert rig.env.store.bindings() == {3: 1}
    await rig.binder.bind(3, 3, "manual")  # spool 3 takes slot 3 from spool 1
    assert rig.env.store.bindings() == {3: 3}


async def test_bind_refuses_rfid_tray(rig):
    await rig.push(4, tray(4, loaded=True, uuid=REAL))
    with pytest.raises(Refused) as e:
        await rig.binder.bind(1, 4, "manual")
    assert e.value.status == 409
    assert rig.calls == [] and rig.reqs == []


async def test_set_filament_failure_aborts_before_3dfp(rig):
    rig.scan(1)
    rig.app[fake_ha.FAIL]["call_service"] = True
    await rig.load(2)
    assert len(rig.calls) == 1 and rig.reqs == [] and rig.events == []
    assert rig.env.store.bindings() == {} and rig.binder.pending() is None


async def test_location_write_failure_still_binds(rig):
    rig.scan(1)
    rig.env.fake[FAIL_ONCE]["/my/spools/acmePLA01"] = (503, "unavailable")
    await rig.load(2)
    assert rig.env.store.bindings() == {2: 1}
    assert bound_events(rig)[0]["location_written"] is False


async def test_profile_missing_still_binds(rig):
    rig.binder.profiles = Profiles({}, [])
    rig.scan(1)
    await rig.load(2)
    assert rig.calls == []
    assert rig.events[0] == ("cargobay_profile_missing", {"slot": 2, "material": "PLA"})
    assert rig.env.store.bindings() == {2: 1}
    assert rig.patches() == [("/my/spools/acmePLA01",
                              {"location": "Printer: Bambu P2S, AMS: 1-2"})]


async def test_reconcile_on_start_silent(env):
    env.store.set_binding(2, 1)
    env.store.set_binding(3, 3)
    env.store.set_binding(4, 2)
    states = {entity(1): tray(1), entity(2): tray(2),
              entity(3): tray(3, loaded=True, uuid=REAL),
              entity(4): tray(4, state="unavailable")}
    rig = await make_rig(env, states)
    try:
        await rig.start()
        assert env.store.bindings() == {4: 2}  # unavailable: left alone
        assert rig.reqs == [] and rig.events == [] and rig.calls == []
        assert 4 not in rig.binder.trays() and rig.binder.trays()[3]["tray_uuid"] == REAL
        # swap safety still holds for the reconciled spool
        env.now[0] += 1
        rig.scan(1)
        assert rig.binder.pending() is None
    finally:
        await close_rig(rig)


async def test_reconnect_never_binds(rig):
    rig.scan(1)
    rig.app[fake_ha.STATES][entity(2)] = tray(2, loaded=True)  # loaded while disconnected
    target = rig.done + 1
    await fake_ha.drop_connections(rig.app)
    await rig.settle(target)
    assert rig.events == [] and rig.calls == [] and rig.env.store.bindings() == {}
    assert rig.binder.trays()[2]["empty"] is False


@pytest.fixture
async def api(rig, aiohttp_client):
    c = await aiohttp_client(make_app(rig.env.inventory, rig.binder, rig.env.options))
    c.h = {"X-API-Key": "K"}
    return c


async def test_manual_bind_route(rig, api):
    await rig.load(3)  # no scan: asks
    rig.events.clear()
    r = await api.post("/cargobay/bind", json={"short_code": "acmePLA01", "slot": 3},
                       headers=api.h)
    assert r.status == 200
    assert await r.json() == {"slot": 3, "spool_id": 1, "location_written": True}
    assert rig.calls == [("bambu_lab", "set_filament", {
        "entity_id": entity(3), "tray_info_idx": "GFL99", "tray_type": "PLA",
        "tray_color": "1A1A1AFF", "nozzle_temp_min": 190, "nozzle_temp_max": 240})]
    assert rig.patches() == [("/my/spools/acmePLA01",
                              {"location": "Printer: Bambu P2S, AMS: 1-3"})]
    assert [t for t, _ in rig.events] == ["cargobay_bound"]
    n = len(rig.reqs)
    r = await api.post("/cargobay/bind", json={"spool_id": 1, "slot": 3})
    assert r.status == 401
    for body in ({"spool_id": 1, "slot": 5}, {"spool_id": 1, "slot": 0},
                 {"spool_id": 1, "slot": "3"}, {"spool_id": 1, "slot": True},
                 {"spool_id": 1}, {"slot": 3}, [1]):
        r = await api.post("/cargobay/bind", json=body, headers=api.h)
        assert r.status == 400, body
    r = await api.post("/cargobay/bind", data="nope", headers=api.h)
    assert r.status == 400
    for body in ({"spool_id": 99, "slot": 3}, {"short_code": "nope", "slot": 3}):
        r = await api.post("/cargobay/bind", json=body, headers=api.h)
        assert r.status == 404, body
    assert len(rig.reqs) == n
    assert rig.env.store.short_code(99) is None  # lookup never minted an id


async def test_manual_bind_route_errors(rig, api):
    await rig.push(4, tray(4, loaded=True, uuid=REAL))
    r = await api.post("/cargobay/bind", json={"spool_id": 1, "slot": 4}, headers=api.h)
    assert r.status == 409
    rig.app[fake_ha.FAIL]["call_service"] = True
    r = await api.post("/cargobay/bind", json={"spool_id": 1, "slot": 2}, headers=api.h)
    assert r.status == 503 and rig.reqs == []
    rig.app[fake_ha.FAIL].clear()
    rig.env.fake[FAIL_ONCE]["/my/spools/acmePLA01"] = (503, "unavailable")
    r = await api.post("/cargobay/bind", json={"spool_id": 1, "slot": 2}, headers=api.h)
    assert r.status == 200 and (await r.json())["location_written"] is False


async def test_manual_unbind_route(rig, api):
    rig.scan(1)
    await rig.load(2)
    rig.reqs.clear()
    r = await api.post("/cargobay/unbind", json={"slot": 2}, headers=api.h)
    assert r.status == 200 and await r.json() == {"slot": 2, "spool_id": 1}
    assert rig.env.store.bindings() == {} and rig.reqs == []  # tray still loaded: no shelve
    r = await api.post("/cargobay/unbind", json={"slot": 2}, headers=api.h)
    assert r.status == 200 and await r.json() == {"slot": 2, "spool_id": None}
    r = await api.post("/cargobay/unbind", json={"slot": 9}, headers=api.h)
    assert r.status == 400
    r = await api.post("/cargobay/unbind", json={"slot": 2})
    assert r.status == 401
    # an emptied tray is shelved on a manual unbind
    await rig.binder.bind(1, 1, "manual")
    rig.reqs.clear()
    r = await api.post("/cargobay/unbind", json={"slot": 1}, headers=api.h)
    assert r.status == 200
    assert rig.patches() == [("/my/spools/acmePLA01", {"location": SHELF})]
