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

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    store = Store(options.db_path)
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                pass
        session = aiohttp.ClientSession()
        try:
            await _serve(options, store, session, key, token, ws_url, rest_url,
                         host, port, stop)
        finally:
            await session.close()
    finally:
        try:
            for sig in installed:
                loop.remove_signal_handler(sig)
        finally:
            store.close()


async def _serve(options, store, session, key, token, ws_url, rest_url, host, port,
                 stop: asyncio.Event) -> None:
    client = Client(session, options.threedfp_base, key, store)
    inventory = Inventory(store, client, options)
    ha_task: asyncio.Task | None = None
    publisher: Publisher | None = None
    quota_watch: QuotaWatch | None = None
    runner: web.AppRunner | None = None
    try:
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
            await publisher.publish()  # serialised with scheduled publishes

        ha_task = asyncio.create_task(ha.run(on_states, binder.on_change))
        stop_task = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({stop_task, ha_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_task.cancel()
        if ha_task.done() and not stop.is_set():
            exc = None if ha_task.cancelled() else ha_task.exception()
            log.error("Home Assistant client stopped unexpectedly (%r); shutting down",
                      exc)
    finally:
        try:
            if ha_task is not None:
                ha_task.cancel()
                await asyncio.gather(ha_task, return_exceptions=True)
            if publisher is not None:
                await _quiet(publisher.close())
            if quota_watch is not None:
                await _quiet(quota_watch.close())
        finally:
            try:
                if runner is not None:
                    await runner.cleanup()
            finally:
                await _quiet(asyncio.wait_for(inventory.refresh_done(), 3))


async def _quiet(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("error during shutdown")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
