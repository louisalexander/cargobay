import json

import pytest

from cargobay.config import load_options


def test_defaults_and_required_key(tmp_path):
    p = tmp_path / "options.json"
    p.write_text(json.dumps({}))
    with pytest.raises(ValueError):
        load_options(str(p))
    p.write_text(json.dumps({"api_key": "k"}))
    o = load_options(str(p))
    assert o.bind_window_s == 300
    assert o.extra_locations == []
