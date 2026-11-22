import asyncio
import contextlib

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from cargobay.ha import HA, HAError, slot_of, tray_entities
from fakes import fake_ha

TRAY3 = "sensor.laforge_ams_2_pro_tray_3"


class Harness:
    def __init__(self, app, server, session, ha):
        self.app, self.server, self.session, self.ha = app, server, session, ha
        self.states_calls: list[dict] = []
        self.changes: list[tuple] = []
        self.sleeps: list[float] = []
        self.states_evt = asyncio.Event()
        self.change_evt = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def on_states(self, states):
        self.states_calls.append(states)
        self.states_evt.set()

    async def on_change(self, entity_id, old, new):
        self.changes.append((entity_id, old, new))
        self.change_evt.set()

    def start(self):
        self.task = asyncio.create_task(self.ha.run(self.on_states, self.on_change))


async def make_harness(token="T", client_token="T"):
    states = {e: fake_ha.tray_state(int(e[-1])) for e in tray_entities}
    states["light.x"] = {"entity_id": "light.x", "state": "on", "attributes": {}}
    app = fake_ha.make_app(token=token, states=states)
    server = TestServer(app)
    await server.start_server()
    session = aiohttp.ClientSession()
    h = Harness(app, server, session, None)

    async def fast_sleep(delay):
        h.sleeps.append(delay)
        await asyncio.sleep(0.01)

    h.ha = HA(
        session,
        ws_url=str(server.make_url(fake_ha.WS_PATH)).replace("http", "ws"),
        rest_url=str(server.make_url("/api")).rstrip("/"),
        token=client_token,
        sleep=fast_sleep,
    )
    return h


@pytest.fixture
async def h():
    harness = await make_harness()
    yield harness
    if harness.task:
        harness.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await harness.task
    await harness.session.close()
    await harness.server.close()


async def wait(evt):
    await asyncio.wait_for(evt.wait(), 5)


def test_slot_of():
    assert [slot_of(e) for e in tray_entities] == [1, 2, 3, 4]
    for bad in ("sensor.laforge_ams_2_pro_tray_5", "light.x", "sensor.laforge_ams_2_pro_tray_0"):
        with pytest.raises(ValueError):
            slot_of(bad)


async def test_initial_states_and_changes(h):
    h.start()
    await wait(h.states_evt)
    assert len(h.states_calls) == 1
    assert sorted(h.states_calls[0]) == sorted(tray_entities)  # light.x filtered out

    old = h.app[fake_ha.STATES][TRAY3]
    new = fake_ha.tray_state(3, "PLA", remain=-1)
    await fake_ha.push_change(h.app, "light.x", None, {"entity_id": "light.x", "state": "off",
                                                       "attributes": {}})
    await fake_ha.push_change(h.app, TRAY3, old, new)
    await wait(h.change_evt)
    assert h.changes == [(TRAY3, old, new)]


async def test_reconnect_resyncs_without_fake_events(h):
    h.start()
    await wait(h.states_evt)
    h.states_evt.clear()
    await fake_ha.drop_connections(h.app)
    await wait(h.states_evt)
    assert len(h.states_calls) == 2
    assert h.changes == []
    assert h.sleeps and h.sleeps[0] == 1
    # the resubscribed socket still delivers changes
    await fake_ha.push_change(h.app, TRAY3, None, fake_ha.tray_state(3, "PLA"))
    await wait(h.change_evt)
    assert len(h.changes) == 1


async def test_call_service_and_fire_event_recorded(h):
    h.start()
    await wait(h.states_evt)
    await h.ha.call_service("bambu_lab", "set_filament", {"entity_id": TRAY3, "tray_info_idx": "x"})
    await h.ha.fire_event("cargobay_loaded", {"slot": 3})
    assert h.app[fake_ha.CALLS] == [("bambu_lab", "set_filament",
                                     {"entity_id": TRAY3, "tray_info_idx": "x"})]
    assert h.app[fake_ha.EVENTS] == [("cargobay_loaded", {"slot": 3})]


async def test_call_service_failure_raises(h):
    h.start()
    await wait(h.states_evt)
    h.app[fake_ha.FAIL]["call_service"] = True
    with pytest.raises(HAError):
        await h.ha.call_service("bambu_lab", "set_filament", {})


async def test_commands_raise_when_not_connected(h):
    with pytest.raises(HAError, match="not connected"):
        await h.ha.call_service("a", "b", {})
    with pytest.raises(HAError, match="not connected"):
        await h.ha.fire_event("e", {})


async def test_callback_may_call_service_and_errors_are_survived(h):
    calls = []

    async def on_states(states):
        raise RuntimeError("boom")  # must not kill the connection

    async def on_change(entity_id, old, new):
        await h.ha.call_service("d", "s", {"x": 1})  # would deadlock without a dispatcher task
        calls.append(entity_id)
        h.change_evt.set()

    h.task = asyncio.create_task(h.ha.run(on_states, on_change))
    await asyncio.wait_for(_until(lambda: h.app[fake_ha.SUBS]), 5)
    await fake_ha.push_change(h.app, TRAY3, None, fake_ha.tray_state(3, "PLA"))
    await wait(h.change_evt)
    assert calls == [TRAY3] and h.app[fake_ha.CALLS] == [("d", "s", {"x": 1})]
    assert len(h.app[fake_ha.CONNECTIONS]) == 1


async def _until(pred):
    while not pred():
        await asyncio.sleep(0.01)


async def test_set_state_rest(h):
    await h.ha.set_state("sensor.cargobay_slot_3", "on", {"short_code": "abc"})
    assert h.app[fake_ha.STATES_SET] == {
        "sensor.cargobay_slot_3": {"state": "on", "attributes": {"short_code": "abc"}}}


async def test_set_state_rest_error_raises():
    bad = await make_harness(client_token="WRONG")
    try:
        with pytest.raises(HAError):
            await bad.ha.set_state("sensor.x", "1", {})
    finally:
        await bad.session.close()
        await bad.server.close()


async def test_auth_invalid_keeps_retrying_with_backoff():
    bad = await make_harness(client_token="WRONG")
    try:
        bad.start()
        await asyncio.wait_for(_until(lambda: len(bad.sleeps) >= 3), 5)
        assert bad.sleeps[:3] == [1, 2, 4]
        assert not bad.task.done()
        assert bad.states_calls == []
    finally:
        bad.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bad.task
        await bad.session.close()
        await bad.server.close()


async def test_large_get_states_result_is_accepted(h):
    for i in range(50):  # ~5 MiB of unrelated entities, above aiohttp's 4 MiB default cap
        eid = f"sensor.pad_{i}"
        h.app[fake_ha.STATES][eid] = {"entity_id": eid, "state": "x",
                                      "attributes": {"blob": "z" * 100_000}}
    h.start()
    await wait(h.states_evt)
    assert sorted(h.states_calls[0]) == sorted(tray_entities)


async def test_started_callback_survives_disconnect(h):
    started, release, finished = asyncio.Event(), asyncio.Event(), []

    async def on_change(entity_id, old, new):
        started.set()
        await release.wait()
        finished.append(entity_id)

    h.task = asyncio.create_task(h.ha.run(h.on_states, on_change))
    await wait(h.states_evt)
    await fake_ha.push_change(h.app, TRAY3, None, fake_ha.tray_state(3, "PLA"))
    await wait(started)
    await fake_ha.drop_connections(h.app)
    await asyncio.sleep(0.1)
    assert finished == []  # still waiting, not cancelled
    release.set()
    await asyncio.wait_for(_until(lambda: finished), 5)
    h.states_evt.clear()
    await wait(h.states_evt)  # and the client reconnected and resynced afterwards
    assert len(h.states_calls) == 2
