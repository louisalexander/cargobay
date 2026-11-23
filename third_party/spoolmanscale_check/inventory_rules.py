"""Pure judgments on what an inventory server answered (Part 1).

Every function here is a plain function over parsed JSON: no network, no
clock, no printing. check_inventory.py does the talking and asks these,
tests/test_inventory_rules.py pins them down. Section numbers refer to
inventory/spec.md.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

HEX_COLOR = re.compile(r"^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
UTC_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

LOCATION_MAX_BYTES = 47          # section 4.4
DEFAULT_FULL_WEIGHT_G = 1000.0   # section 4.4, with neither initial nor filament weight
WEIGHT_TOLERANCE_G = 0.5         # the scale writes whole grams
ID_MAX = 2147483647              # common.md, Data: the scale holds an id in a signed 32 bits
SERVER_NAME = re.compile(r"^[A-Za-z0-9 ._-]+$")   # section 10, planned

SINGLE_SPOOL = r"/api/v1/spool/\d+"
# How long the scale gives an answer, section 8. First match wins; a request
# not listed has no fixed limit here. The stamp is the one list with limit=1.
ANSWER_BUDGETS_S = (
    ("GET", re.compile(r"^/api/v1/(health|info)$"), 3.0),
    ("GET", re.compile(r"^/api/v1/spool\?.*limit=1(&|$)"), 2.0),
    ("GET", re.compile(r"^/api/v1/spool\?"), 8.0),
    ("GET", re.compile(f"^{SINGLE_SPOOL}$"), 8.0),
    ("GET", re.compile(r"^/api/v1/location$"), 8.0),
    ("POST", re.compile(r"^/api/v1/tag/scan$"), 8.0),
    ("PATCH", re.compile(f"^{SINGLE_SPOOL}$"), 5.0),
    ("PUT", re.compile(f"^{SINGLE_SPOOL}/measure$"), 5.0),
    ("POST", re.compile(f"^{SINGLE_SPOOL}/tag$"), 5.0),
)

# ("must" | "should", message)
Finding = Tuple[str, str]


def normalise_uid(text: Any) -> str:
    """Hex digits only, uppercase: the comparison form for every UID (section 5)."""
    return "".join(c for c in str(text) if c in HEX_DIGITS).upper()


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_id(value: Any) -> bool:
    """common.md, Data: an integer the scale can hold, and 0 means no spool there."""
    return is_int(value) and 1 <= value <= ID_MAX


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_utc_date(text: Any) -> bool:
    return isinstance(text, str) and bool(UTC_DATE.match(text))


def is_hex_color(text: Any) -> bool:
    return isinstance(text, str) and bool(HEX_COLOR.match(text))


def _positive(value: Any) -> Optional[float]:
    """A weight that counts as set: numeric and above zero (0 means unset)."""
    if is_number(value) and value > 0:
        return float(value)
    return None


def tare_of(spool: Dict[str, Any]) -> float:
    """spool_weight, else the filament's, else the vendor's (section 4.4)."""
    filament = spool.get("filament") or {}
    vendor = filament.get("vendor") or {}
    for candidate in (spool.get("spool_weight"), filament.get("spool_weight"),
                      vendor.get("empty_spool_weight")):
        value = _positive(candidate)
        if value is not None:
            return value
    return 0.0


def full_weight_of(spool: Dict[str, Any]) -> float:
    """initial_weight, else filament.weight, else 1000 g (section 4.4)."""
    filament = spool.get("filament") or {}
    for candidate in (spool.get("initial_weight"), filament.get("weight")):
        value = _positive(candidate)
        if value is not None:
            return value
    return DEFAULT_FULL_WEIGHT_G


def net_after_measure(gross_g: float, spool: Dict[str, Any]) -> Tuple[float, float]:
    """What a measure of gross_g must leave: (remaining_weight, initial_weight).

    Section 6.1: the server subtracts the tare it reports; a net weight above
    the full weight becomes the new initial weight.
    """
    net = gross_g - tare_of(spool)
    return net, max(full_weight_of(spool), net)


def weights_match(a: Any, b: Any) -> bool:
    return is_number(a) and is_number(b) and abs(float(a) - float(b)) <= WEIGHT_TOLERANCE_G


def spool_object_problems(spool: Any, native_tags: bool) -> List[Finding]:
    """Section 4.4: the fields the scale reads, at their levels."""
    if not isinstance(spool, dict):
        return [("must", "spool is not a JSON object")]
    found: List[Finding] = []
    found += _shape_findings(spool, native_tags)
    found += _weight_findings(spool)
    found += _filament_findings(spool.get("filament"))
    found += _text_findings(spool)
    return found


def _shape_findings(spool: Dict[str, Any], native_tags: bool) -> List[Finding]:
    found: List[Finding] = []
    if not is_id(spool.get("id")):
        found.append(("must", f"id is not an integer from 1 to {ID_MAX}"))
    if not isinstance(spool.get("archived"), bool):
        found.append(("must", "archived is not a boolean"))
    if not isinstance(spool.get("extra"), dict):
        found.append(("must", "extra is missing or not an object ({} when empty)"))
    if native_tags and not isinstance(spool.get("tags"), list):
        found.append(("must", "tags is missing or not an array ([] when empty)"))
    return found


def _weight_findings(spool: Dict[str, Any]) -> List[Finding]:
    found: List[Finding] = []
    for key in ("remaining_weight", "initial_weight", "spool_weight"):
        value = spool.get(key)
        if value is not None and not is_number(value):
            found.append(("must", f"{key} is not a JSON number (reads as 0 on the scale)"))
    return found


def _filament_findings(filament: Any) -> List[Finding]:
    if not isinstance(filament, dict):
        return [("must", "filament is missing or not an object")]
    found: List[Finding] = []
    if not is_id(filament.get("id")):
        found.append(("must", f"filament.id is not an integer from 1 to {ID_MAX}"))
    color = filament.get("color_hex")
    if color is not None and not is_hex_color(color):
        found.append(("should", f"filament.color_hex {color!r} is not RRGGBB or RRGGBBAA without '#'"))
    for key in ("weight", "spool_weight"):
        value = filament.get(key)
        if value is not None and not is_number(value):
            found.append(("must", f"filament.{key} is not a JSON number"))
    vendor = filament.get("vendor")
    if vendor is not None and not isinstance(vendor, dict):
        found.append(("should", "filament.vendor is not an object"))
    elif isinstance(vendor, dict) and not is_id(vendor.get("id")):
        found.append(("must", f"filament.vendor.id is not an integer from 1 to {ID_MAX}"))
    return found


def _text_findings(spool: Dict[str, Any]) -> List[Finding]:
    found: List[Finding] = []
    location = spool.get("location")
    if location is not None and not isinstance(location, str):
        found.append(("should", "location is not a string"))
    elif isinstance(location, str) and len(location.encode("utf-8")) > LOCATION_MAX_BYTES:
        found.append(("should", f"location longer than {LOCATION_MAX_BYTES} bytes is cut on the scale"))
    for key in ("last_used", "first_used", "registered"):
        value = spool.get(key)
        if value is not None and not is_utc_date(value):
            found.append(("should", f"{key} {value!r} is not ISO 8601 UTC with Z"))
    return found


def list_problems(spools: Any, allow_archived: bool) -> List[Finding]:
    """Section 4.2: one array, unique ids, no archived spool in the active list."""
    if not isinstance(spools, list):
        return [("must", "answer is not a JSON array")]
    found: List[Finding] = []
    ids = [s.get("id") for s in spools if isinstance(s, dict)]
    if len(set(ids)) != len(ids):
        found.append(("must", "spool ids are not unique"))
    if not allow_archived:
        archived = [s.get("id") for s in spools if isinstance(s, dict) and s.get("archived") is True]
        if archived:
            found.append(("must", f"allow_archived=false returned archived spools {archived[:5]}"))
    return found


def scan_problem(answer: Any, expected_id: Optional[int]) -> Optional[str]:
    """Section 5.1: a scan answers 200 with matched_spool_id, the id or null."""
    if not isinstance(answer, dict) or "matched_spool_id" not in answer:
        return "scan answer has no matched_spool_id"
    got = answer.get("matched_spool_id")
    if got != expected_id:
        return f"matched_spool_id is {got!r}, expected {expected_id!r}"
    return None


def tags_hold(spool: Dict[str, Any], uid: str) -> bool:
    """Section 5.1: spool objects list their tags, in any spelling."""
    want = normalise_uid(uid)
    return any(isinstance(t, dict) and normalise_uid(t.get("uid", "")) == want
               for t in spool.get("tags") or [])


def stamp_problems(spools: Any, header_count: Optional[int], active_count: int,
                   highest_id: Optional[int]) -> List[Finding]:
    """Section 6.2: limit=1 and sort=id:desc give the newest spool and the count."""
    found: List[Finding] = []
    if not isinstance(spools, list) or len(spools) != 1:
        found.append(("should", "limit=1 did not return exactly one spool"))
    elif highest_id is not None and (spools[0] or {}).get("id") != highest_id:
        found.append(("should", f"sort=id:desc did not return the highest id {highest_id}"))
    if header_count is None:
        found.append(("should", "x-total-count header missing"))
    elif header_count != active_count:
        found.append(("should", f"x-total-count {header_count} differs from the active list ({active_count})"))
    return found


def key_problems(statuses: Dict[str, int]) -> List[Finding]:
    """Section 2: a wrong or missing key gets 401 or 403 everywhere but health."""
    return [("must", f"{what} -> HTTP {status}") for what, status in statuses.items()
            if status not in (401, 403)]


def answer_budget_s(method: str, path: str) -> Optional[float]:
    """The time section 8 gives this request, or None when it names no fixed one."""
    for budget_method, pattern, seconds in ANSWER_BUDGETS_S:
        if method == budget_method and pattern.match(path):
            return seconds
    return None


def slow_problems(timed: List[Tuple[str, str, float]]) -> List[Finding]:
    """Section 8: answers later than the scale waits. The time includes connecting."""
    found: List[Finding] = []
    for method, path, seconds in timed:
        budget = answer_budget_s(method, path)
        if budget is not None and seconds > budget:
            found.append(("should", f"{method} {path} took {seconds:.1f} s with connecting, "
                                    f"the scale waits {budget:g} s for the answer"))
    return found


def retry_after_s(header: Optional[str]) -> Optional[int]:
    """Section 10: Retry-After in seconds. A date, or nothing, gives no wait."""
    text = (header or "").strip()
    return int(text) if text.isdigit() else None


def server_name_problem(info: Any) -> Optional[str]:
    """Section 10, planned: what a server name in /info may hold. None when fine or absent."""
    server = info.get("server") if isinstance(info, dict) else None
    if server is None:
        return None
    name = server.get("name") if isinstance(server, dict) else None
    if isinstance(name, str) and SERVER_NAME.match(name):
        return None
    return f"server.name {name!r} holds more than letters, digits, space, '.', '-' and '_': it will be ignored"
