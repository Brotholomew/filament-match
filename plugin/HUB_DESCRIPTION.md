# Filament Match

**Stop hand-building filament presets.**

With Filament Match, you don't have to manually create a preset for every spool or limit yourself to the Orca presets you already have. It reads the filament data exposed by your printer and finds the best matching preset using the [Open Filament Database](https://openfilamentdatabase.org/) — an open catalogue of filament profiles — alongside your installed Orca presets. OFD matching is enabled by default. Spoolman users can sync prices with their chosen instance.

Filament Match is the review layer between your printer and Orca. It remembers the spool, tag and filament mappings you confirm.

## Highlights

- Reads the filament data exposed by your printer; no printer IP field needed.
- Matches installed Orca presets **and** the Open Filament Database, shows why it matched, and opens a chooser when several presets are viable.
- OFD matching is on by default, so a new spool can get a ready profile without manual creation.
- Scans automatically, but writes nothing to Orca until you press **Apply**.
- Lets you edit vendor, material, subtype, product and colour, then rerun matching with **Match**.
- Optional Spoolman enrichment by spool ID and per-kg price sync; manual prices also work.
- Handles slot-count mismatches without silently changing the project.
- Pure Python, no third-party packages, macOS/Windows/Linux.

## No local preset? No problem.

The Open Filament Database is a community catalogue of filament profiles. Filament Match uses it as a second preset library automatically. If no installed preset is a strong match, the plugin proposes the best OFD profile, shows brand and product metadata, and installs it into Orca with **Apply**. You are not limited to the presets already on your machine, and you do not have to create one by hand for new filament.

## How to install

### Use the Orca Plugin Hub

1. In OrcaSlicer, sign in to Orca Cloud with the same account you use at [cloud.orcaslicer.com](https://cloud.orcaslicer.com).
2. Open **File → Plugins**, select the **Plugin Hub** tab, and search for **Filament Match**.
3. Click **Subscribe**, then enable **Activate** for the plugin.
4. Restart OrcaSlicer if the plugin does not appear immediately. Open **File → Plugins → Filament Match → Run plugin**, or use the Speed Dial.

Orca Cloud plugins are a beta feature. Filament Match requires OrcaSlicer `2.5.2` or newer.

### Install from source

The source, tests and full documentation are at [github.com/Brotholomew/filament-match](https://github.com/Brotholomew/filament-match).

macOS and Linux:

```sh
git clone git@github.com:Brotholomew/filament-match.git
cd filament-match
./plugin/install.sh
```

On Linux, pass `"$HOME/.config/OrcaSlicer"` to the installer if it is not the default. On Windows, build `plugin/dist/filament_match.py` with `python plugin\build_single_file.py` and copy it to `%APPDATA%\OrcaSlicer\orca_plugins\filament_match\`. Restart Orca afterwards.

Do not enable the local development copy and the Orca Cloud subscription at the same time; both would auto-load.

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

6. You may have to approve the printer permission checks again.

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

Scanning selects the best candidate; choosing a preset changes the selection only. **Match** reruns matching from the edited fields and ignores confirmed history. **Apply** writes the selection to Orca, remembers confirmed mappings, and installs a lone OFD candidate. The plugin does not hook Orca's native **Sync filaments** button.

## Filament prices

Expand a feed to see **Current price (per kg)**, **New price (per kg)**, **Fetch price** and **Save price**.

- **Current price** is the chosen preset's `filament_cost`.
- **Fetch price** reads the Spoolman spool and converts the whole-spool price to Orca's per-kg price: `(spool.price ?? filament.price) / ((spool.initial_weight ?? filament.weight) / 1000)`.
- **Save price** stages the new price; **Apply** writes it into the preset.
- Spoolman prices load into **New price** on the first scan. A rescan preserves an edited or fetched value; changing the preset reloads **Current price** but leaves **New price** alone.
- If Spoolman is blank, invalid or unreachable, Fetch is disabled; manual prices still work.

**Changes are applied only after you press `Save price` and then `Apply` at the top of the plugin pane.** Without Spoolman, type a price into **New price** and save it the same way.

Orca's plugin API cannot save an existing preset: plugin-owned presets are updated in place, a system preset gets a `<preset base> (local)` copy, and a user preset is flattened into the copy and inherits its system parent. If several feeds share a preset, the last price wins. The matcher ignores the `_local/<bundle>/` prefix and the labels `(local)`, `(custom)`, `(OFD)` and `(copy)`, so custom/local copies are exact candidates; a `(local)` price copy ranks first. Prices do not participate in matching.

## Spoolman

Spoolman is optional. The printer supplies slot data and often a spool ID; Spoolman fills vendor, product, material, colour and temperatures when the printer does not. An `orca_filament_preset` or `orca_preset` extra can hint a preset, and a `variant` extra fills the subtype (for example `SnapSpeed`); a printer-reported subtype wins. Spoolman does not provide Orca's preset list or perform matching.

## Matching order

1. A confirmed mapping for the exact spool/tag/filament identity, while the preset still fits the current vendor/material/subtype/product and colour.
2. Exact printer-reported Orca `filament_id`, then exact vendor + material + subtype + product.
3. A fuzzy name/colour match above a confidence threshold.
4. An Open Filament Database candidate (on by default), then the exact compatible `Generic <material>` preset.

History survives restarts and plugin reinstalls. Only user-confirmed matches are recorded.

## Slot count mismatch

OrcaSlicer 2.5.2 has no plugin API to add, remove or resize project filament slots, so the plugin never silently changes Orca's slot count. Extra printer feeds are shown as **No Orca slot** and skipped by **Apply**, with the number of slots to add. Extra project slots are left untouched, with the number to remove. Add or remove filaments in Orca's **Filament** tab, then press **↻**.

## Limitations

- Orca's plugin API cannot add a button next to native **Synchronize filament list from AMS**, override native AMS sync, or open the native Filament Settings dialog; the plugin uses a docked panel and its own settings popup.
- `begin_update` is undocumented; apply reports failures instead of silently ignoring them.
- System presets may come from `.opc` caches, so the plugin does not rely on their JSON or `filament_id`.
- Generic Moonraker omits vendor/subtype; Spoolman or a richer printer API can fill them.
- Do not run another plugin that changes filament slots at the same time.

## Troubleshooting

**The plugin does not appear.** Restart Orca, open **File → Plugins**, and check that **Filament Match** is enabled. A source install must contain exactly one direct `.py` file: `orca_plugins/filament_match/filament_match.py`.

**A network request was denied.** Orca asks once per base origin: the printer address, the Spoolman address and the OFD. Approve the prompt and press **↻**.

**An old version keeps loading.** Older source installs sometimes left `orca_plugins/filament_match.py/`, which shadows the real folder. Remove it and restart Orca; current installers do this automatically.
