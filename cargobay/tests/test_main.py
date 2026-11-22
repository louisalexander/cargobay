import asyncio
import contextlib
import json
import socket
import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from cargobay.main import run
from cargobay.store import Store
from fakes import fake_ha
from fakes.fake_3dfp import REQUESTS, SAMPLE, make_app as make_3dfp


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


class Rig:
    pass


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    r = Rig()
    r.f3 = make_3dfp(SAMPLE)
    r.fha = fake_ha.make_app(token="T")
    r.s3, r.sha = TestServer(r.f3), TestServer(r.fha)
    await r.s3.start_server()
    await r.sha.start_server()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "T")
    r.tmp, r.port, r.task = tmp_path, free_port(), None
    r.http = aiohttp.ClientSession()

    def write(with_key: bool):
        key = tmp_path / "key"
        if with_key:
            key.write_text("SYNTHETIC-KEY\n")
        opts = {"api_key": "K", "key_file": str(key), "db_path": str(tmp_path / "c.db"),
                "threedfp_base": str(r.s3.make_url("")).rstrip("/")}
        (tmp_path / "options.json").write_text(json.dumps(opts))

    def start():
        r.task = asyncio.create_task(run(
            str(tmp_path / "options.json"),
            ws_url=str(r.sha.make_url(fake_ha.WS_PATH)).replace("http", "ws"),
            rest_url=str(r.sha.make_url("/api")).rstrip("/"),
            port=r.port, host="127.0.0.1"))

    async def stop():
        r.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await r.task

    r.write, r.start, r.stop = write, start, stop
    r.url = lambda p: f"http://127.0.0.1:{r.port}{p}"

    async def healthy():
        try:
            async with r.http.get(r.url("/api/v1/health")) as resp:
                return resp.status == 200
        except aiohttp.ClientError:
            return False

    r.healthy = healthy
    yield r
    if r.task and not r.task.done():
        await r.stop()
    await r.http.close()
    await r.s3.close()
    await r.sha.close()


async def test_boots_with_fakes(rig, caplog):
    rig.write(True)
    rig.start()
    await until(rig.healthy)
    sets = rig.fha[fake_ha.STATES_SET]

    async def published():
        return "sensor.cargobay_spools" in sets and "sensor.cargobay_tray_1" in sets

    await until(published)
    assert sets["sensor.cargobay_spools"]["state"] == "3"
    assert rig.f3[REQUESTS][0][:2] == ("GET", "/my/spools")
    await rig.stop()
    assert rig.task.done()
    assert "SYNTHETIC-KEY" not in caplog.text


async def test_missing_key_file_read_only(rig, caplog):
    rig.write(False)
    seed = Store(str(rig.tmp / "c.db"))
    seed.save_snapshot(SAMPLE, time.time())
    seed.close()
    rig.start()
    await until(rig.healthy)
    h = {"X-API-Key": "K"}
    async with rig.http.get(rig.url("/api/v1/spool"), headers=h) as r:
        assert r.status == 200 and len(await r.json()) == 4  # active incl. Bambu
    async with rig.http.patch(rig.url("/api/v1/spool/1"), headers=h,
                              json={"remaining_weight": 500}) as r:
        assert r.status == 502
    assert rig.f3[REQUESTS] == []  # no network request without a key
    assert "no_key" in caplog.text or "cannot read the 3dfp key file" in caplog.text
    await rig.stop()
