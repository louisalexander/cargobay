"""In-process fake of Home Assistant's websocket + REST API (synthetic data only)."""

import copy

import aiohttp
from aiohttp import web

CALLS = web.AppKey("calls", list)  # (domain, service, service_data)
EVENTS = web.AppKey("events", list)  # (event_type, event_data)
STATES_SET = web.AppKey("states_set", dict)  # entity_id -> {"state", "attributes"}
STATES = web.AppKey("states", dict)  # entity_id -> state object
SOCKETS = web.AppKey("sockets", list)  # open websockets
SUBS = web.AppKey("subs", dict)  # id(ws) -> subscription message id
FAIL = web.AppKey("fail", dict)  # mutate in place: {"call_service": True} -> success false
CONNECTIONS = web.AppKey("connections", list)  # one entry per authenticated socket

WS_PATH = "/websocket"


def tray_state(n: int, state: str = "empty", **attrs) -> dict:
    return {
        "entity_id": f"sensor.laforge_ams_2_pro_tray_{n}",
        "state": state,
        "attributes": {"remain": -1, **attrs},
        "last_changed": "2026-01-01T00:00:00+00:00",
    }


def make_app(token: str = "T", states: dict | None = None) -> web.Application:
    app = web.Application()
    app[CALLS] = []
    app[EVENTS] = []
    app[STATES_SET] = {}
    app[STATES] = copy.deepcopy(states) if states is not None else {}
    app[SOCKETS] = []
    app[SUBS] = {}
    app[FAIL] = {}
    app[CONNECTIONS] = []

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.1.0"})
        msg = await ws.receive_json()
        if msg.get("type") != "auth" or msg.get("access_token") != token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.1.0"})
        app[SOCKETS].append(ws)
        app[CONNECTIONS].append(1)
        try:
            async for raw in ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    break
                await handle(ws, raw.json())
        finally:
            if ws in app[SOCKETS]:
                app[SOCKETS].remove(ws)
            app[SUBS].pop(id(ws), None)
        return ws

    async def handle(ws: web.WebSocketResponse, msg: dict) -> None:
        mid, kind = msg["id"], msg["type"]
        ok = {"id": mid, "type": "result", "success": True, "result": None}
        if kind == "get_states":
            ok["result"] = copy.deepcopy(list(app[STATES].values()))
        elif kind == "subscribe_events":
            app[SUBS][id(ws)] = mid
        elif kind == "call_service":
            app[CALLS].append((msg["domain"], msg["service"], msg.get("service_data", {})))
            if app[FAIL].get("call_service"):
                ok = {"id": mid, "type": "result", "success": False,
                      "error": {"code": "service_error", "message": "fake failure"}}
        elif kind == "fire_event":
            app[EVENTS].append((msg["event_type"], msg.get("event_data", {})))
        else:
            ok = {"id": mid, "type": "result", "success": False,
                  "error": {"code": "unknown_command", "message": kind}}
        await ws.send_json(ok)

    async def set_state(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {token}":
            return web.Response(status=401)
        entity_id = request.match_info["entity_id"]
        body = await request.json()
        new = entity_id not in app[STATES_SET]
        app[STATES_SET][entity_id] = {"state": body["state"],
                                      "attributes": body.get("attributes", {})}
        return web.json_response({"entity_id": entity_id, **body}, status=201 if new else 200)

    app.router.add_get(WS_PATH, websocket)
    app.router.add_post("/api/states/{entity_id}", set_state)
    return app


async def push_change(app: web.Application, entity_id: str,
                      old: dict | None, new: dict | None) -> None:
    if new is None:
        app[STATES].pop(entity_id, None)
    else:
        app[STATES][entity_id] = copy.deepcopy(new)
    for ws in list(app[SOCKETS]):
        sub = app[SUBS].get(id(ws))
        if sub is None:
            continue
        await ws.send_json({
            "id": sub, "type": "event",
            "event": {"event_type": "state_changed",
                      "data": {"entity_id": entity_id, "old_state": old, "new_state": new}},
        })


async def drop_connections(app: web.Application) -> None:
    for ws in list(app[SOCKETS]):
        await ws.close()
