import pytest

from cargobay.api import make_app
from cargobay.inventory import Upstream
from fakes.fake_3dfp import FAIL, REQUESTS

SC = "acmePLA01"


@pytest.fixture
async def cli(env, aiohttp_client):
    c = await aiohttp_client(make_app(env.inventory, None, env.options))
    c.h = {"X-API-Key": "K"}
    c.reqs = env.fake[REQUESTS]
    c.reqs.clear()  # drop the startup list call
    return c


def patched(cli):
    return [(m, p, b) for m, p, b in cli.reqs if m == "PATCH"]


async def test_patch_weight_absolute_and_reads_back(cli):
    r = await cli.patch("/api/v1/spool/1", json={"remaining_weight": 500}, headers=cli.h)
    assert r.status == 200 and (await r.json())["remaining_weight"] == 500
    assert patched(cli) == [("PATCH", f"/my/spools/{SC}", {"remaining_grams": 500})]
    assert (await (await cli.get("/api/v1/spool/1", headers=cli.h)).json())["remaining_weight"] == 500


async def test_patch_location_null_reads_back_absent(cli):
    r = await cli.patch("/api/v1/spool/3", json={"location": None}, headers=cli.h)
    assert r.status == 200
    assert "location" not in await (await cli.get("/api/v1/spool/3", headers=cli.h)).json()
    assert patched(cli)[-1][2] == {"location": None}


async def test_patch_slot_location_refused(cli):
    r = await cli.patch("/api/v1/spool/1", json={"location": "Printer: Bambu P2S, AMS: 1-3"},
                        headers=cli.h)
    assert r.status == 409 and "message" in await r.json()
    assert cli.reqs == []


async def test_local_only_patch_no_3dfp_call(cli):
    r = await cli.patch("/api/v1/spool/1", json={"spool_weight": 196}, headers=cli.h)
    assert r.status == 200 and (await r.json())["spool_weight"] == 196
    body = {"extra": {"last_dried": "\"2026-10-01T12:34:56.000Z\""}}
    r = await cli.patch("/api/v1/spool/1", json=body, headers=cli.h)
    assert r.status == 200
    assert (await r.json())["extra"]["last_dried"] == "\"2026-10-01T12:34:56.000Z\""
    assert cli.reqs == []


async def test_combined_patch_one_request(cli):
    r = await cli.patch("/api/v1/spool/1", headers=cli.h, json={
        "remaining_weight": 612, "location": "Shelf: Office Supply Closet",
        "last_used": "2026-10-01"})
    assert r.status == 200 and (await r.json())["last_used"] == "2026-10-01T00:00:00Z"
    assert patched(cli) == [("PATCH", f"/my/spools/{SC}", {
        "remaining_grams": 612, "location": "Shelf: Office Supply Closet"})]


async def test_measure_subtracts_reported_tare(cli):
    await cli.patch("/api/v1/spool/1", json={"spool_weight": 196}, headers=cli.h)
    r = await cli.put("/api/v1/spool/1/measure", json={"weight": 1196}, headers=cli.h)
    body = await r.json()
    assert r.status == 200 and body["remaining_weight"] == 1000
    assert body["remaining_weight"] == 1196 - body["spool_weight"]
    assert body["last_used"].endswith("Z")


async def test_measure_over_full_raises_initial(cli):
    await cli.patch("/api/v1/spool/1", json={"spool_weight": 0}, headers=cli.h)
    body = await (await cli.put("/api/v1/spool/1/measure", json={"weight": 1100},
                                headers=cli.h)).json()
    assert body["initial_weight"] == 1100 and body["remaining_weight"] == 1100


async def test_archive_writes_zero(cli):
    r = await cli.patch("/api/v1/spool/1", json={"remaining_weight": 0.0, "archived": True},
                        headers=cli.h)
    assert r.status == 200 and (await r.json())["archived"] is True
    assert patched(cli) == [("PATCH", f"/my/spools/{SC}", {"remaining_grams": 0})]


async def test_unarchive_needs_weight(cli):
    r = await cli.patch("/api/v1/spool/4", json={"archived": False}, headers=cli.h)
    assert r.status == 409 and cli.reqs == []
    r = await cli.patch("/api/v1/spool/4", json={"archived": False, "remaining_weight": 300},
                        headers=cli.h)
    assert (await r.json())["archived"] is False


async def test_bad_requests(cli):
    assert (await cli.patch("/api/v1/spool/99", json={}, headers=cli.h)).status == 404
    assert (await cli.patch("/api/v1/spool/1", json=[1], headers=cli.h)).status == 400
    assert (await cli.patch("/api/v1/spool/1", json={"remaining_weight": "x"},
                            headers=cli.h)).status == 400
    assert (await cli.patch("/api/v1/spool/1", json={"extra": {"Bad": "1"}},
                            headers=cli.h)).status == 400
    assert (await cli.put("/api/v1/spool/1/measure", json={}, headers=cli.h)).status == 400
    assert cli.reqs == []


async def test_3dfp_failure_is_502_and_changes_nothing(cli, env):
    env.fake[FAIL]["*"] = (503, "unavailable")
    snap = env.store.snapshot()
    r = await cli.patch("/api/v1/spool/1", headers=cli.h,
                        json={"remaining_weight": 500, "spool_weight": 150})
    assert r.status == 502 and "message" in await r.json()
    got = await (await cli.get("/api/v1/spool/1", headers=cli.h)).json()
    assert got["remaining_weight"] == 850 and got["spool_weight"] == 200
    assert env.store.snapshot() == snap


async def test_bambu_loaded_refused_shelf_allowed(cli, env):
    uuid = env.inventory.remote(5).bambu_tray_uuid
    env.inventory.trays = lambda: {2: {"tray_uuid": uuid, "empty": False}}
    r = await cli.put("/api/v1/spool/5/measure", json={"weight": 900}, headers=cli.h)
    assert r.status == 409 and cli.reqs == []
    env.inventory.trays = lambda: {}
    r = await cli.put("/api/v1/spool/5/measure", json={"weight": 900}, headers=cli.h)
    assert r.status == 200 and len(patched(cli)) == 1


async def test_set_location_internal_and_retry(env):
    inv = env.inventory
    await inv.set_location(3, "Printer: Bambu P2S, AMS: 1-4", internal=True)
    assert env.fake[REQUESTS][-1][2] == {"location": "Printer: Bambu P2S, AMS: 1-4"}
    env.fake[FAIL]["*"] = (422, "invalid_location")
    n = len(env.fake[REQUESTS])
    with pytest.raises(Upstream):
        await inv.set_location(3, "Nope", internal=True)
    assert len(env.fake[REQUESTS]) - n == 1  # not the store location: no retry


async def test_set_location_store_retries_null(env):
    inv = env.inventory
    inv.options.store_location = "Bad Place"
    env.fake[FAIL]["*"] = (422, "invalid_location")
    n = len(env.fake[REQUESTS])
    with pytest.raises(Upstream):  # fake keeps failing, so the retry fails too
        await inv.set_location(3, "Bad Place", internal=True)
    assert [b for _, _, b in env.fake[REQUESTS][n:]] == [{"location": "Bad Place"},
                                                          {"location": None}]
