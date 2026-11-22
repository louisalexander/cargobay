"""Service entry point: wires the components together and runs until stopped."""

import asyncio
import contextlib
import logging
import os
import signal

import aiohttp
from aiohttp import web

from .api import make_app
from .binder import Binder
from .config import load_options
from .ha import HA
from .inventory import Inventory
from .profiles import Profiles, load_table
from .publish import Publisher, QuotaWatch
from .store import Store
from .threedfp import Client

log = logging.getLogger(__name__)


def _read_key(path: str) -> str | None:
    try:
        with open(path) as f:
            key = f.read().strip()
    except OSError as e:
        log.error("cannot read the 3dfp key file %s: %s", path, type(e).__name__)
        return None
    if not key:
        log.error("the 3dfp key file %s is empty", path)
        return None
    return key


async def run(options_path: str = "/data/options.json", *,
              ws_url: str = "ws://supervisor/core/websocket",
              rest_url: str = "http://supervisor/core/api",
              port: int = 7912, host: str = "0.0.0.0") -> None:
    options = load_options(options_path)
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    key = _read_key(options.key_file)
    if key is None:
        log.error("running read-only from the snapshot; writes to 3dfp will fail")

    store = Store(options.db_path)
    session = aiohttp.ClientSession()
    runner: web.AppRunner | None = None
    ha_task: asyncio.Task | None = None
    inventory: Inventory | None = None
    publisher: Publisher | None = None
    quota_watch: QuotaWatch | None = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        client = Client(session, options.threedfp_base, key, store)
        inventory = Inventory(store, client, options)
        await inventory.start()
        profiles = Profiles(load_table(), options.profile_overrides)
        ha = HA(session, ws_url=ws_url, rest_url=rest_url, token=token)
        binder = Binder(store, inventory, ha, profiles, options)
        inventory.trays = binder.trays
        publisher = Publisher(ha, inventory, store)
        quota_watch = QuotaWatch(ha, store)
        inventory.on_change.append(publisher.schedule)
        binder.on_bindings_change.append(publisher.schedule)
        client.on_quota.append(quota_watch)

        runner = web.AppRunner(make_app(inventory, binder, options))
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("Cargo Bay listening on %s:%d", host, port)

        async def on_states(states: dict) -> None:
            await binder.on_states(states)
            await publisher.publish()

        ha_task = asyncio.create_task(ha.run(on_states, binder.on_change))
        await stop.wait()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        if ha_task is not None:
            ha_task.cancel()
            await asyncio.gather(ha_task, return_exceptions=True)
        if inventory is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(inventory.refresh_done(), 3)
        if publisher is not None:
            await publisher.close()
        if quota_watch is not None:
            await quota_watch.close()
        if runner is not None:
            await runner.cleanup()
        await session.close()
        store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
