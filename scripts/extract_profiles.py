#!/usr/bin/env python3
"""Extract Bambu's Generic filament profiles from ha-bambulab's filament table.

Usage:
    ssh haos cat /config/custom_components/bambu_lab/filaments_detail.json > /tmp/f.json
    python3 scripts/extract_profiles.py /tmp/f.json > cargobay/src/cargobay/generic_profiles.json

Keeps only Generic-vendor entries (public profile data):
{id: {"type", "name", "nozzle_min", "nozzle_max"}}.
"""

import json
import sys


def extract(detail: dict) -> dict:
    out = {}
    for idx in sorted(detail):
        e = detail[idx]
        if not isinstance(e, dict) or e.get("filament_vendor") != "Generic":
            continue
        out[idx] = {
            "type": e["filament_type"],
            "name": e["name"],
            "nozzle_min": int(e["nozzle_temperature_range_low"]),
            "nozzle_max": int(e["nozzle_temperature_range_high"]),
        }
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        detail = json.load(f)
    json.dump(extract(detail), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
