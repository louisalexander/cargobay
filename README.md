# Cargo Bay

A Home Assistant OS add-on that bridges a [SpoolmanScale](https://github.com/Niko11111/SpoolmanScale)
filament scale to [3dfilamentprofiles.com](https://3dfilamentprofiles.com) and a Bambu
Lab AMS: scan and weigh a third-party spool, load it into a slot, and the printer gets
the right filament profile while 3dfp gets the weight and location.

## Install

In Home Assistant: Settings, Add-ons, Add-on store, the three-dot menu, Repositories,
then add `https://github.com/louisalexander/cargobay`. Install **Cargo Bay**, set
`api_key`, and start it. See [`cargobay/DOCS.md`](cargobay/DOCS.md) for options and
scale setup.

Design: [`docs/superpowers/specs/2026-11-20-cargobay-design.md`](docs/superpowers/specs/2026-11-20-cargobay-design.md).
