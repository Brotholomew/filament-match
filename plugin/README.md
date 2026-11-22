# Filament Match

An OrcaSlicer 2.5+ plugin that reads the filaments loaded in a printer, finds
the best matching preset for each slot from your installed Orca presets and the
[Open Filament Database](https://openfilamentdatabase.org/), lets you
review/edit the detected fields and the match, and remembers confirmed
spool/tag/filament → preset mappings.

OFD matching is enabled by default, so a new filament does not need a hand-built
preset. Spoolman is optional enrichment and price sync, not a requirement.

## Install

```sh
cd /path/to/filament-match
./plugin/install.sh
```

The script builds one file (`plugin/dist/filament_match.py`) and installs it as:

```
~/Library/Application Support/OrcaSlicer/orca_plugins/filament_match/filament_match.py
```

Restart OrcaSlicer, open **File → Plugins**, enable **Filament Match**, allow
network access when Orca asks, then run **Plugins → Run → Filament Match**.
The plugin opens as a docked panel beside the sidebar; drag it, float it, or
close it like Orca's own panels. (On hosts without dock panels it falls back to
a normal window.) Orca asks once per base origin — the printer address, the Spoolman
address and the Open Filament Database — rather than once per API path or
spool. The in-flight request
may be denied while the dialog is open; approve it and press **↻**.

For Windows/Linux paths and manual install, see [INSTALL.md](INSTALL.md).

## Use

1. Select and connect the printer in OrcaSlicer (Device tab, or a printer preset
   with a configured print host). The plugin reads the address from Orca; there
   is no IP field.
2. Enable the plugin once. From then on it opens a docked panel and scans
   automatically when the panel starts.
3. The **Settings** tile is collapsed at the bottom. Open it to set the optional
   **Spoolman URL** or Open Filament Database options.
4. Review each filament feed:
   - scanning only **selects** a preset; it does not write anything to Orca,
   - the preset input shows that selection; choosing another entry changes the
     selection only,
   - the pencil button opens that preset’s settings in a popup,
   - the chevron expands the fields used for matching,
   - **Match** at the bottom re-runs matching from the edited fields and changes
     the selection only.
5. Press **Apply** next to **↻** to write every changed selection to Orca and
   remember the confirmed spool/tag/filament mappings. Apply installs and
   selects library candidates when needed. If one row fails, the other rows are
   still applied and the failure is shown on that row.
6. On the first scan, the best candidate is selected even when several are
   plausible; no popup opens. If **Match** produces more than one viable
   candidate, a chooser popup opens; choosing one only changes the selection.
   A lone OFD candidate is preselected as **Library match**; press **Apply** to
   install it.
7. If the printer or presets change, use the small **↻** button at the top to
   scan again.

The plugin does **not** hook Orca's native “Sync filaments” button; it reads the
printer itself and leaves Orca's native button untouched.

## Matching order

1. A user-confirmed mapping for the exact spool/tag/filament identity, but only
   if the mapped preset still fits the current vendor/material/subtype/product
   (and colour when the preset has one). A stale refill mapping falls through.
2. Exact printer-reported Orca `filament_id` among compatible installed presets.
3. Exact vendor + material + subtype + product. A product text that is only a
   colour (`Yellow`), only the vendor/material (`PETG Pink`), or the material
   plus a grade word (`PETG Basic`, `Premium PCTG`) does not count as a product
   constraint; meaningful names such as `ABS Pastel` or `PETG Matte` still do.
4. A fuzzy name/colour match above a confidence threshold.
   Vendor presets that carry the variant only in their name (for example
   `Snapmaker PLA SnapSpeed`) are compared by the name tokens when the preset
   has no explicit subtype setting.
5. An Open Filament Database candidate (enabled by default). The index is cached locally;
   matching uses it without a network request, so vendor presets still appear
   when Orca's network permission is denied. Applying a candidate uses a cached
   preset JSON when available and fetches one preset otherwise.
6. The exact compatible `Generic <material>` preset, not a variant such as
   `Generic PLA-CF`, unless no exact generic material preset exists.

History is stored in Orca's per-plugin data folder:

```
~/Library/Application Support/OrcaSlicer/orca_plugins/plugin_data/filament_match/history.json
```

It survives restarts and plugin reinstalls. Only user-confirmed matches are
recorded; automatic generic fallbacks are not.

The explicit **Match** button deliberately ignores history. It re-evaluates
from the fields in the expanded section, so editing a previously applied feed
can produce a different candidate and, when several candidates remain, the
chooser popup.

## Slot count mismatch

OrcaSlicer 2.5.2 has no plugin API to add, remove or resize project filament
slots. The plugin therefore never silently changes Orca's slot count:

- if the printer reports more feeds than the project has slots, the extra feeds
  are shown as **No Orca slot** and are skipped by **Apply**; the panel shows
  exactly how many slots to add;
- if the project has more slots than the printer reports, the extra project
  slots are left untouched; the panel shows how many to remove.

To sync all feeds, add or remove the stated number of filaments in Orca's
**Filament** tab, then press **↻**. The plugin then applies every feed that has a
slot. A mismatch no longer produces one error per extra feed.

## Filament prices

Expand a feed to see **Current price (per kg)**, **New price (per kg)**,
**Fetch price** and **Save price** after the Match button.

- **Current price** is the chosen preset’s `filament_cost`.
- **Fetch price** reads the feed’s Spoolman spool and converts the whole-spool
  price to Orca’s per-kg price:
  `price / (initial_weight / 1000)`, with the filament price/weight as fallback.
- **Save price** stages the new price. **Apply** writes it into the preset.
- On the first scan, Spoolman prices are loaded into **New price**
  automatically. A rescan preserves a value you edited or fetched.
- Changing the chosen preset reloads **Current price** but leaves **New price**
  alone.
- If Spoolman is blank, invalid or unreachable, Fetch is disabled with a
  tooltip; manual new prices still work.
- A feed without a Spoolman spool (for example The Filament) can still use a
  manual price.

Plugin-owned local presets are updated in place. A system message gets a local
copy named `<preset base> (local)` inheriting the system preset. A **user
preset** is flattened into the copy and inherits its system parent, because
Orca's local bundle loader cannot resolve a user-preset parent and silently
skips such a copy.

The matcher normalizes Orca's `_local/<bundle>/` prefix and the labels
`(local)`, `(custom)`, `(OFD)` and `(copy)`, so custom/local copies are exact
candidates. A `(local)` price copy ranks before its original. If several
presets match, **Match** opens the chooser; scan preselects the local copy. This is necessary because Orca’s
plugin API cannot save an existing preset. If multiple feeds share a preset,
the last applied price wins.

The price fields are UI/preset metadata only; they do not participate in
filament matching.

## OFD cache

OFD matching is enabled by default. To match only installed Orca presets, clear
**Open Filament Database** in Settings. The **Settings** tile has **Clear OFD
cache**. It deletes only the local OFD
index (`ofd_index.json`) and cached preset JSON (`ofd_presets/`); history,
settings and installed presets are kept. The next **↻** downloads a fresh index
and whichever preset is needed.

When an OFD preset inherits a system generic that the printer shadows (for
example `Generic PETG @System` is incompatible on a U1 because
`Generic PETG @U1 0.4 nozzle` is the active variant), the plugin now remaps the
parent to the compatible same-base, same-material variant before installing it.
The OFD preset itself keeps its name and settings.

## Spoolman

Spoolman is optional enrichment by spool ID:

- the printer supplies slot data and often a spool ID;
- Spoolman fills vendor, product name, material, colour and temperatures when
  the printer does not;
- a Spoolman extra field (`orca_filament_preset` / `orca_preset`) can be used as
  an explicit preset hint;
- a Spoolman `variant` extra fills the subtype when the printer did not report
  one (for example `SnapSpeed`). A printer-reported subtype always wins.

Spoolman does not provide Orca's preset list, does not perform the matching and
is not needed for the plugin to work. The mock printer already includes the
fields directly, so it can be tested with a blank Spoolman URL.

## Limitations

- OrcaSlicer 2.5.2's plugin API cannot add a button to the native filament
  title bar next to **Synchronize filament list from AMS**. The plugin opens a
  docked panel beside the sidebar instead; adding that exact button needs a C++
  change to OrcaSlicer itself.
- Cannot override Orca's native AMS/filament sync; there is no sync-complete
  event or tray-payload plugin API.
- Cannot open Orca's native Filament Settings dialog. The pencil button shows
  the preset's settings in the plugin's popup; edit the preset itself in Orca.
- `begin_update` is shipped but undocumented; applying a match reports failures
  instead of silently ignoring them.
- System presets are often served from `.opc` caches, so the plugin does not
  rely on reading their JSON or on their `filament_id`.
- Generic Moonraker reports material/colour/filament ID, not vendor/subtype;
  those fields stay blank unless Spoolman or a richer printer API supplies them.
- Do not run another plugin that also changes filament slots at the same time.

## Development

```
plugin/
  filament_match.py          # Orca entry, action, UI
  filament_match_core.py     # pure matching/history
  filament_match_sources.py  # printer/Spoolman/OFD adapters
  tests/                     # unittest suites
  dev/mock_printer.py        # editable mock Snapmaker/Moonraker printer
  build_single_file.py       # flatten to one installable .py
  install.sh                 # build + install locally
  PLAN.md                    # design and phases
  API_NOTES.md               # verified Orca 2.5.2 plugin API facts
  TESTING.md                 # live test checklist
  MOCK_PRINTER.md            # mock printer setup and editor
```

Run tests:

```sh
python3 -m unittest discover -s plugin/tests -p 'test_*.py' -v
```

To test feeder counts and filament combinations without a real printer, see
[MOCK_PRINTER.md](MOCK_PRINTER.md).
