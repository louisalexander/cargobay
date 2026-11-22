# Cargo Bay

Lets an unmodified SpoolmanScale use 3dfilamentprofiles.com (3dfp) as its inventory,
and binds third-party spools to Bambu AMS trays through Home Assistant: the tray gets
the spool's filament profile, and 3dfp gets the weight and slot location.

## Options

| Option | Meaning |
|---|---|
| `api_key` | Shared secret the scale sends as `X-API-Key` (required). |
| `bind_window_s` | Seconds (30-3600, default 300) a scan stays eligible to bind to the next loaded tray. |
| `extra_locations` | Extra 3dfp location names to offer. |
| `profile_overrides` | Per-filament Bambu profile overrides. |

```yaml
api_key: "choose-a-long-random-string"
bind_window_s: 300
extra_locations:
  - "Shelf: Office Supply Closet"
profile_overrides:
  - filament: "Generic PETG"
    tray_info_idx: "GFG99"
    nozzle_temp_min: 230
    nozzle_temp_max: 260
```

## Scale setup

In the SpoolmanScale, set the base URL to `http://homeassistant.local:7912` and the
API key to the add-on's `api_key` (sent as `X-API-Key`). If `.local` name resolution
fails on the scale, give Home Assistant a DHCP reservation in your router (Eero) and
use its IP address instead.

## 3dfp key

The 3dfp API key is read from `/config/.3dfp_key` on HAOS (visible in the add-on as
`/homeassistant/.3dfp_key`, mounted read-only). It is never copied or logged. Without
it the add-on runs read-only from its cached snapshot.

## Events

Fired on the Home Assistant event bus: `cargobay_bound`, `cargobay_unbound`,
`cargobay_unbound_load`, `cargobay_profile_missing`, `cargobay_quota_low`.

## Sensors

`sensor.cargobay_spools` and `sensor.cargobay_tray_1` .. `sensor.cargobay_tray_4`.

## Routes callable from Home Assistant

- `POST /cargobay/bind`
- `POST /cargobay/unbind`

Use these from `rest_command`s (for example a dashboard spool picker).
