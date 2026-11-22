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
  - filament: "Polymaker PLA Matte Black"
    tray_info_idx: "GFL99"
    nozzle_temp_min: 200
    nozzle_temp_max: 230
```

### Profile overrides

By default a bound spool gets Bambu's generic profile for its material (PLA ->
`GFL99` "Generic PLA", PETG -> `GFG99` "Generic PETG", ...). An override replaces that
for one filament:

- `filament` is matched against the spool's display name, built from the 3dfp fields
  as `"<brand> <material> <material_type> <color>"` (for example
  `"Polymaker PLA Matte Black"`). A trailing parenthesised colour code such as
  `"(10501)"` is dropped, runs of spaces count as one, and the match ignores case.
  Copy the name exactly as it appears in `spools[].name` of `sensor.cargobay_spools`.
  A non-matching name is silently unused.
- `tray_info_idx` is the Bambu filament profile id to set on the tray.
- `nozzle_temp_min` / `nozzle_temp_max` are optional. When omitted, the temperatures
  come from the built-in generic table entry for `tray_info_idx`, or, if that id is
  not in the table, from the generic profile for the spool's material.

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
