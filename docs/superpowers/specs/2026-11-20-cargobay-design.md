# Cargo Bay — design

_2026-11-20. Status: approved 2026-11-21 (owner). §3/§6 updated 2026-11-21 with the `.local` spike result._

Cargo Bay is a bridge that lets an off-the-shelf **SpoolmanScale** (ESP32 filament
scale with NFC) run against **3dfilamentprofiles.com (3dfp)** as its inventory, and
that tells the **Bambu P2S "LaForge" + AMS 2 Pro** which third-party spool just went
into which slot, through **Home Assistant**. It ships as an HAOS add-on.

## 1. Intent and why

Bambu RFID reels already sync to 3dfp (`homeassistant/bin/filament_sync.py`, #207):
the AMS reports their remaining % and the script writes grams + slot location.
**Third-party spools get none of that.** The AMS has no scale; it estimates remaining
filament only from a Bambu RFID tag plus fed length, so a third-party tray reports
`remain = -1`, no `tray_uuid`, and `filament_sync.py` skips it. Their profile on the
printer has to be set by hand, and their 3dfp weight drifts.

The flow we want:

1. Put the spool on the scale. It reads the NFC sticker and weighs it.
2. Put the spool in an AMS slot.
3. The bridge works out "that spool is now in slot 2", sets slot 2's filament profile
   on the printer (material, colour, temps), writes the measured weight and the slot
   location to 3dfp.
4. Taking the spool out and weighing it again writes the true remaining weight and
   puts it back on the shelf in 3dfp.

## 2. Goals, non-goals, anti-goals

**Goals (v1)**
- SpoolmanScale firmware **unmodified** (released v0.8.3+) talks to Cargo Bay as if it
  were Spoolman (SpoolmanScale-Integration **Part 1: Inventory, contract v1**).
- 3dfp is the **only** inventory of spools, filaments, weights and locations.
- NFC tags (any NTAG/MIFARE UID) link to 3dfp spools; one spool may carry two tags.
- Weighing on the scale writes an absolute `remaining_grams` to 3dfp.
- Scan-then-load binds a spool to an AMS tray; the bridge calls ha-bambulab
  `bambu_lab.set_filament` for that tray and writes the slot location to 3dfp.
- Unloading a cargobay-bound tray releases the binding and shelves the spool in 3dfp.
- Passes `check/check_inventory.py` (MUST items + 12–14) from SpoolmanScale-Integration.

**Non-goals (v1)**
- Per-print usage deduction while a spool is loaded. v1 records "last weighed";
  3dfp's figure is right at load and at unload, stale in between. (See §9, later.)
- Creating filaments/spools from the scale (Part 1 §6.4 filament database, §6.5 create).
  New spools are added on 3dfp first, then linked at the scale.
- Bambu RFID reels. `filament_sync.py` keeps owning them unchanged.
- Labels, drying log beyond storing `extra.last_dried`, multiple printers, AMS HT,
  external spool holder.
- Forking or modifying SpoolmanScale firmware (GPL-3; we only speak its HTTP spec).

**Anti-goals (failure even if it "works")**
- **A second inventory.** Cargo Bay's SQLite holds only what 3dfp has no field for
  (int ids, tag UIDs, tare, tray bindings, extras). Never a weight or location of record.
- **Burning the 3dfp quota.** 1,000 requests/month, 4xx count. No polling of 3dfp; the
  scale's 30 s health poll and list reads are answered locally.
- **Touching an RFID tray.** A tray with a real `tray_uuid` is `filament_sync.py`'s.
- **Delta writes.** Weight writes are absolute and idempotent (same rule as #207).
- **Guessing a binding** from colour/material. A binding comes only from scan-then-load
  or an explicit pick (spec Part 2 §5: "`spool_id` is the server's assignment").

## 3. Constraints and facts found

- **SpoolmanScale** (github.com/Niko11111/SpoolmanScale, GPL-3, v0.8.3 released
  2026-10-25). Hardware: WT32-SC01 Plus (ESP32-S3 + 3.5" touch), PN532, NAU7802,
  YZC-133 2 kg load cell; enclosure on MakerWorld #2713675. Speaks HTTP to
  `<base>/api/v1/...`, no redirects, TLS 1.2, `Accept-Encoding: identity`,
  `User-Agent: ESP32HTTPClient`, key as `X-API-Key` / Bearer / Basic. Base URL ≤ 79 chars.
- **Part 2 (AMS source) is a draft and not in any released firmware.** In v0.8.3 the
  on-scale AMS view exists only for the FilaMan/BamBuddy backends. So v1 cannot assign
  trays from the scale; binding is done on the HA side (§5.3). Part 2 is phase 2.
- **3dfp API** (`https://3dfilamentprofiles.com/api/v1`, as used by `filament_sync.py`):
  `GET /my/spools` (whole list, refuses pagination), `GET/PATCH /my/spools/{short_code}`,
  `GET /filaments?...`, `POST /my/spools`. Spool ids are string `short_code`s; fields
  include `remaining_grams`, `location`, `notes`, a filament with brand/name/colour.
  Slot locations are `"Printer: Bambu P2S, AMS: 1-<slot>"`; the shelf is
  `"Shelf: Office Supply Closet"`; null reads back as "Shelf". `remaining_grams: 0`
  triggers 3dfp's Empty workflow. Key lives in root-only `/config/.3dfp_key`.
- **ha-bambulab** (HACS, not repo-managed). Tray sensors
  `sensor.laforge_ams_2_pro_tray_1..4` with attributes `tray_uuid`, `tag_uid`,
  `filament_id`, `color`, `remain`, `remain_enabled`, `tray_weight`, `name`, `empty`.
  Action `bambu_lab.set_filament(entity_id, tray_info_idx, tray_color RRGGBBAA,
  tray_type, nozzle_temp_min, nozzle_temp_max)`.
- **HAOS DHCP churn.** The box's LAN IP changes on reboot, so the scale must not be
  given an IP. **`.local` spike (2026-11-21):** a Freenove ESP32-S3 on SpoolmanScale
  v0.8.3's exact platform pin (pioarduino 55.03.312-1 → Arduino core 3.3.12, IDF
  v5.5.5) on the house Eero network: the core ships with
  `CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES=y`; `WiFi.hostByName("homeassistant.local")`
  resolves (≈215 ms cold) and `HTTPClient` `GET http://homeassistant.local:8123/` →
  200, both with and without an mDNS responder running on the board (the scale runs
  one by default). The firmware resolves hosts with `WiFi.hostByName`
  (`src/web/web_access.cpp`). Not yet confirmed on the real WT32-SC01 Plus (§7a M3).

## 4. Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A (picked)** | **HAOS add-on**: Python service + SQLite, speaks Part 1 to the scale, backs onto 3dfp, talks to HA core over the Supervisor websocket. | One new service, but always on with HA, no extra box, the repo is the add-on repo. Natural future home for `filament_sync.py`. |
| B | Run real Spoolman (+ OpenSpoolMan for AMS) and mirror Spoolman → 3dfp. | Zero scale-side work, but two inventories that drift; 3dfp stops being the source of truth. |
| C | HA custom component serving the API via `HomeAssistantView`. | No new service, but every change needs an HA restart and it's another non-CI-managed component. |

Louis picked **A**, repo name **`cargobay`** (Star Trek naming: Janeway, Sisko, LaForge;
the cargo bay is where stores are kept — and the AMS has bays).

## 5. Design

### 5.1 Components

```
SpoolmanScale ──HTTP (Part 1)──▶ Cargo Bay add-on ──HTTPS──▶ 3dfp API
                                   │   ▲
                                   │   └── SQLite (/data/cargobay.db)
                                   ▼
                      HA core websocket (Supervisor token)
                      · subscribe sensor.laforge_ams_2_pro_tray_*
                      · call bambu_lab.set_filament
                      · fire cargobay_* events / notify
```

Python 3.12, one asyncio process (aiohttp server + HA websocket client). The add-on
maps `/config` read-only only to read `/config/.3dfp_key` (no second copy of the key).

### 5.2 Part 1 inventory, backed by 3dfp

Endpoints implemented: everything in Part 1 §1 MUST, plus §6.1 measure, §6.2 list
stamp, §6.3 locations. §6.4/6.5 answer `404` (no database / no create). §7.2 copy
answers `404`.

**Id mapping.** SQLite table `spool(id INTEGER PK AUTOINCREMENT, short_code UNIQUE,
spool_weight, tags…)`. A 3dfp `short_code` gets an int the first time it is seen and
keeps it forever (ids never reused, so the §6.2 stamp works). Filament and vendor ids
are mapped the same way from 3dfp's filament id / brand.

**Spool object** (Part 1 §4.4) is assembled from the cached 3dfp spool + SQLite:
`remaining_weight` ← `remaining_grams`; `initial_weight` ← filament net weight if 3dfp
has it, else 1000; `spool_weight` ← SQLite tare (scale writes it via PATCH); `location`
← 3dfp location; `archived` ← 3dfp empty/archived state; `tags` ← SQLite; `extra` ←
SQLite key/values; `filament.{name, material, color_hex (no #), vendor.name}` ← 3dfp.

**Writes.**
- `PATCH remaining_weight` / `PUT measure` → 3dfp `PATCH {remaining_grams}` (absolute,
  rounded). `measure` subtracts exactly the tare it reports.
- `PATCH location` → 3dfp `PATCH {location}` (null allowed).
- `PATCH archived:true, remaining_weight:0` → 3dfp `remaining_grams: 0`.
- `PATCH spool_weight`, `extra`, tag link/unlink → SQLite only; no 3dfp call.
- A write is answered with the updated spool built from the local copy after the
  3dfp `PATCH` succeeds. 3dfp failure → `502`-class error to the scale (it shows a
  failed write); never a fake `200`.

**Bambu reels (§5.2a, builder-check call).** Bambu reels are listed (so the scale
recognises them and the inventory looks complete) but weight writes to a spool that
`filament_sync.py` currently has in an AMS slot are refused (`409`, the scale shows a
failed write): filament_sync owns that figure while loaded. On the shelf, weighing a
Bambu reel writes normally (filament_sync only ever lowers a settled spool, so a real
weighing is not undone). A scanned Bambu tray UUID auto-matches 3dfp's `bambu_spool_id`.

**Locations (§6.3).** `GET /location` lists shelf/storage locations only, never the
AMS slot locations: a slot is entered only by a binding (§5.3), never by a location
write. A PATCH to a slot location answers `409`.

**Archived.** `archived` ↔ 3dfp's empty state. If the builder finds 3dfp cannot revive
an Empty spool over the API, a revive answers `409` and is done on 3dfp; no local
archived flag (anti-goal: second inventory).

**Tags (Part 1 §5.1 native).** `tag(uid_norm PK, spool_id, format)`; UIDs normalised
(hex only, upper). `POST /tag/scan` never writes; `409` with `spool_id` when linking a
tag owned by another spool. Every scan is also recorded as the **pending scan** (§5.3).

**Cache and quota.** The 3dfp spool list is held in memory + SQLite snapshot.
Refreshed: at start; at most once per 6 h when the scale asks for a list; and at most
once per 10 min on a tag scan that resolves to a spool missing from the cache. _(Builder
-check call:)_ **not** on filament_sync's `filament_inventory` event — it carries only
per-filament totals, so a refresh there would cost a GET per print/tray change/HA start.
The only staleness that leaves is locations/weights filament_sync writes for Bambu reels,
which Cargo Bay treats as read-only anyway (§5.2a). Own
writes are applied to the cache locally. Health/info/stamp/list/scan never call 3dfp.
A request counter persisted in SQLite; below 100 remaining for the month → HA push,
refresh stops, writes continue.

Budget (expected): ~4 list refreshes/day ≈ 120/month + ~2 writes per spool load/unload
cycle. Shared with `filament_sync.py` (~1 GET + ≤4 PATCH per print).

### 5.3 Binding a spool to a tray (scan-then-load)

- **Pending scan:** the last spool resolved by `/tag/scan` or measured, with a
  timestamp. It expires after **5 min** (option `bind_window_s`). _(Builder-check
  call, swap safety:)_ a scan/measure arms it **only** for a spool that is not bound
  now and was not unbound in the last `bind_window_s` — so weighing spool A right after
  pulling it from slot 2 never arms A for the next load. Any unload event clears a
  pending scan armed *before* that unload (a swap restarts the sequence: unload, weigh
  A, scan B, load B). A newer scan replaces an older one.
- Cargo Bay subscribes to the four tray sensors. A **load event** = a tray going from
  `empty: true` to `empty: false` **and** carrying no real `tray_uuid` (all zeros/empty).
  An RFID load is ignored (filament_sync's).
- Load event + live pending scan → **bind** that spool to that tray, consume the
  pending scan. Without a pending scan → fire `cargobay_unbound_load` (HA can push
  "Which spool is in slot 3?") and do nothing else.
- **On bind:**
  1. Release any previous binding of that spool or that tray (one spool per tray, one
     tray per spool).
  2. `bambu_lab.set_filament` on the tray: `tray_info_idx` from a material → Bambu
     generic profile table (`GFL99` Generic PLA, `GFG99` Generic PETG, `GFB99` Generic
     ABS, … vendored JSON, overridable per 3dfp filament in add-on options), colour
     from 3dfp (`RRGGBB` + `FF`), `tray_type` = material, nozzle temps from 3dfp if it
     has them else the table's defaults.
  3. 3dfp `PATCH {location: "Printer: Bambu P2S, AMS: 1-<slot>"}` (weight was already
     written by the weighing).
  4. Fire `cargobay_bound {slot, spool_id, short_code, name}`.
- **Unload event** (bound tray goes `empty: true`, or an RFID reel appears in it):
  drop the binding; if the tray emptied, 3dfp `PATCH {location: "Shelf: Office Supply
  Closet"}` (same fallback-to-null rule as #214). If an RFID reel replaced it,
  `filament_sync.py` already shelves the stale spool via its location_conflict path —
  Cargo Bay just drops the binding and does not write.
- **Manual bind/unbind:** an HA-callable path for the dashboard fallback. The add-on
  exposes `POST /cargobay/bind {spool_id|short_code, slot}` and `/cargobay/unbind
  {slot}` (same key), called from an HA `rest_command`; same bind steps.
- Bindings persist in SQLite and are reconciled on start against live tray state
  (bound tray now empty or RFID → unbind silently).
- Never bind across an HA reconnect glitch: `unavailable/unknown → loaded` is not a
  load event (same guard as `filament_sync_tray_change`).

### 5.4 HA side (lives in the `homeassistant` repo; in v1 scope, its own PR)

- Cargo Bay publishes `sensor.cargobay_spools` over the websocket (state = count,
  attribute = id/short_code/name/colour/grams of non-archived, non-Bambu spools, from
  its cache — zero 3dfp calls) and `sensor.cargobay_tray_<n>` (bound spool or none).
- `rest_command.cargobay_bind/unbind` + a tray-picker on the 3D Printer dashboard
  (pick a spool from `sensor.cargobay_spools` for a slot) as the fallback when no scan
  preceded a load.
- Automation: `cargobay_unbound_load` → push "Which spool is in slot N?" with a link
  to the picker. `cargobay_bound` → short confirmation push (or none; owner's call).
- Docs: `docs/3d-printer.md` user page; `agent-docs/printer.md` §39 note that
  third-party trays are Cargo Bay's.

## 6. Deployment

- Repo `~/code/cargobay` is a custom **HAOS add-on repository** (`repository.yaml` +
  `cargobay/config.yaml`, Dockerfile on the HA base Python image). Installed via
  Settings → Add-ons → Repositories.
- Port `7912/tcp` mapped on the host. Options: `api_key` (scale's `X-API-Key`),
  `bind_window_s`, profile overrides. HA access via `homeassistant_api: true`.
- **Stable address for the scale:** base URL `http://homeassistant.local:7912`
  (mDNS; §3 spike). Fallback if `.local` proves flaky on the real scale: an Eero DHCP
  reservation for the HAOS box and `http://<reserved-ip>:7912`.
- Updates: bump `version` in `config.yaml`, push, update in the add-on store.

## 7. Testing

- Unit + HTTP tests (pytest) against a **fake 3dfp** (records every request, so quota
  behaviour is asserted: health/list/scan make zero 3dfp calls).
- Run SpoolmanScale-Integration's `check/check_inventory.py` (vendored, MIT) against the
  service + fake 3dfp in CI; MUST items must pass, plus 12–14.
- Binding tests drive a fake HA websocket: scan→load binds and calls `set_filament`
  with the right args; load without scan fires `cargobay_unbound_load`; RFID load is
  ignored; expiry; reconnect glitch; restart reconciliation.
- End-to-end once on the real scale before calling v1 done.

### 7a. Milestones (hardware not bought yet, 2026-11-20)

1. **Bridge without a scale.** Service, fake 3dfp, fake HA, checker passing in CI. The
   vendored `check_inventory.py` stands in for the scale (it sends the scale's user agent,
   TLS 1.2 and request shapes).
2. **Bridge on HAOS against real 3dfp + real AMS.** Install the add-on; drive it with the
   checker (read-only mode first, then `--write` on two spare spools) and with curl
   standing in for scan/measure; real scan-then-load on a real tray; HA picker PR.
3. **Hardware** (gated on parts arriving): order list §8, print the enclosure, flash via
   the SpoolmanScale web flasher, point the scale at Cargo Bay on
   `homeassistant.local:7912` (Eero DHCP reservation only if `.local` is flaky),
   end-to-end test. v1 is done only after this.

## 8. Hardware (order list)

Per SpoolmanScale README: WT32-SC01 Plus, PN532 (I²C), Adafruit NAU7802, YZC-133 2 kg
load cell, 90° USB-C panel cable, STEMMA QT/JST cables; NTAG215 stickers for spools.
Print the MakerWorld #2713675 enclosure on LaForge. The Flipper Zero is optional: handy
to read/verify a sticker's UID, not part of the flow.

## 9. Later (not v1)

- Part 2 AMS source endpoints so trays are picked on the scale, once firmware ships it.
- Per-print usage deduction for bound trays (needs per-tray usage from ha-bambulab or
  slicer weights per filament), keeping writes absolute.
- Part 1 §6.4/6.5 using 3dfp's filament catalogue, so new spools are created on the scale.
- Fold `filament_sync.py` into Cargo Bay so one process owns all 3dfp traffic and quota.

## 10. Left to the builder

- Exact 3dfp field names for filament net weight, colour, temps, empty/archived state
  (read a live `GET /my/spools` once; quota cost 1).
- Generic profile table contents (ha-bambulab ships `filaments_detail.json` on HAOS).
- Library choices inside the add-on, SQLite schema details, logging.
