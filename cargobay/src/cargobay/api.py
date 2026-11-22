"""Spoolman Part 1 HTTP API (read side)."""

import base64
import binascii
import hmac
import json

from aiohttp import web

from . import __version__
from .config import Options
from .inventory import Inventory, Refused, Upstream

INVENTORY = web.AppKey("inventory", Inventory)
BINDER = web.AppKey("binder", object)
OPTIONS = web.AppKey("options", Options)


def _json(data, status: int = 200, headers: dict | None = None) -> web.Response:
    return web.json_response(data, status=status, headers=headers)


def _presented_key(request: web.Request) -> str:
    key = request.headers.get("X-API-Key")
    if key is not None:
        return key
    auth = request.headers.get("Authorization", "")
    scheme, _, value = auth.partition(" ")
    value = value.strip()
    if scheme.lower() == "bearer":
        return value
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return ""
        return decoded.partition(":")[2]
    return ""


@web.middleware
async def auth(request: web.Request, handler):
    if not (request.method == "GET" and request.path == "/api/v1/health"):
        key = request.app[OPTIONS].api_key
        if not hmac.compare_digest(_presented_key(request).encode(), key.encode()):
            return _json({"error": "unauthorized"}, 401)
    return await handler(request)


def _missing() -> web.Response:
    return _json({"error": "not found"}, 404)


async def not_found(_request: web.Request) -> web.Response:
    return _missing()


async def health(_r: web.Request) -> web.Response:
    return _json({"status": "healthy"})


async def info(_r: web.Request) -> web.Response:
    return _json({"version": __version__})


async def list_spools(request: web.Request) -> web.Response:
    inv = request.app[INVENTORY]
    q = request.query
    inv.maybe_refresh("list")
    allow = q.get("allow_archived", "false").lower() == "true"
    spools = inv.spools(allow)
    tag = q.get("tag")
    if tag is not None:
        found = inv.find_by_tag(tag, allow)
        spools = [found] if found else []
    for k, want in q.items():
        if k.startswith("extra."):
            name = k[len("extra."):]
            spools = [s for s in spools if want in _plain(s["extra"].get(name))]
    if q.get("sort") in ("id:desc", "id:asc"):
        spools.sort(key=lambda s: s["id"], reverse=q["sort"] == "id:desc")
    total = len(spools)
    try:
        limit = int(q["limit"])
    except (KeyError, ValueError):
        limit = None
    if limit is not None and limit >= 0:
        spools = spools[:limit]
    return _json(spools, headers={"x-total-count": str(total)})


def _plain(value: str | None) -> str:
    """Stored extra values are JSON text; compare without the JSON quotes."""
    if value is None:
        return "\0"  # never matches
    try:
        v = json.loads(value)
    except ValueError:
        return value
    return v if isinstance(v, str) else value


async def one_spool(request: web.Request) -> web.Response:
    try:
        spool_id = int(request.match_info["id"])
    except ValueError:
        return _missing()
    spool = request.app[INVENTORY].spool(spool_id)
    return _json(spool) if spool else _missing()


async def field_list(request: web.Request) -> web.Response:
    return _json(request.app[INVENTORY].field_list())


async def locations(request: web.Request) -> web.Response:
    return _json(request.app[INVENTORY].locations())


async def tag_reader(_r: web.Request) -> web.Response:
    return _json([])


def _spool_id(request: web.Request) -> int | None:
    try:
        return int(request.match_info["id"])
    except ValueError:
        return None


async def _write(request: web.Request, run) -> web.Response:
    spool_id = _spool_id(request)
    if spool_id is None:
        return _missing_msg()
    try:
        body = await request.json()
    except ValueError:
        return _json({"message": "body must be JSON"}, 400)
    try:
        return _json(await run(request.app[INVENTORY], spool_id, body))
    except Refused as e:
        out = {"message": e.message}
        if e.spool_id is not None:
            out["spool_id"] = e.spool_id
        return _json(out, e.status)
    except Upstream as e:
        return _json({"message": e.message}, 502)


def _missing_msg() -> web.Response:
    return _json({"message": "not found"}, 404)


async def patch_spool(request: web.Request) -> web.Response:
    return await _write(request, lambda inv, i, body: inv.patch(i, body))


async def measure(request: web.Request) -> web.Response:
    async def run(inv, i, body):
        if not isinstance(body, dict) or "weight" not in body:
            raise Refused(400, "weight is required")
        return await inv.measure(i, body["weight"])
    return await _write(request, run)


def make_app(inventory: Inventory, binder, options: Options) -> web.Application:
    app = web.Application(middlewares=[auth])
    app[INVENTORY] = inventory
    app[BINDER] = binder
    app[OPTIONS] = options
    p = "/api/v1"
    app.router.add_get(f"{p}/health", health)
    app.router.add_get(f"{p}/info", info)
    app.router.add_get(f"{p}/spool", list_spools)
    app.router.add_get(f"{p}/spool/{{id}}", one_spool)
    app.router.add_patch(f"{p}/spool/{{id}}", patch_spool)
    app.router.add_put(f"{p}/spool/{{id}}/measure", measure)
    app.router.add_get(f"{p}/field/spool", field_list)
    app.router.add_get(f"{p}/location", locations)
    app.router.add_get(f"{p}/tag/reader", tag_reader)
    for path in ("external/manufacturer", "external/filament/search", "external/material"):
        app.router.add_get(f"{p}/{path}", not_found)
    for path in ("vendor", "filament", "spool"):
        app.router.add_post(f"{p}/{path}", not_found)
    return app
