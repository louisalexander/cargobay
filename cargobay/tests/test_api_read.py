import base64

import pytest

from cargobay.api import make_app


@pytest.fixture
async def cli(env, aiohttp_client):
    c = await aiohttp_client(make_app(env.inventory, None, env.options))
    c.h = {"X-API-Key": "K"}
    return c


async def test_health_open_rest_needs_key(cli):
    assert (await cli.get("/api/v1/health")).status == 200
    for p in ("/api/v1/info", "/api/v1/spool", "/api/v1/tag/reader", "/api/v1/field/spool"):
        r = await cli.get(p)
        assert r.status == 401, p
        assert "error" in await r.json()
        assert (await cli.get(p, headers={"X-API-Key": "bad"})).status == 401
    basic = base64.b64encode(b"any:K").decode()
    for h in ({"X-API-Key": "K"}, {"Authorization": "Bearer K"},
              {"Authorization": f"Basic {basic}"}):
        for p in ("/api/v1/info", "/api/v1/spool", "/api/v1/tag/reader",
                  "/api/v1/field/spool"):
            assert (await cli.get(p, headers=h)).status == 200
    for bad in ("Basic !!!", "Basic " + base64.b64encode(b"nocolon").decode(), "Bearer"):
        assert (await cli.get("/api/v1/info", headers={"Authorization": bad})).status == 401


async def test_info_version(cli):
    assert (await (await cli.get("/api/v1/info", headers=cli.h)).json()) == {"version": "0.1.0"}


async def test_list_active_and_all(cli):
    active = await (await cli.get("/api/v1/spool?allow_archived=false", headers=cli.h)).json()
    assert len(active) == 4 and all(not s["archived"] for s in active)
    assert await (await cli.get("/api/v1/spool", headers=cli.h)).json() == active
    allx = await (await cli.get("/api/v1/spool?allow_archived=true", headers=cli.h)).json()
    assert len(allx) == 5 and [s["archived"] for s in allx].count(True) == 1
    for s in allx:
        assert {"id", "archived", "extra", "tags"} <= s.keys() and "id" in s["filament"]


async def test_stamp(cli):
    r = await cli.get("/api/v1/spool?allow_archived=false&limit=1&sort=id:desc", headers=cli.h)
    body = await r.json()
    assert r.status == 200 and r.headers["x-total-count"] == "4"
    assert [s["id"] for s in body] == [5]
    r = await cli.get("/api/v1/spool?allow_archived=false&limit=1", headers=cli.h)
    assert len(await r.json()) == 1 and r.headers["x-total-count"] == "4"
    r = await cli.get("/api/v1/spool?allow_archived=true", headers=cli.h)
    assert r.headers["x-total-count"] == "5"
    assert "content-encoding" not in r.headers


async def test_single_and_404(cli):
    r = await cli.get("/api/v1/spool/2", headers=cli.h)
    assert r.status == 200 and (await r.json())["id"] == 2
    assert (await cli.get("/api/v1/spool/4", headers=cli.h)).status == 200  # archived
    assert (await cli.get("/api/v1/spool/999", headers=cli.h)).status == 404
    assert (await cli.get("/api/v1/spool/abc", headers=cli.h)).status == 404


async def test_tag_and_extra_filters(env, cli):
    env.store.link_tag(3, "04A1B2C3D4E5F6", "ntag")
    env.store.merge_extra(2, {"last_dried": '"2026-10-01T12:34:56.000Z"'})
    r = await cli.get("/api/v1/spool?allow_archived=false&tag=04%3Aa1%3Ab2%3Ac3%3Ad4%3Ae5%3Af6",
                      headers=cli.h)
    body = await r.json()
    assert [s["id"] for s in body] == [3]
    assert body[0]["tags"] == [{"uid": "04A1B2C3D4E5F6", "format": "ntag"}]
    assert await (await cli.get("/api/v1/spool?tag=FFFF", headers=cli.h)).json() == []
    env.store.link_tag(4, "AABB", "ntag")  # archived spool
    assert await (await cli.get("/api/v1/spool?allow_archived=false&tag=AABB",
                                headers=cli.h)).json() == []
    assert len(await (await cli.get("/api/v1/spool?allow_archived=true&tag=AABB",
                                    headers=cli.h)).json()) == 1
    r = await cli.get("/api/v1/spool?extra.last_dried=2026-10", headers=cli.h)
    assert [s["id"] for s in await r.json()] == [2]
    r = await cli.get("/api/v1/spool?extra.last_dried=2027", headers=cli.h)
    assert await r.json() == []


async def test_location_list_never_slots(env, cli):
    env.options.extra_locations.append("Dry Box")
    locs = await (await cli.get("/api/v1/location", headers=cli.h)).json()
    assert locs == ["Shelf: Office Supply Closet", "Dry Box"]
    assert not any(l.startswith("Printer:") for l in locs)


async def test_field_list_and_reader(cli):
    fl = await (await cli.get("/api/v1/field/spool", headers=cli.h)).json()
    assert fl[0]["key"] == "last_dried"
    assert await (await cli.get("/api/v1/tag/reader", headers=cli.h)).json() == []


async def test_database_and_create_404(cli):
    for p in ("/api/v1/external/manufacturer", "/api/v1/external/filament/search",
              "/api/v1/external/material", "/api/v1/nope"):
        assert (await cli.get(p, headers=cli.h)).status == 404, p
    for p in ("vendor", "filament", "spool"):
        r = await cli.post(f"/api/v1/{p}", json={}, headers=cli.h)
        assert r.status == 404, p
    assert (await cli.get("/api/v1/spool/", headers=cli.h, allow_redirects=False)).status == 404
