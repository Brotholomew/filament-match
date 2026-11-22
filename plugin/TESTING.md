# Filament Match — live test checklist

Use this after the plugin files are integrated.

## Install

```sh
cd /path/to/filament-match
./plugin/install.sh
```

This builds `plugin/dist/filament_match.py` and installs it as the single entry
file:

```
~/Library/Application Support/OrcaSlicer/orca_plugins/filament_match/filament_match.py
```

Restart OrcaSlicer. Open **File → Plugins**. The plugin should be listed as
**Filament Match**. Activate it and allow:

- network access (Moonraker/Spoolman/OFD),
- threads if prompted.

Then run **Plugins → Run → Filament Match**.

## Basic flow

1. Open the plugin from **Plugins → Run → Filament Match**. It opens as a
   docked panel beside the sidebar and starts scanning automatically.
2. Confirm the **Printer** row in the collapsed **Settings** tile shows the
   printer selected in OrcaSlicer and its host (for example
   `Snapmaker U1 — 10.0.0.176 — connected`).
3. Optionally open **Settings** and set **Spoolman URL** to
   `https://spoolman.blachut.me`.
4. The first scan runs without a button.
   - If no printer is selected or it is disconnected, a native popup asks you
     to connect one first.
   - On the first printer/Spoolman request Orca may show a permission dialog.
     It can appear behind the Orca window. There is one prompt per base origin,
     not per API path or spool. Approve it; if that first scan is blocked, press
     **↻**. Orca denies the in-flight request while the prompt is open (fail
     closed) and persists the host grant afterwards.
   - OFD matching does not need that permission: the index is read from the
     plugin cache. Vendor presets such as Spectrum PCTG, 3DJake ABS, CC3D PETG
     and eSUN PETG should appear as library matches even when no network request
     is allowed.
5. Confirm one row per loaded toolhead, with the matched preset already chosen
   in that row's preset input. The row should say **Selected — press Apply**;
   Orca's slots must not have changed yet.
6. On the first scan, several viable matches select the best candidate without
   a popup. Pressing **Match** opens the chooser only if it produces more than
   one candidate. The chooser does not appear for a single match and choosing
   a candidate does not apply it.
7. Press the pencil button to open a popup with that preset's settings
   (read-only; the plugin cannot open Orca's native preset editor).
8. Press the chevron to expand the fields used for matching. Edit
   vendor/material/subtype/product/colour/spool ID, then press **Match**. Check
   that only the row selection changes before **Apply** is pressed.
9. Choose a different entry in the preset input; it only changes the selection.
10. Press **Apply** next to **↻**. Every changed row is written to Orca and the
    status changes to **Applied**. Cached OFD presets install without a network
    request; only an uncached preset asks for OFD permission once. Confirm the
    preset selectors in Orca's Filament tab now show those presets.
11. If the printer reports more feeds than the project has filament slots, the
    panel should show **Project has fewer filament slots than the printer**,
    mark the extra rows **No Orca slot**, apply the feeds that fit, and state
    how many slots to add. It must not print one error per extra feed. Add that
    many filaments in Orca's Filament tab, press **↻**, and Apply again.
12. If the project has more slots than the printer reports, the panel should
    state how many extra slots are left unchanged. Remove them in Orca's
    Filament tab if an exact match is wanted; the plugin cannot resize slots.
13. Open **Settings** and press **Clear OFD cache**. The status should say the
    index and preset files were cleared; `history.json` and `settings.json`
    must remain. Press **↻** to download fresh OFD data.
14. On a U1, install an OFD preset whose catalogue parent is
    `Generic PETG @System` or `Generic PLA @System`. The installed local preset
    should inherit the compatible `@U1 0.4 nozzle` variant instead of failing.
15. Expand a feed with a Spoolman spool. The current price should come from the
    chosen preset and the new price should auto-fill from Spoolman
    (`spool.price / (initial_weight / 1000)`).
16. Change the preset input. Current price should reload; new price must stay.
    Press **Fetch price** and confirm only new price changes.
17. Edit new price, press **Save price**, then **Apply**. The preset should be
    updated. For a system preset such as eSUN PETG, a local
    `eSUN PETG (local)` copy should be created and selected.
18. Clear the Spoolman URL and rescan. Fetch price should be disabled with an
    add/fix URL tooltip; manual price + Save + Apply should still work.
19. After applying a price to a system preset, press **Match** on that feed.
    The `(local)` copy should be listed first and, because the original system
    preset also matches, the chooser popup should open. On scan, the local copy
    should be preselected without a popup.
20. Apply a price to a **user preset** (for example `CC3D PETG (custom)`).
    Orca must load a local `CC3D PETG (custom) (local)` copy without a
    "can not find parent" error. Press Match: the custom local copy, the
    original custom preset, and the OFD preset should be candidates, with the
    custom local copy first and the chooser open.
11. Close and reopen Orca, run the action again, and confirm the remembered
    mapping is used on the next scan.

## Panel behavior

- The filament feeds are listed first; **Settings** is a collapsed tile at the
  bottom.
- The plugin appears as a docked panel beside the sidebar, not a floating
  window. Drag it to another edge, resize it, float it, or close it.
- Running **Plugins → Run → Filament Match** again shows the existing panel.
- The panel is hidden while Orca is on Device, Project or Calibration and
  returns with Prepare/Preview.
- If dock panels are unavailable, the plugin falls back to a normal window.

## What to verify in Orca

- The slot number is 1-based and matches T1…T4.
- `begin_update` changes the slot; no crash if the slot count is too small.
- A `_local` bundle preset is selected by its visible name, not `_local/...`.
- History is written to:
  `~/Library/Application Support/OrcaSlicer/orca_plugins/plugin_data/filament_match/history.json`
- Delete that file while Orca is closed to reset history.
- If the printer is offline, the plugin shows an error and does not crash.
- If Spoolman is unreachable, matching still works from printer data.

## OFD library

1. Open **Settings** and enable **Open Filament Database**.
2. Scan a filament with no local match.
3. Pick a library entry in that slot's preset input.
4. Confirm one preset is written under
   `user/<active-folder>/_local/filament_match/filament/` and appears in Orca.
5. Confirm the installed preset is selected.

## Known expected limitations

- The plugin does **not** hook Orca’s native **Sync filaments** action; it reads
  the printer itself. Use the **↻** button to rescan.
- The pencil button opens the plugin’s preset settings popup. Orca’s native
  Filament Settings dialog cannot be opened by a plugin.
- `begin_update` is shipped but undocumented; if it changes, applying a match
  will report an error rather than silently doing nothing.
- Generic Moonraker only reports material/colour/filament ID; vendor/subtype
  may be blank. That is normal.
- Do not run another plugin that also changes filament slots at the same time.

## Logs

If something fails, check:

```
~/Library/Application Support/OrcaSlicer/log/python_*.log
```

and the plugin’s own log in its storage folder if present.

## Mock printer

For testing different feeder counts and filament combinations without the U1,
run `plugin/dev/mock_printer.py` and open `http://127.0.0.1:7125/`. Point the
active Orca printer preset's `print_host` at `127.0.0.1:7125`, then press **↻**
in Filament Match. The four-spool state matching the Spoolman fixtures is
`plugin/dev/mock_states/four_spools.json` (3DJake ABS pink, CC3D PETG pink,
eSUN PETG green, and The Filament PLA Lite White with no Spoolman spool).
See [MOCK_PRINTER.md](MOCK_PRINTER.md).

## Debugging without touching the open project

Use an isolated second Orca instance with a cloned data directory and an
instrumented copy of the plugin. It can dump the real preset bundle and every
match row to JSON. The method and commands are in
[dev/ORCA_DEBUG.md](dev/ORCA_DEBUG.md).
