"""In-process fake of the 3dfp API (synthetic data only; the repo is public)."""

import copy

from aiohttp import web

REQUESTS = web.AppKey("requests", list)  # (method, path, body) tuples
AUTHS = web.AppKey("auths", list)  # Authorization header per request
FAIL = web.AppKey("fail", dict)  # mutate in place, never reassign a key on a started app
# path or "*" -> (status, code or None for no body)
FAIL_ONCE = web.AppKey("fail_once", dict)  # like FAIL, but each entry fails one request
REMAINING = web.AppKey("remaining", int)
SPOOLS = web.AppKey("spools", list)
EXTRA = web.AppKey("extra", dict)  # merged into the list body (pagination tests)

SAMPLE = [
    {
        "short_code": "acmePLA01", "brand": "Acme", "material": "PLA",
        "material_type": "Basic", "color": "Jet Black", "rgb": "#1A1A1A",
        "location": "Shelf", "remaining_grams": 850, "empty_spool_weight": 200,
        "filament_id": 9001,
    },
    {
        "short_code": "acmePETG1", "brand": "Acme", "material": "PETG",
        "material_type": "Basic", "color": "Sky Blue", "rgb": "#3A8FD9",
        "location": "Printer: Bambu P2S, AMS: 1-2", "remaining_grams": 640,
        "empty_spool_weight": 210, "filament_id": 9002,
    },
    {
        "short_code": "acmeMAT01", "brand": "Acme", "material": "PLA",
        "material_type": "Matte", "color": "Moss Green", "rgb": "#5B7F3A",
        "location": "Shelf: Office Supply Closet", "remaining_grams": 1000,
        "empty_spool_weight": 190, "filament_id": 9003,
    },
    {
        "short_code": "acmeEMP01", "brand": "Acme", "material": "PLA",
        "material_type": "Basic", "color": "Signal Red", "rgb": "#C8102E",
        "location": "Shelf", "remaining_grams": 0, "empty_spool_weight": 200,
        "filament_id": 9004,
    },
    {
        "short_code": "bblPLA001", "brand": "Bambu Lab", "material": "PLA",
        "material_type": "Basic", "color": "Bambu Green (10501)", "rgb": "#00AE42",
        "location": "Printer: Bambu P2S, AMS: 1-1", "remaining_grams": 700,
        "empty_spool_weight": 250, "filament_id": 9005,
        "spool_properties": {
            "bambu_tray_uuid": "0123456789ABCDEF0123456789ABCDEF",
            "bambu_tag_uid": "0123456789ABCDEF",
        },
    },
]


def make_app(spools: list[dict], remaining: int | None = 1000) -> web.Application:
    app = web.Application()
    app[SPOOLS] = copy.deepcopy(spools)
    app[REQUESTS] = []
    app[AUTHS] = []
    app[FAIL] = {}
    app[FAIL_ONCE] = {}
    app[EXTRA] = {}
    app[REMAINING] = remaining  # value before the first request; echoed minus requests seen

    def error(status: int, code: str | None, headers) -> web.Response:
        if code is None:  # bare status, no JSON error body
            return web.Response(status=status, headers=headers)
        return web.json_response(
            {"error": {"code": code, "message": f"fake {code}"}},
            status=status, headers=headers,
        )

    @web.middleware
    async def record(request: web.Request, handler):
        body = await request.json() if request.can_read_body else None
        app[REQUESTS].append((request.method, request.path, body))
        app[AUTHS].append(request.headers.get("Authorization"))
        headers = {}
        if app[REMAINING] is not None:  # None: omit the header
            headers["X-RateLimit-Remaining"] = str(app[REMAINING] - len(app[REQUESTS]))
        fail = (app[FAIL_ONCE].pop(request.path, None) or app[FAIL].get(request.path)
                or app[FAIL].get("*"))
        if fail:
            return error(*fail, headers)
        resp = await handler(request)
        resp.headers.update(headers)
        return resp

    app.middlewares.append(record)

    async def list_spools(request: web.Request) -> web.Response:
        return web.json_response({"data": app[SPOOLS], **app[EXTRA]})

    async def patch_spool(request: web.Request) -> web.Response:
        code = request.match_info["short_code"]
        for s in app[SPOOLS]:
            if s["short_code"] == code:
                s.update(await request.json())
                return web.json_response({"spool": s})
        return web.json_response(
            {"error": {"code": "not_found", "message": "no such spool"}}, status=404
        )

    app.router.add_get("/my/spools", list_spools)
    app.router.add_patch("/my/spools/{short_code}", patch_spool)
    return app
