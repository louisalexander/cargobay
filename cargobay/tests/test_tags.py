import pytest

from cargobay.api import make_app
from fakes.fake_3dfp import REQUESTS

UID = "04:A1:B2:C3:D4:E5:F6"
NORM = "04A1B2C3D4E5F6"
BAMBU = "0123456789ABCDEF0123456789ABCDEF"


@pytest.fixture
async def cli(env, aiohttp_client):
    c = await aiohttp_client(make_app(env.inventory, None, env.options))
    c.h = {"X-API-Key": "K"}
    c.reqs = env.fake[REQUESTS]
    c.reqs.clear()
    c.env = env
    return c


async def scan(cli, uid):
    r = await cli.post("/api/v1/tag/scan", json={"uid": uid}, headers=cli.h)
    assert r.status == 200
    return await r.json()


async def test_link_scan_conflict_unknown(cli):
    r = await cli.post("/api/v1/spool/1/tag", json={"uid": UID, "format": "ntag"},
                       headers=cli.h)
    assert r.status == 200 and (await r.json())["tags"] == [{"uid": NORM, "format": "ntag"}]
    hit = await scan(cli, "04a1b2c3d4e5f6")
    assert hit["matched_spool_id"] == 1
    assert hit["spool"]["tags"] == [{"uid": NORM, "format": "ntag"}]
    r = await cli.post("/api/v1/spool/2/tag", json={"uid": UID}, headers=cli.h)
    assert r.status == 409
    assert await r.json() == {"message": f"Tag {NORM} is already linked to spool 1.",
                              "spool_id": 1}
    assert (await scan(cli, "DEADBEEF")) == {"matched_spool_id": None}
    r = await cli.post("/api/v1/spool/1/tag", json={"uid": UID}, headers=cli.h)
    assert r.status == 200  # relink to the same spool


async def test_scan_matches_archived(cli):
    await cli.post("/api/v1/spool/4/tag", json={"uid": "AABBCCDD"}, headers=cli.h)
    assert (await scan(cli, "aa:bb:cc:dd"))["matched_spool_id"] == 4


async def test_scan_never_writes_and_miss_refresh_bounded(cli):
    env = cli.env
    unknown_bambu = "FEDCBA9876543210FEDCBA9876543210"  # may be newly paired in 3dfp
    for _ in range(50):
        env.now[0] += 19 * 60 / 50  # 19 min: only one 10-min gap opens
        assert (await scan(cli, unknown_bambu)) == {"matched_spool_id": None}
    await env.inventory.refresh_done()
    lists = [x for x in cli.reqs if x[0] == "GET" and x[1] == "/my/spools"]
    assert len(lists) == 1
    assert [x for x in cli.reqs if x[0] != "GET"] == []


async def test_unknown_sticker_scan_never_refreshes(cli):
    env = cli.env
    for _ in range(50):
        env.now[0] += 3600  # far past every refresh gap
        assert (await scan(cli, UID)) == {"matched_spool_id": None}  # 7-byte UID
    await env.inventory.refresh_done()
    assert cli.reqs == []


async def test_scan_of_tag_owned_by_uncached_spool_refreshes(cli):
    env = cli.env
    env.store.link_tag(env.store.spool_id("goneSPOOL"), NORM, "ntag")  # not in 3dfp list
    env.now[0] += 601
    assert (await scan(cli, UID)) == {"matched_spool_id": None}
    await env.inventory.refresh_done()
    assert [(m, p) for m, p, _ in cli.reqs] == [("GET", "/my/spools")]


async def test_scan_bambu_tray_uuid(cli):
    assert (await scan(cli, BAMBU))["matched_spool_id"] == 5
    assert (await scan(cli, BAMBU.lower()))["matched_spool_id"] == 5


async def test_scan_hit_calls_on_seen(cli):
    seen = []
    cli.env.inventory.on_seen.append(seen.append)
    await scan(cli, BAMBU)
    await scan(cli, "DEADBEEF")
    assert seen == [5]


async def test_unlink_any_spelling(cli):
    await cli.post("/api/v1/spool/1/tag", json={"uid": UID}, headers=cli.h)
    r = await cli.delete("/api/v1/spool/1/tag/04-a1-b2-c3-d4-e5-f6", headers=cli.h)
    assert r.status == 200
    r = await cli.delete("/api/v1/spool/1/tag/04-a1-b2-c3-d4-e5-f6", headers=cli.h)
    assert r.status == 404


async def test_tag_search_param(cli):
    await cli.post("/api/v1/spool/1/tag", json={"uid": UID}, headers=cli.h)
    r = await cli.get("/api/v1/spool?allow_archived=false&tag=04%3AA1%3AB2%3AC3%3AD4%3AE5%3AF6",
                      headers=cli.h)
    assert [s["id"] for s in await r.json()] == [1]


async def test_link_validation(cli):
    r = await cli.post("/api/v1/spool/1/tag", json={"format": "ntag"}, headers=cli.h)
    assert r.status == 400
    r = await cli.post("/api/v1/spool/99/tag", json={"uid": UID}, headers=cli.h)
    assert r.status == 404
