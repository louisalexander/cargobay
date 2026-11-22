import json
from dataclasses import dataclass, field


@dataclass
class Options:
    api_key: str
    bind_window_s: int = 300
    key_file: str = "/homeassistant/.3dfp_key"
    threedfp_base: str = "https://3dfilamentprofiles.com/api/v1"
    store_location: str = "Shelf: Office Supply Closet"
    extra_locations: list[str] = field(default_factory=list)
    profile_overrides: list[dict] = field(default_factory=list)
    db_path: str = "/data/cargobay.db"


def load_options(path: str = "/data/options.json") -> Options:
    with open(path) as f:
        raw = json.load(f)
    if not str(raw.get("api_key") or "").strip():
        raise ValueError("api_key is required")
    known = Options.__dataclass_fields__
    return Options(**{k: v for k, v in raw.items() if k in known})
