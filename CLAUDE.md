# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

**Cargo Bay** — an HAOS add-on that lets an unmodified **SpoolmanScale** (ESP32 NFC
filament scale) use **3dfilamentprofiles.com (3dfp)** as its inventory, and binds
third-party spools to **Bambu P2S "LaForge" + AMS 2 Pro** trays through **Home
Assistant** (sets the tray's filament profile via ha-bambulab, writes weight + slot
location to 3dfp). Star Trek naming: the house AI is Janeway, the media server Sisko,
the printer LaForge.

**Read the design before any work:** `docs/superpowers/specs/2026-11-20-cargobay-design.md`.
Its anti-goals (§2) are load-bearing — don't "improve" them away.

Status (2026-11-21): design approved; implementation plan at
`docs/superpowers/plans/2026-11-21-cargobay-v1.md` (M1+M2; HA side gets its own plan). No
code yet. Scale parts ordered. `.local` spike passed on an ESP32-S3 (design §3), so the scale
uses `http://homeassistant.local:7912`; the Eero DHCP reservation is now only a fallback.

## Sibling repos and external specs

- `~/code/homeassistant` — the HA config repo (CI deploys it to HAOS). Owns
  `bin/filament_sync.py` (Bambu RFID reels → 3dfp, #207; read its
  `agent-docs/printer.md` §39) and will hold Cargo Bay's HA side (dashboard picker,
  rest_commands, automations, user docs `docs/3d-printer.md`). Follow that repo's own
  CLAUDE.md when editing it.
- SpoolmanScale firmware: https://github.com/Niko11111/SpoolmanScale (GPL-3 — we do NOT
  fork or vendor it; we only speak its HTTP spec).
- Integration spec: https://github.com/Niko11111/SpoolmanScale-Integration —
  `common.md`, `inventory/spec.md` (Part 1, contract v1, what we implement),
  `ams-source/spec.md` (Part 2, DRAFT, not in released firmware — phase 2).
  `check/check_inventory.py` (MIT) is our acceptance test.

## Hard-won facts (carried over from the homeassistant repo)

- **3dfp quota: 1,000 requests/month, 4xx count**, shared with `filament_sync.py`.
  Never poll 3dfp; answer the scale's health/list/scan locally from cache.
- **3dfp writes are ABSOLUTE `remaining_grams`**, never deltas. `0` triggers 3dfp's
  Empty workflow. Slot location names: `"Printer: Bambu P2S, AMS: 1-<slot>"`; shelf:
  `"Shelf: Office Supply Closet"`; null reads back as "Shelf". Wrong location names
  422 and burn quota.
- 3dfp API key: root-only file `/config/.3dfp_key` on HAOS. Never log or commit it.
- ha-bambulab is a HACS component (not repo-managed). Trays:
  `sensor.laforge_ams_2_pro_tray_1..4`. Action `bambu_lab.set_filament`.
- Bambu reel identity is `tray_uuid` (shared by its two tags), not `tag_uid`.
  Third-party trays report `remain = -1` and an empty/zero `tray_uuid`.
- HAOS is at `homeassistant.local` / `ssh haos` (root, `~/.ssh/haos_deploy`). **Never
  hardcode its LAN IP** — DHCP changes it every reboot (Eero reservation is a fallback; see design §3/§6).
- A reconnect `unavailable/unknown → real` on a tray sensor is not a load event.

## Working conventions (owner's standing rules)

- **Open a GitHub issue before starting any new piece of work**, close it when it lands.
  Every new issue: `gh issue edit <N> --add-project "Blairmont Smart Home"
  --add-assignee louisalexander`; when work starts, move it to **In Progress** on that
  board (Projects v2 Status field → `gh project item-edit`, project
  `PVT_kwHOAKcBS84BZmio`).
- **Conventional Commits**: `<type>(<scope>): <what changed> [— why]`; types feat, fix,
  docs, refactor, chore, ci, test. Describe the change, not the file.
- Commit/push only when asked; branch off `main` for feature work.
- Any user-visible change also updates the homeassistant repo's MkDocs manual
  (`docs/3d-printer.md`).
