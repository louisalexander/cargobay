import asyncio

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from cargobay.ha import HA
from cargobay.publish import Publisher, QuotaWatch
from fakes import fake_ha
from fakes.fake_3dfp import REQUESTS

DAY = 86400


@pytest.fixture
async def hub(env):
    app = fake_ha.make_app(token="T")
    server = TestServer(app)
    await server.start_server()
    session = aiohttp.ClientSession()
    ha = HA(session, ws_url=str(server.make_url(fake_ha.WS_PATH)).replace("http", "ws"),
            rest_url=str(server.make_url("/api")).rstrip("/"), token="T")
    env.ha, env.app = ha, app
    yield env
    await session.close()
    await server.close()


async def test_spools_sensor_excludes_bambu_and_archived(hub):
    await Publisher(hub.ha, hub.inventory, hub.store).publish()
    st = hub.app[fake_ha.STATES_SET]["sensor.cargobay_spools"]
    assert st["state"] == "3"
    assert st["attributes"]["friendly_name"] == "Cargo Bay spools"
    items = st["attributes"]["spools"]
    assert {i["short_code"] for i in items} == {"acmePLA01", "acmePETG1", "acmeMAT01"}
    first = next(i for i in items if i["short_code"] == "acmePLA01")
    assert first == {"id": 1, "short_code": "acmePLA01", "name": "Acme PLA Basic Jet Black",
                     "color_hex": first["color_hex"], "grams": 850}
    assert first["color_hex"]


async def test_tray_sensor_after_bind(hub):
    pub = Publisher(hub.ha, hub.inventory, hub.store)
    await pub.publish()
    assert hub.app[fake_ha.STATES_SET]["sensor.cargobay_tray_2"]["state"] == "none"
    hub.store.set_binding(2, 1)
    await pub.publish()
    sets = hub.app[fake_ha.STATES_SET]
    assert sets["sensor.cargobay_tray_2"]["state"] == "Acme PLA Basic Jet Black"
    assert sets["sensor.cargobay_tray_2"]["attributes"] == {
        "spool_id": 1, "short_code": "acmePLA01", "friendly_name": "Cargo Bay tray 2"}
    for n in (1, 3, 4):
        assert sets[f"sensor.cargobay_tray_{n}"]["state"] == "none"


async def test_publish_makes_no_3dfp_calls(hub):
    hub.fake[REQUESTS].clear()
    await Publisher(hub.ha, hub.inventory, hub.store).publish()
    assert hub.fake[REQUESTS] == []


async def test_publish_survives_ha_failure(env):
    session = aiohttp.ClientSession()
    ha = HA(session, ws_url="ws://127.0.0.1:1/x", rest_url="http://127.0.0.1:1/api",
            token="T")
    try:
        await Publisher(ha, env.inventory, env.store).publish()  # logs, no raise
    finally:
        await session.close()


async def test_schedule_coalesces_bursts(hub):
    pub = Publisher(hub.ha, hub.inventory, hub.store, min_interval=0.05)
    calls = []
    real = pub._publish

    async def counted():
        calls.append(1)
        await real()

    pub._publish = counted
    for _ in range(5):
        pub.schedule()
    await pub._task
    assert len(calls) == 1
    pub.schedule()
    await pub._task
    assert len(calls) == 2
    await pub.close()


class StubHA:
    def __init__(self):
        self.events = []

    async def fire_event(self, event_type, data):
        self.events.append((event_type, data))


@pytest.mark.parametrize("env", [{"remaining": 100}], indirect=True)
async def test_quota_low_fires_once_per_month(env):
    ha = StubHA()
    env.client.on_quota.append(watch := QuotaWatch(ha, env.store))

    async def events():
        await asyncio.gather(*watch._tasks)
        return ha.events

    await env.client.list_spools()  # fake echoes 100 - requests seen: 98 now
    ev = await events()
    assert len(ev) == 1 and ev[0][0] == "cargobay_quota_low" and ev[0][1]["remaining"] < 100
    assert env.store.quota().warned is True
    await env.client.list_spools()
    assert len(await events()) == 1
    env.now[0] += 40 * DAY  # next month: the client resets warned; low again
    await env.client.list_spools()
    assert len(await events()) == 2
