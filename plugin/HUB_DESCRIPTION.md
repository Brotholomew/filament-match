# Filament Match

Filament Match reads the filament loaded in each printer slot, finds the best matching OrcaSlicer filament preset, lets you review and change the match, and applies the result to your project. Spoolman and the Open Filament Database are optional enrichments, not requirements.

It is a matcher and review layer, not a preset generator. It works with the presets already installed in Orca and remembers the spool, tag and filament mappings you confirm.

## Highlights

- Reads filament data directly from the connected Moonraker or Snapmaker printer. There is no printer IP field; the address comes from Orca.
- Matches every feed to a compatible installed Orca filament preset and shows why it matched.
- Scans automatically when the panel opens, but writes nothing to Orca until you press **Apply**.
- Lets you edit the detected vendor, material, subtype, product and colour, then rerun matching with **Match**.
- Opens a chooser when more than one preset is a viable match.
- Optional Spoolman enrichment by spool ID, including vendor, product, material, colour, temperatures, subtype and per-kg price sync.
- Manual price entry works without Spoolman.
- Uses the Open Filament Database to propose presets for filaments that are not installed, with a local index cache.
- Handles printer/project slot-count mismatches without silently changing the project.
- Remembers confirmed spool/tag/filament mappings across restarts.
- Pure Python, no third-party packages, macOS/Windows/Linux.

## How to install

### Use the Orca Plugin Hub

1. In OrcaSlicer, sign in to Orca Cloud with the same account you use at [cloud.orcaslicer.com](https://cloud.orcaslicer.com).
2. Open **File → Plugins**, select the **Plugin Hub** tab, and search for **Filament Match**.
3. Click **Subscribe**, then enable **Activate** for the plugin.
4. Restart OrcaSlicer if the plugin does not appear immediately. Open **File → Plugins → Filament Match → Run plugin**, or use the Speed Dial.

Orca Cloud plugins are a beta feature. Use a current OrcaSlicer build with plugin support; `2.5.2` is the tested version.

### Install from source

The source, tests and full documentation are at [github.com/Brotholomew/filament-match](https://github.com/Brotholomew/filament-match).

macOS and Linux:

```sh
git clone git@github.com:Brotholomew/filament-match.git
cd filament-match
./plugin/install.sh
```

On Linux, pass your Orca data directory if it is not the default:

```sh
./plugin/install.sh "$HOME/.config/OrcaSlicer"
```

Windows: build `plugin/dist/filament_match.py` with `python plugin\build_single_file.py` and copy it to `%APPDATA%\OrcaSlicer\orca_plugins\filament_match\filament_match.py`, then restart Orca.

Do not enable the local development copy and the Orca Cloud subscription at the same time; they use separate plugin folders and both would auto-load.

## Plugin usage

1. Make sure your printer is connected in Orca:

   ![Select the printer](https://api.orcaslicer.com/api/v1/bundles/media/301c0d71-be42-40b7-b332-646d82dab75c/content)

2. Press the space bar to open the Speed Dial, or navigate to **File → Plugins → Filament Match → Run plugin**:

   ![Open the plugin](https://api.orcaslicer.com/api/v1/bundles/media/3f367b05-4b78-4e08-8a1e-63e49d5d6aac/content)

3. Allow the plugin to start:

   ![Allow the plugin to start](https://api.orcaslicer.com/api/v1/bundles/media/3fa642e9-a24d-4a0a-b36d-4ad1389f45f2/content)

4. Allow the plugin to access your printer's lanes and filament detection:

   ![Printer lane permissions](https://api.orcaslicer.com/api/v1/bundles/media/39c0c681-8b3f-4a45-8b93-b11e290ff89c/content)

   ![Printer filament-detect permissions](https://api.orcaslicer.com/api/v1/bundles/media/3247a107-af41-4073-b0af-2f9a34100323/content)

5. Re-scan the printer after granting access:

   ![Re-scan the printer](https://api.orcaslicer.com/api/v1/bundles/media/35827c85-155a-41e1-bbc6-b85d0b705711/content)

6. You may have to approve the printer permission checks again:

   ![Printer permissions again, step 1](https://api.orcaslicer.com/api/v1/bundles/media/3d8560cd-a5de-48b0-b9ac-e46ede038aa9/content)

   ![Printer permissions again, step 2](https://api.orcaslicer.com/api/v1/bundles/media/37b82423-bfa6-4baf-a71d-fbebbe7e63f4/content)

   ![Printer permissions again, step 3](https://api.orcaslicer.com/api/v1/bundles/media/3ea0ea21-a218-4e2d-8b7a-75145d5cb71e/content)

7. The presets are matched to your printer's spools. Missing presets are fetched from the Open Filament Database. Review the rows and press **Apply** to write the selected presets to Orca:

   ![Apply the chosen presets](https://api.orcaslicer.com/api/v1/bundles/media/399fa13d-da16-41eb-acd8-c30798cbb227/content)

8. You can edit the search metadata used to find presets for a feed:

   ![Edit spool metadata](https://api.orcaslicer.com/api/v1/bundles/media/39534a9e-e1e1-49fa-a072-1b760fb01757/content)

9. If more than one preset matches the metadata, a list of matching presets appears. Choosing one changes the selection only; press **Apply** to use it:

   ![Choose a preset from the list](https://api.orcaslicer.com/api/v1/bundles/media/3a166762-d510-415d-ae1b-87f44401a30f/content)

10. Optionally, connect a Spoolman instance in the **Settings** tile:

    ![Spoolman settings](https://api.orcaslicer.com/api/v1/bundles/media/3645aaf1-45e6-47e1-b828-3220639c9ff8/content)

11. Allow the plugin to fetch data from Spoolman:

    ![Spoolman permissions](https://api.orcaslicer.com/api/v1/bundles/media/3c4c9ddd-ca58-449a-9796-f19d3f51015b/content)

12. You can now fetch price data from Spoolman for a feed:

    ![Spoolman price panel](https://api.orcaslicer.com/api/v1/bundles/media/375c6705-a575-4774-bfb8-baef63d06a76/content)

### What Match and Apply do

- Scanning selects the best candidate in each row. It does not change Orca.
- The preset input shows the current selection. Choosing another entry changes the selection only.
- The pencil button opens that preset's settings in a popup. It does not edit the preset itself.
- The chevron expands the fields used for matching.
- **Match** reruns matching from the edited fields and changes the selection only. It deliberately ignores confirmed history.
- **Apply** writes every changed selection to Orca and remembers the confirmed spool, tag and filament mappings. If one row fails, the other rows are still applied and the failure is shown on that row.
- On the first scan, the best candidate is selected even when several are plausible; no popup opens. If **Match** produces more than one viable candidate, the chooser opens.
- A lone OFD candidate is preselected as **Library match**. Press **Apply** to install it.
- Use the small **↻** button to scan again after changing the printer, spools or presets.

The plugin does not hook Orca's native **Sync filaments** button. It reads the printer itself and leaves Orca's own button untouched.

## Filament prices

Expand a feed to see **Current price (per kg)**, **New price (per kg)**, **Fetch price** and **Save price** after the Match button.

- **Current price** is the chosen preset's `filament_cost`.
- **Fetch price** reads the feed's Spoolman spool and converts the whole-spool price to Orca's per-kg price: `(spool.price ?? filament.price) / ((spool.initial_weight ?? filament.weight) / 1000)`.
- **Save price** stages the new price. **Apply** writes it into the preset.
- On the first scan, Spoolman prices are loaded into **New price** automatically. A rescan preserves a value you edited or fetched.
- Changing the chosen preset reloads **Current price** but leaves **New price** alone.
- If Spoolman is blank, invalid or unreachable, Fetch is disabled with a tooltip; manual new prices still work.

**Changes are applied only after you press `Save price` and then `Apply` at the top of the plugin pane.** If you do not have a Spoolman instance, you can still type a price into **New price** and save it the same way.

Orca's plugin API cannot save an existing preset. Plugin-owned local presets are updated in place. A system preset gets a local copy named `<preset base> (local)` that inherits the system preset. A user preset is flattened into the copy and inherits its system parent, because Orca's local bundle loader cannot resolve a user-preset parent and silently skips such a copy. If multiple feeds share a preset, the last applied price wins.

The matcher ignores Orca's internal `_local/<bundle>/` prefix and the labels `(local)`, `(custom)`, `(OFD)` and `(copy)`, so custom and local copies are exact candidates. A `(local)` price copy ranks before its original. Price fields are metadata only and do not participate in filament matching.

## Spoolman

Spoolman is optional enrichment by spool ID:

- the printer supplies slot data and often a spool ID;
- Spoolman fills vendor, product name, material, colour and temperatures when the printer does not;
- a Spoolman extra field (`orca_filament_preset` or `orca_preset`) can be used as an explicit preset hint;
- a Spoolman `variant` extra fills the subtype when the printer did not report one (for example `SnapSpeed`). A printer-reported subtype always wins.

Spoolman does not provide Orca's preset list, does not perform the matching and is not needed for the plugin to work.

## Matching order

1. A user-confirmed mapping for the exact spool/tag/filament identity, but only if the mapped preset still fits the current vendor/material/subtype/product (and colour when the preset has one). A stale refill mapping falls through.
2. Exact printer-reported Orca `filament_id` among compatible installed presets.
3. Exact vendor + material + subtype + product. Product text that is only a colour, only the vendor/material, or the material plus a grade word does not count as a product constraint. Meaningful names such as `ABS Pastel` or `PETG Matte` still do.
4. A fuzzy name/colour match above a confidence threshold.
5. An optional Open Filament Database candidate.
6. The exact compatible `Generic <material>` preset, not a variant such as `Generic PLA-CF`, unless no exact generic material preset exists.

History is stored in Orca's per-plugin data folder and survives restarts and plugin reinstalls. Only user-confirmed matches are recorded; automatic generic fallbacks are not.

## Slot count mismatch

OrcaSlicer 2.5.2 has no plugin API to add, remove or resize project filament slots, so the plugin never silently changes Orca's slot count:

- if the printer reports more feeds than the project has slots, the extra feeds are shown as **No Orca slot** and skipped by **Apply**; the panel shows exactly how many slots to add;
- if the project has more slots than the printer reports, the extra project slots are left untouched; the panel shows how many to remove.

Add or remove the stated number of filaments in Orca's **Filament** tab, then press **↻**.

## Limitations

- OrcaSlicer's plugin API cannot add a button next to the native **Synchronize filament list from AMS** button. The plugin opens a docked panel instead.
- The plugin cannot override Orca's native AMS/filament sync. There is no sync-complete event or tray-payload plugin API.
- The plugin cannot open Orca's native Filament Settings dialog. The pencil button shows the preset's settings in the plugin's own popup.
- `begin_update` is shipped but undocumented; applying a match reports failures instead of silently ignoring them.
- System presets are often served from `.opc` caches, so the plugin does not rely on reading their JSON or on their `filament_id`.
- Generic Moonraker reports material/colour/filament ID, not vendor/subtype. Those fields stay blank unless Spoolman or a richer printer API supplies them.
- Do not run another plugin that also changes filament slots at the same time.

## Troubleshooting

**The plugin does not appear.** Restart Orca, open **File → Plugins**, and check that **Filament Match** is enabled. If you installed from source, the plugin folder must contain exactly one direct `.py` file: `orca_plugins/filament_match/filament_match.py`.

**A network request was denied.** Orca asks once per base origin: the printer address, the Spoolman address and the OFD. Approve the prompt and press **↻**.

**An old version keeps loading.** Older source installs sometimes left a folder named `orca_plugins/filament_match.py/`. Orca loads that folder instead of `orca_plugins/filament_match/`. Remove the legacy folder and restart Orca. Current versions of `plugin/install.sh` remove it automatically.
