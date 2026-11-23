import asyncio
import re
import sys
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import unused_port

from cargobay.api import make_app

CHECKER = (Path(__file__).resolve().parents[2]
           / "third_party" / "spoolmanscale_check" / "check_inventory.py")


async def test_checker_passes(env):
    assert CHECKER.is_file()
    runner = web.AppRunner(make_app(env.inventory, None, env.options))
    await runner.setup()
    port = unused_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(CHECKER), f"http://127.0.0.1:{port}", "--key", "K",
            "--write", "--spool", "1", "--spool-b", "2",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), 120)
    finally:
        await runner.cleanup()
    text = out.decode()
    assert proc.returncode == 0, text
    bad = [ln for ln in text.splitlines() if re.match(r"^warn\s+1[234]\s", ln)]
    assert not bad, text
    print(text)
