import pytest

from cargobay.inventory import Inventory
from cargobay.mapping import to_remote
from cargobay.store import Store
from fakes.fake_3dfp import FAIL, REQUESTS
from conftest import T0

H6 = 6 * 3600


async def test_reads_cost_zero_3dfp_calls(env, aiohttp_client):
    from cargobay.api import make_app

    c = await aiohttp_client(make_app(env.inventory, None, env.options))
    h = {"X-API-Key": "K"}
    env.fake[REQUESTS].clear()
    for _ in range(100):
        assert (await c.get("/api/v1/health")).status == 200
    for _ in range(20):
        assert (await c.get("/api/v1/spool?allow_archived=true", headers=h)).status == 200
        assert (await c.get(
            "/api/v1/spool?allow_archived=false&limit=1&sort=id:desc", headers=h)).status == 200
        assert (await c.get("/api/v1/spool/1", headers=h)).status == 200
    assert (await c.get("/api/v1/location", headers=h)).status == 200
    assert (await c.get("/api/v1/field/spool", headers=h)).status == 200
    assert (await c.get("/api/v1/info", headers=h)).status == 200
    assert (await c.get("/api/v1/tag/reader", headers=h)).status == 200
    await env.inventory.refresh_done()
    assert env.fake[REQUESTS] == []


async def test_list_refresh_at_most_every_6h(env, aiohttp_client):
    from cargobay.api import make_app

    c = await aiohttp_client(make_app(env.inventory, None, env.options))
    h = {"X-API-Key": "K"}
    env.fake[REQUESTS].clear()
    assert (await c.get("/api/v1/spool", headers=h)).status == 200
    await env.inventory.refresh_done()
    assert env.fake[REQUESTS] == []  # start just refreshed

    env.now[0] = T0 + H6 + 1
    r = await c.get("/api/v1/spool?allow_archived=true", headers=h)
    assert len(await r.json()) == 5  # served from cache
    await env.inventory.refresh_done()
    assert [(m, p) for m, p, _ in env.fake[REQUESTS]] == [("GET", "/my/spools")]
    assert env.store.snapshot_ts() == T0 + H6 + 1
    await c.get("/api/v1/spool", headers=h)
    await env.inventory.refresh_done()
    assert len(env.fake[REQUESTS]) == 1


async def test_scan_miss_refresh_after_10_min(env):
    env.fake[REQUESTS].clear()
    env.now[0] = T0 + 599
    env.inventory.maybe_refresh("scan_miss")
    await env.inventory.refresh_done()
    assert env.fake[REQUESTS] == []
    env.now[0] = T0 + 601
    env.inventory.maybe_refresh("scan_miss")
    env.inventory.maybe_refresh("scan_miss")  # one at a time
    await env.inventory.refresh_done()
    assert len(env.fake[REQUESTS]) == 1


@pytest.mark.parametrize("env", [{"remaining": 99}], indirect=True)
async def test_quota_low_stops_refresh(env):
    env.fake[REQUESTS].clear()
    env.now[0] = T0 + 10 * H6
    env.inventory.maybe_refresh("list")
    env.inventory.maybe_refresh("scan_miss")
    await env.inventory.refresh_done()
    assert env.fake[REQUESTS] == []


async def test_snapshot_survives_restart_without_3dfp(env):
    env.fake[FAIL]["*"] = (503, "down")
    store2 = Store(env.db)
    inv2 = Inventory(store2, env.client, env.options, clock=lambda: env.now[0])
    await inv2.start()
    await inv2.refresh_done()
    assert len(inv2.spools(True)) == 5
    assert [s["id"] for s in inv2.spools(True)] == [1, 2, 3, 4, 5]
    store2.close()


async def test_failed_refresh_keeps_cache_and_ids(env):
    env.fake[FAIL]["*"] = (500, "boom")
    env.now[0] = T0 + H6 + 1
    env.inventory.maybe_refresh("list")
    await env.inventory.refresh_done()
    assert len(env.inventory.spools(True)) == 5


async def test_on_change_and_active_filter(env):
    calls = []
    env.inventory.on_change.append(lambda: calls.append(1))
    env.now[0] = T0 + H6 + 1
    env.inventory.maybe_refresh("list")
    await env.inventory.refresh_done()
    assert calls == [1]
    assert len(env.inventory.spools(False)) == 4
    assert env.inventory.remote(4).archived is True
    assert env.inventory.remote(999) is None


async def test_spool_object_shape(env):
    s = env.inventory.spool(2)
    assert s["remaining_weight"] == 640 and s["initial_weight"] == 1000
    assert s["spool_weight"] == 210 and s["extra"] == {} and s["tags"] == []
    assert s["location"] == "Printer: Bambu P2S, AMS: 1-2"
    assert s["filament"]["color_hex"] == "3A8FD9"
    assert s["filament"]["vendor"]["name"] == "Acme"
    assert s["filament"]["weight"] == 1000
    assert "location" not in env.inventory.spool(1)  # Shelf reads as none
    env.store.set_local(1, spool_weight=180.0, initial_weight=800.0)
    s1 = env.inventory.spool(1)
    assert s1["spool_weight"] == 180.0 and s1["initial_weight"] == 800.0


async def test_location_cut_on_utf8_boundary(env):
    item = dict(env.inventory._cache[1][0], location="é" * 40)  # 80 bytes
    env.inventory._cache[1] = (item, to_remote(item))
    loc = env.inventory.spool(1)["location"]
    assert loc == "é" * 23 and len(loc.encode()) == 46


async def test_field_list_includes_extra_keys(env):
    assert env.inventory.field_list() == [
        {"key": "last_dried", "name": "last_dried", "field_type": "text"}]
    env.store.merge_extra(1, {"note": '"x"'})
    keys = [f["key"] for f in env.inventory.field_list()]
    assert keys == ["last_dried", "note"]


def _restart(env):
    store2 = Store(env.db)
    return store2, Inventory(store2, env.client, env.options, clock=lambda: env.now[0])


async def test_restart_with_fresh_snapshot_costs_nothing(env):
    env.fake[REQUESTS].clear()
    env.now[0] = T0 + 599  # the fixture refreshed at T0
    store2, inv2 = _restart(env)
    await inv2.start()
    await inv2.refresh_done()
    assert env.fake[REQUESTS] == [] and len(inv2.spools(True)) == 5
    store2.close()


async def test_restart_with_old_snapshot_refreshes_once(env):
    env.fake[REQUESTS].clear()
    env.now[0] = T0 + 601
    store2, inv2 = _restart(env)
    await inv2.start()
    await inv2.refresh_done()
    assert [(m, p) for m, p, _ in env.fake[REQUESTS]] == [("GET", "/my/spools")]
    store2.close()


async def test_start_without_snapshot_refreshes(env, tmp_path):
    env.fake[REQUESTS].clear()
    store2 = Store(str(tmp_path / "fresh.db"))
    inv2 = Inventory(store2, env.client, env.options, clock=lambda: env.now[0])
    await inv2.start()
    await inv2.refresh_done()
    assert [(m, p) for m, p, _ in env.fake[REQUESTS]] == [("GET", "/my/spools")]
    store2.close()


async def test_restart_quota_low_with_snapshot_costs_nothing(env):
    env.fake[REQUESTS].clear()
    env.now[0] = T0 + 10 * H6  # snapshot old: only the quota floor stops the GET
    q = env.store.quota()
    env.store.set_quota(q.month, 99, q.used, q.warned)
    store2, inv2 = _restart(env)
    assert env.client.quota_remaining() == 99
    await inv2.start()
    await inv2.refresh_done()
    assert env.fake[REQUESTS] == [] and len(inv2.spools(True)) == 5
    store2.close()


async def test_list_refresh_gap_10_min(env):
    env.fake[REQUESTS].clear()
    env.now[0] = T0 + H6 + 1  # old snapshot
    env.inventory.maybe_refresh("list")
    await env.inventory.refresh_done()
    env.now[0] += 599
    env.store.save_snapshot(env.store.snapshot(), 0.0)  # still "old"
    env.inventory.maybe_refresh("list")
    await env.inventory.refresh_done()
    assert len(env.fake[REQUESTS]) == 1
