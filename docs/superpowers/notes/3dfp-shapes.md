# 3dfp response shapes (captured 2026-11-21)

One live `GET /api/v1/my/spools` (quota cost 1). Field names and types only; no values from
the account are recorded here (the repo is public).

## Envelope and headers

- Body: `{"data": [<spool>, ...]}`. No `next`/`has_more`/`total`/`meta` keys present.
- Headers: `x-ratelimit-limit: 1000`, `x-ratelimit-remaining: <int>` (HTTP/2, lower-case names;
  read them case-insensitively).

## List item (`<spool>`)

| 3dfp key | JSON type | Feeds `Remote` field |
|---|---|---|
| `short_code` | string | `short_code` |
| `brand` | string | `brand` (`"Bambu Lab"` for Bambu reels) |
| `material` | string, e.g. `PLA`, `PETG`, `ASA`, `TPU` | `material` |
| `material_type` | string, e.g. `Basic`, `Matte`, `Silk+`, `Translucent`, `95A HF/High Speed` | part of `name` |
| `color` | string, colour name, Bambu reels add ` (<code>)` | `color_name` |
| `rgb` | string `#RRGGBB` | `color_hex` (strip `#`, upper) |
| `location` | string; `"Shelf"` when unset | `location` (`"Shelf"` → `None`) |
| `remaining_grams` | int | `remaining_grams`; `0` ⇒ `archived` |
| `empty_spool_weight` | int (grams) | `empty_spool_weight` (new field, tare fallback) |
| `filament_id` | int | `filament_key = str(filament_id)` |
| `spool_properties.bambu_tray_uuid` | string, 32 hex; present only on paired Bambu reels | `bambu_tray_uuid` (new field) |
| `spool_properties.bambu_tag_uid` | string, 16 hex | not used |
| `spool_properties.last_dried_date` | string or null | not used (scale's `last_dried` stays local) |
| `spool_properties.spool_type`, `purchase_*` | strings/numbers | not used |
| `notes`, `td_value`, `spool_url`, `updated_at` | various | not used |

## Not present in 3dfp

- No archived/empty flag: a spool in 3dfp's Empty state is listed with `remaining_grams: 0`.
  So `archived` ⇔ `remaining_grams == 0`, and "revive" is just writing a weight above 0.
- No net (full-spool) weight → `initial_weight` = local override, else 1000.
- No nozzle temperatures → always the generic profile table's.
- No separate filament name → display name `"<material> <material_type> <color without (code)>"`.

## Not captured (deliberately)

- `GET /my/spools/{short_code}` detail: not needed, the list carries `bambu_tray_uuid`.
- PATCH answer body: Cargo Bay must not depend on it; it answers the scale from its own cache
  after a 2xx.
- filament_sync's cache file: not needed (tray UUID → spool comes from the list itself).
