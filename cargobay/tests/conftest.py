
import pytest
from aiohttp.test_utils import TestClient, TestServer

from cargobay.config import Options
from cargobay.inventory import Inventory
from cargobay.store import Store
from cargobay.threedfp import Client
from fakes.fake_3dfp import SAMPLE, make_app as make_fake

T0 = 1_700_000_000.0


class Env:
    """Fake 3dfp + store + client + inventory (started and settled)."""

    def __init__(self, fake, fake_tc, store, client, inventory, options, now, db):
        self.fake, self.fake_tc, self.store = fake, fake_tc, store
        self.client, self.inventory, self.options = client, inventory, options
        self.now, self.db = now, db


@pytest.fixture
async def env(tmp_path, request):
    fake = make_fake(SAMPLE, **getattr(request, "param", {}))
    fake_tc = TestClient(TestServer(fake))
    await fake_tc.start_server()
    db = str(tmp_path / "c.db")
    store = Store(db)
    now = [T0]
    options = Options(api_key="K", db_path=db)
    client = Client(fake_tc.session, str(fake_tc.make_url("")).rstrip("/"), "K",
                    store, clock=lambda: now[0])
    inventory = Inventory(store, client, options, clock=lambda: now[0])
    await inventory.start()
    await inventory.refresh_done()
    yield Env(fake, fake_tc, store, client, inventory, options, now, db)
    await inventory.refresh_done()
    await fake_tc.close()
    store.close()
