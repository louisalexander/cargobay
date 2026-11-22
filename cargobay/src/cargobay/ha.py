"""Home Assistant client: Supervisor websocket (states, events, commands) + REST state sets.

The client is a faithful pipe. It never synthesises state changes: after a reconnect it only
calls ``on_states`` with the fresh states and lets the consumer reconcile.
"""

import asyncio
import itertools
import logging
import re
from collections.abc import Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

tray_entities = [f"sensor.laforge_ams_2_pro_tray_{n}" for n in range(1, 5)]
_TRAY_RE = re.compile(r"sensor\.laforge_ams_2_pro_tray_([1-4])")

COMMAND_TIMEOUT = 10.0
BACKOFF_MAX = 30.0

OnStates = Callable[[dict[str, dict]], Awaitable]
OnChange = Callable[[str, dict | None, dict | None], Awaitable]


class HAError(Exception):
    pass


def slot_of(entity_id: str) -> int:
    m = _TRAY_RE.fullmatch(entity_id)
    if not m:
        raise ValueError(f"not an AMS tray entity: {entity_id}")
    return int(m.group(1))


class HA:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        ws_url: str = "ws://supervisor/core/websocket",
        rest_url: str = "http://supervisor/core/api",
        token: str,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
        command_timeout: float = COMMAND_TIMEOUT,
    ):
        self._session = session
        self._ws_url = ws_url
        self._rest_url = rest_url.rstrip("/")
        self._token = token
        self._sleep = sleep
        self._command_timeout = command_timeout
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        # per-connection event routing
        self._sub_id: int | None = None
        self._ready = False
        self._early: list[tuple] = []
        self._queue: asyncio.Queue | None = None

    # ---- public commands -------------------------------------------------

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        await self._command({"type": "call_service", "domain": domain,
                             "service": service, "service_data": data})

    async def fire_event(self, event_type: str, data: dict) -> None:
        await self._command({"type": "fire_event", "event_type": event_type,
                             "event_data": data})

    async def set_state(self, entity_id: str, state: str, attributes: dict) -> None:
        try:
            async with self._session.post(
                f"{self._rest_url}/states/{entity_id}",
                json={"state": state, "attributes": attributes},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=self._command_timeout),
            ) as resp:
                if resp.status not in (200, 201):
                    raise HAError(f"set_state {entity_id}: HTTP {resp.status}")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise HAError(f"set_state {entity_id}: {type(e).__name__}") from e

    # ---- connection loop -------------------------------------------------

    async def run(self, on_states: OnStates, on_change: OnChange) -> None:
        backoff = 1.0
        while True:
            authed = False
            try:
                async with self._session.ws_connect(self._ws_url, heartbeat=30, max_msg_size=0) as ws:
                    await self._authenticate(ws)
                    authed = True
                    backoff = 1.0
                    await self._serve(ws, on_states, on_change)
                log.warning("HA connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep retrying whatever went wrong
                log.warning("HA connection failed: %s", e)
            if authed:
                backoff = 1.0
            await self._sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async with asyncio.timeout(self._command_timeout):
            first = await ws.receive_json()
            if first.get("type") != "auth_required":
                raise HAError(f"unexpected greeting: {first.get('type')}")
            await ws.send_json({"type": "auth", "access_token": self._token})
            reply = await ws.receive_json()
        if reply.get("type") != "auth_ok":
            log.error("HA authentication failed: %s", reply.get("message", reply.get("type")))
            raise HAError("auth failed")

    async def _serve(self, ws, on_states: OnStates, on_change: OnChange) -> None:
        self._ws = ws
        self._sub_id, self._ready, self._early = None, False, []
        self._queue = asyncio.Queue()
        reader = asyncio.create_task(self._read(ws))
        dispatcher = asyncio.create_task(self._dispatch(self._queue, on_states, on_change))
        try:
            # subscribe first, buffer events until the snapshot is queued: no gap, in order
            self._sub_id = next(self._ids)
            await self._command({"type": "subscribe_events", "event_type": "state_changed"},
                                mid=self._sub_id)
            snapshot = await self._command({"type": "get_states"})
            states = {s["entity_id"]: s for s in snapshot or []
                      if s.get("entity_id") in tray_entities}
            self._queue.put_nowait(("states", states))
            for item in self._early:
                self._queue.put_nowait(item)
            self._early, self._ready = [], True
            await reader
        finally:
            self._ws = None
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            # drop queued, not-yet-started items (reconnect resyncs via on_states) but let a
            # callback that already started run to completion; shutdown cancels it.
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(None)
            try:
                await dispatcher
            except BaseException:
                dispatcher.cancel()
                await asyncio.gather(dispatcher, return_exceptions=True)
                raise

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = msg.json()
                mid, kind = data.get("id"), data.get("type")
                if kind == "result":
                    fut = self._pending.pop(mid, None)
                    if fut is None or fut.done():
                        continue
                    if data.get("success"):
                        fut.set_result(data.get("result"))
                    else:
                        err = data.get("error") or {}
                        fut.set_exception(HAError(f"{err.get('code')}: {err.get('message')}"))
                elif kind == "event" and mid == self._sub_id:
                    self._route_event(data.get("event") or {})
        finally:
            self._fail_pending(HAError("connection lost"))

    def _route_event(self, event: dict) -> None:
        d = event.get("data") or {}
        entity_id = d.get("entity_id")
        if entity_id not in tray_entities:
            return
        item = ("change", entity_id, d.get("old_state"), d.get("new_state"))
        if self._ready:
            self._queue.put_nowait(item)
        else:
            self._early.append(item)

    async def _dispatch(self, queue: asyncio.Queue, on_states: OnStates,
                        on_change: OnChange) -> None:
        # separate task so a callback can itself await call_service without deadlocking the reader
        while True:
            item = await queue.get()
            if item is None:
                return
            try:
                if item[0] == "states":
                    await on_states(item[1])
                else:
                    await on_change(*item[1:])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("HA callback failed")

    async def _command(self, payload: dict, mid: int | None = None):
        ws = self._ws
        if ws is None or ws.closed:
            raise HAError("not connected")
        mid = mid if mid is not None else next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await ws.send_json({"id": mid, **payload})
            return await asyncio.wait_for(fut, self._command_timeout)
        except TimeoutError as e:
            raise HAError(f"{payload['type']} timed out") from e
        except (aiohttp.ClientError, ConnectionError) as e:
            raise HAError(f"{payload['type']} failed: {e}") from e
        finally:
            self._pending.pop(mid, None)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
