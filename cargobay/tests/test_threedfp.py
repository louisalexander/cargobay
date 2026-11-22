import pytest
from aiohttp.test_utils import TestClient, TestServer

from cargobay.store import Store
from cargobay.threedfp import ApiError, Client
from fakes.fake_3dfp import AUTHS, EXTRA, FAIL, REQUESTS, SAMPLE, SPOOLS, make_app


@pytest.fixture
async def env(tmp_path, request):
    app = make_app(SAMPLE, **getattr(request, "param", {}))
    tc = TestClient(TestServer(app))
    await tc.start_server()
    store = Store(str(tmp_path / "s.db"))
    now = [1_700_000_000.0]  # 2023-11 UTC
    client = Client(tc.session, str(tc.make_url("")).rstrip("/"), "K", store,
                    clock=lambda: now[0])
    yield app, client, store, now
    await tc.close()
    store.close()


async def test_list_returns_data(env):
    app, client, *_ = env
    data = await client.list_spools()
    assert [s["short_code"] for s in data] == [s["short_code"] for s in SAMPLE]


async def test_list_refuses_pagination(env):
    app, client, *_ = env
    app[SPOOLS][:] = [SAMPLE[0]]
    # in-place only: reassigning a key on a started app warns
    for extra in ({"has_more": True}, {"next": "/x?page=2"}, {"total": 5},
                  {"meta": {"total": 5}}):
        app[EXTRA].clear()
        app[EXTRA].update(extra)
        with pytest.raises(ApiError) as e:
            await client.list_spools()
        assert e.value.code == "paginated"


async def test_total_equal_or_bool_is_fine(env):
    app, client, *_ = env
    app[EXTRA].update(total=len(SAMPLE), has_more=False)
    assert len(await client.list_spools()) == len(SAMPLE)
    app[EXTRA].clear()
    app[EXTRA].update(total=True)
    assert len(await client.list_spools()) == len(SAMPLE)


@pytest.mark.parametrize("env", [{"remaining": 812}], indirect=True)
async def test_quota_from_header(env):
    app, client, store, _ = env
    await client.list_spools()
    assert client.quota_remaining() == 811
    q = store.quota()
    assert q.month == "2023-11" and q.used == 1 and q.remaining == 811


@pytest.mark.parametrize("env", [{"remaining": None}], indirect=True)
async def test_no_header_falls_back_to_1000_minus_used(env):
    _, client, store, _ = env
    await client.list_spools()
    await client.list_spools()
    q = store.quota()
    assert q.used == 2 and q.remaining is None
    assert client.quota_remaining() == 998


async def test_month_rollover_resets_before_counting(env):
    _, client, store, now = env
    store.set_quota("2023-10", 5, 900, True)  # stale month
    assert client.quota_remaining() == 1000  # nothing used yet this month
    await client.list_spools()
    q = store.quota()
    assert (q.month, q.used, q.warned) == ("2023-11", 1, False)
    assert q.remaining == 999
    now[0] = 1_701_388_800.0 + 60  # 2023-12-01 00:01 UTC
    assert client.quota_remaining() == 1000
    await client.list_spools()
    q = store.quota()
    assert (q.month, q.used, q.remaining, q.warned) == ("2023-12", 1, 998, False)


@pytest.mark.parametrize("env", [{"remaining": None}], indirect=True)
async def test_rollover_without_header_clears_stale_remaining(env):
    _, client, store, _ = env
    store.set_quota("2023-10", 5, 900, True)
    await client.list_spools()
    q = store.quota()
    assert (q.month, q.used, q.remaining, q.warned) == ("2023-11", 1, None, False)
    assert client.quota_remaining() == 999


async def test_error_body_parsed_and_counted(env):
    app, client, store, _ = env
    app[FAIL]["/my/spools/abc"] = (422, "invalid_location")
    with pytest.raises(ApiError) as e:
        await client.patch_spool("abc", {"location": "nope"})
    assert e.value.status == 422 and e.value.code == "invalid_location"
    assert store.quota().used == 1
    assert client.quota_remaining() == 999


async def test_error_without_body_falls_back(env):
    app, client, *_ = env
    app[FAIL]["*"] = (503, None)
    with pytest.raises(ApiError) as e:
        await client.list_spools()
    assert e.value.status == 503 and e.value.code == "http_503"


async def test_not_found(env):
    _, client, *_ = env
    with pytest.raises(ApiError) as e:
        await client.patch_spool("nope", {"remaining_grams": 1})
    assert e.value.status == 404 and e.value.code == "not_found"


async def test_sends_bearer_and_absolute_grams(env):
    app, client, *_ = env
    out = await client.patch_spool("acmePLA01", {"remaining_grams": 612})
    assert app[AUTHS][-1] == "Bearer K"
    assert app[REQUESTS][-1] == ("PATCH", "/my/spools/acmePLA01", {"remaining_grams": 612})
    assert out["remaining_grams"] == 612


async def test_network_error(tmp_path):
    import aiohttp

    async with aiohttp.ClientSession() as session:
        store = Store(str(tmp_path / "n.db"))
        client = Client(session, "http://127.0.0.1:1", "K", store)
        with pytest.raises(ApiError) as e:
            await client.list_spools()
    assert e.value.status == 0 and e.value.code == "network"
    assert store.quota().used == 1  # no response, but 3dfp may have counted it
