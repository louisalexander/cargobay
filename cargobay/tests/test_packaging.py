import re
from pathlib import Path

import cargobay


def test_version_matches_addon_config():
    text = (Path(__file__).parent.parent / "config.yaml").read_text()
    m = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', text, re.M)
    assert m and m.group(1) == cargobay.__version__


def test_run_sh_keeps_supervisor_env():
    # s6-overlay v3 clears the env for CMD; with-contenv restores SUPERVISOR_TOKEN
    first = (Path(__file__).parent.parent / "run.sh").read_text().splitlines()[0]
    assert first == "#!/usr/bin/with-contenv sh"
