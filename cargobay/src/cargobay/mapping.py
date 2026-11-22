"""3dfp spool JSON -> Remote. The only place that knows 3dfp's field names."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Remote:
    short_code: str
    brand: str
    name: str
    material: str
    color_name: str
    color_hex: str | None
    location: str | None
    remaining_grams: int | None
    empty_spool_weight: int | None
    bambu_tray_uuid: str | None
    archived: bool
    filament_key: str
    is_bambu: bool


_RGB = re.compile(r"#?([0-9A-Fa-f]{6})")
_CODE = re.compile(r"\s*\([^()]*\)\s*$")


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def to_remote(item: dict) -> Remote:
    brand = _text(item.get("brand"))
    material = _text(item.get("material"))
    color = _text(item.get("color"))
    name = " ".join(
        f"{material} {_text(item.get('material_type'))} {_CODE.sub('', color)}".split()
    )
    m = _RGB.fullmatch(_text(item.get("rgb")))
    loc = _text(item.get("location"))
    props = item.get("spool_properties")
    uuid = re.sub(
        r"[^0-9A-Fa-f]", "",
        _text(props.get("bambu_tray_uuid")) if isinstance(props, dict) else "",
    ).upper()
    grams = _int(item.get("remaining_grams"))
    fid = item.get("filament_id")
    return Remote(
        short_code=item["short_code"],
        brand=brand,
        name=name,
        material=material,
        color_name=color,
        color_hex=m.group(1).upper() if m else None,
        location=None if not loc or loc.lower() == "shelf" else loc,
        remaining_grams=grams,
        empty_spool_weight=_int(item.get("empty_spool_weight")),
        bambu_tray_uuid=uuid or None,
        archived=grams == 0,
        filament_key=str(fid) if fid is not None else f"{brand}|{name}",
        is_bambu=brand.lower() == "bambu lab",
    )
