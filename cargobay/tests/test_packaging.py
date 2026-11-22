import re
from pathlib import Path

import cargobay


def test_version_matches_addon_config():
    text = (Path(__file__).parent.parent / "config.yaml").read_text()
    m = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', text, re.M)
    assert m and m.group(1) == cargobay.__version__
