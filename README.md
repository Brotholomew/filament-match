# Filament Match

**Stop hand-building filament presets.**

When you use Filament Match, you don't have to manually create a preset for
every spool or limit yourself to the Orca presets you already have. It reads the
filament data exposed by your printer and finds the best matching preset using
the **[Open Filament Database](https://openfilamentdatabase.org/)** — an open
catalogue of community-maintained filament profiles — alongside your installed
Orca presets. OFD matching is **enabled by default**. If you use Spoolman, it can
also sync your price information with your chosen Spoolman instance.

Filament Match is the review layer between your printer and Orca: scan, inspect
the match, edit the metadata if needed, and press **Apply**. It remembers the
spool, tag and filament mappings you confirm.

![Filament Match applies matched presets to the printer slots](assets/8-apply-the-chosen-presets.jpg)

## Features

- Reads filament data directly from the connected Moonraker or Snapmaker
  printer. There is no printer IP field to configure; the address comes from
  Orca.
- Matches every feed to a compatible preset from your installed Orca library
  **and** the Open Filament Database, and shows why it matched.
- OFD matching is on by default, so a new spool does not need a hand-built
  preset before you can use it.
- Scans automatically when the panel opens, but only **selects** candidates.
  Nothing is written to Orca until you press **Apply**.
- Lets you edit the detected vendor, material, subtype, product and colour,
  then rerun matching with **Match**.
- Opens a chooser when more than one preset is a viable match.
- Optionally enriches printer data from Spoolman by spool ID, including vendor,
  product, material, colour, temperatures and subtype.
- Fetches per-kg filament prices from Spoolman and writes them back to Orca
  presets. Manual price entry works without Spoolman.
- Proposes and installs Open Filament Database presets for filaments that are
  not installed, with a local index cache for offline matching.
- Handles printer/project slot-count mismatches without silently changing the
  project or producing an error per extra feed.
- Remembers confirmed spool/tag/filament mappings across restarts and plugin
  reinstalls.
- Pure Python, no third-party packages, works on macOS, Windows and Linux.

## No local preset? No problem.

The [Open Filament Database](https://openfilamentdatabase.org/) is an open
catalogue of filament profiles. Filament Match uses it as a second preset
library automatically: scan the printer, and if no installed preset is a strong
match, the plugin proposes the best OFD profile and can install it into Orca in
one **Apply**. You are not limited to the handful of presets already on your
machine, and you do not have to create a preset by hand just to use a new
filament.

OFD matching is enabled by default. You can turn it off or point the plugin at a
different index in the **Settings** tile.

## How to install

### Use the Orca Plugin Hub

1. In OrcaSlicer, sign in to Orca Cloud with the same account you use at
   [cloud.orcaslicer.com](https://cloud.orcaslicer.com).
2. Open **File → Plugins**, select the **Plugin Hub** tab, and search for
   **Filament Match**.
3. Click **Subscribe**, then enable **Activate** for the plugin.
4. Restart OrcaSlicer if the plugin does not appear immediately. You can then
   run it from **File → Plugins → Filament Match → Run plugin**, from the Speed
   Dial, or with the Speed Dial shortcut.

Orca Cloud plugins are a beta feature. Filament Match requires OrcaSlicer
`2.5.2` or newer.

### Install from source

macOS and Linux:

```sh
git clone git@github.com:Brotholomew/filament-match.git
cd filament-match
./plugin/install.sh
```

The installer detects the default data directory on macOS and Linux. Pass one
explicitly to install into a clone or non-default location:

```sh
./plugin/install.sh "$HOME/Library/Application Support/OrcaSlicer"   # macOS
./plugin/install.sh "$HOME/.config/OrcaSlicer"                       # Linux
```

Windows (PowerShell) needs Python 3.12 or newer to build. Installing a prebuilt
release artifact needs no Python:

```powershell
git clone git@github.com:Brotholomew/filament-match.git
cd filament-match
python --version
python plugin\build_single_file.py

$plugins = Join-Path $env:APPDATA "OrcaSlicer\orca_plugins"
$dest = Join-Path $plugins "filament_match"
$legacy = Join-Path $plugins "filament_match.py"

if (Test-Path $legacy) { Remove-Item -Recurse -Force $legacy }
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Get-ChildItem $dest -File |
  Where-Object { ($_.Extension -in ".py", ".whl") -and $_.Name -ne "filament_match.py" } |
  Remove-Item -Force
Remove-Item -Recurse -Force (Join-Path $dest "__pycache__") -ErrorAction SilentlyContinue
Copy-Item plugin\dist\filament_match.py (Join-Path $dest "filament_match.py") -Force
'{"enabled": true}' | Set-Content -Encoding ascii (Join-Path $dest ".install_state.json")
```

Each stable GitHub release also attaches `filament_match_any.py` and a SHA-256 file.
To install without Python, download both, verify the checksum, and copy the
artifact into the same folder as `filament_match.py`.

Then restart OrcaSlicer, open **File → Plugins**, enable **Filament Match**, and
run it. The plugin opens as a docked panel beside the sidebar; drag it, float it,
or close it like Orca's own panels. On hosts without dock panels it opens a
normal window.

> [!IMPORTANT]
> Do not enable the local development copy and the Orca Cloud subscription at
> the same time. They use separate plugin folders and both would auto-load.
> Remove the local folder if you switch to the Hub version, or unsubscribe if
> you switch back.

For manual installation, uninstall steps and platform details, see
[plugin/INSTALL.md](plugin/INSTALL.md).

## Plugin usage

1. Make sure your printer is connected in Orca:

   ![Select the printer](assets/0-select-printer.jpg)

2. Press the space bar to open the Speed Dial, or navigate to
   **File → Plugins → Filament Match → Run plugin**:

   ![Open the plugin](assets/1-open-plugin.jpg)

3. Allow the plugin to start:

   ![Allow the plugin to start](assets/2-allow-plugin-start.jpg)

4. Allow the plugin to access your printer's lanes and filament detection:

   ![Printer lane permissions](assets/3-permissions-for-printers-lanes.jpg)

   ![Printer filament-detect permissions](assets/5-permissions-for-printers-filament-detect.jpg)

5. Re-scan the printer after granting access:

   ![Re-scan the printer](assets/6-re-scan-printer.jpg)

6. You may have to approve the printer permission checks again:

   ![Printer permissions again, step 1](assets/7-printer-permissions-again1.jpg)

   ![Printer permissions again, step 2](assets/7-printer-permissions-again2.jpg)

   ![Printer permissions again, step 3](assets/7-printer-permissions-again3.jpg)

7. The presets are matched to your printer's spools. Missing presets are
   fetched from the Open Filament Database. Review the rows and press **Apply**
   to write the selected presets to Orca:

   ![Apply the chosen presets](assets/8-apply-the-chosen-presets.jpg)

8. You can edit the search metadata used to find presets for a feed:

   ![Edit spool metadata](assets/9-edit-spool-metadata.jpg)

9. If more than one preset matches the metadata, a list of matching presets
   appears. Choosing one changes the selection only; press **Apply** to use it:

   ![Choose a preset from the list](assets/10-choose-preset-from-list.jpg)

10. Optionally, connect a Spoolman instance in the **Settings** tile:

    ![Spoolman settings](assets/11-spoolman-settings.jpg)

11. Allow the plugin to fetch data from Spoolman:

    ![Spoolman permissions](assets/12-spoolman-permissions.jpg)

12. You can now fetch price data from Spoolman for a feed:

    ![Spoolman price panel](assets/14-spoolman-price-panel.jpg)

### What Match and Apply do

- Scanning selects the best candidate in each row. It does not change Orca.
- The preset input shows the current selection. Choosing another entry changes
  the selection only.
- The pencil button opens that preset's settings in a popup. It does not edit
  the preset itself.
- The chevron expands the fields used for matching.
- **Match** reruns matching from the edited fields and changes the selection
  only. It deliberately ignores confirmed history.
- **Apply** writes every changed selection to Orca and remembers the confirmed
  spool, tag and filament mappings. If one row fails, the other rows are still
  applied and the failure is shown on that row.
- On the first scan, the best candidate is selected even when several are
  plausible; no popup opens. If **Match** produces more than one viable
  candidate, the chooser opens.
- A lone OFD candidate is preselected as **Library match**. Press **Apply** to
  install it.
- Use the small **↻** button at the top to scan again after changing the
  printer, spools or presets.

The plugin does not hook Orca's native **Sync filaments** button. It reads the
printer itself and leaves Orca's own button untouched.

## Filament prices

Expand a feed to see **Current price (per kg)**, **New price (per kg)**,
**Fetch price** and **Save price** after the Match button.

- **Current price** is the chosen preset's `filament_cost`.
- **Fetch price** reads the feed's Spoolman spool and converts the whole-spool
  price to Orca's per-kg price:
  `(spool.price ?? filament.price) / ((spool.initial_weight ?? filament.weight) / 1000)`.
- **Save price** stages the new price. **Apply** writes it into the preset.
- On the first scan, Spoolman prices are loaded into **New price**
  automatically. A rescan preserves a value you edited or fetched.
- Changing the chosen preset reloads **Current price** but leaves **New price**
  alone.
- If Spoolman is blank, invalid or unreachable, Fetch is disabled with a
  tooltip; manual new prices still work.

**Changes are applied only after you press `Save price` and then `Apply` at the
top of the plugin pane.** If you do not have a Spoolman instance, you can still
type a price into **New price** and save it the same way.

Orca's plugin API cannot save an existing preset. Plugin-owned local presets are
updated in place. A system preset gets a local copy named
`<preset base> (local)` that inherits the system preset. A **user preset** is
flattened into the copy and inherits its system parent, because Orca's local
bundle loader cannot resolve a user-preset parent and silently skips such a
copy. If multiple feeds share a preset, the last applied price wins.

The matcher ignores Orca's internal `_local/<bundle>/` prefix and the labels
`(local)`, `(custom)`, `(OFD)` and `(copy)`, so custom and local copies are
exact candidates. A `(local)` price copy ranks before its original. The price
fields are metadata only and do not participate in filament matching.

## Spoolman

Spoolman is optional enrichment by spool ID:

- the printer supplies slot data and often a spool ID;
- Spoolman fills vendor, product name, material, colour and temperatures when
  the printer does not;
- a Spoolman extra field (`orca_filament_preset` or `orca_preset`) can be used
  as an explicit preset hint;
- a Spoolman `variant` extra fills the subtype when the printer did not report
  one (for example `SnapSpeed`). A printer-reported subtype always wins.

Spoolman does not provide Orca's preset list, does not perform the matching and
is not needed for the plugin to work.

## Open Filament Database

**OFD matching is enabled by default.** The
[Open Filament Database](https://openfilamentdatabase.org/) is an open catalogue
of community-maintained filament profiles. If no installed preset is a good
match, the plugin proposes the best OFD profile, shows its brand and product
metadata, and installs it into Orca when you press **Apply**. You do not need to
create the preset manually or leave Orca to find it.

The OFD index is cached locally and used without a network request, so vendor
presets still appear when Orca's network permission is denied. Applying an OFD
candidate uses a cached preset JSON when available and fetches one preset
otherwise. To match only installed Orca presets, clear **Open Filament Database**
in the **Settings** tile.

The **Settings** tile also has **Clear OFD cache**. It deletes only the local OFD
index (`ofd_index.json`) and cached preset JSON (`ofd_presets/`); history,
settings and installed presets are kept. The next **↻** downloads a fresh index.

## Matching order

1. A user-confirmed mapping for the exact spool/tag/filament identity, but only
   if the mapped preset still fits the current vendor/material/subtype/product
   (and colour when the preset has one). A stale refill mapping falls through.
2. Exact printer-reported Orca `filament_id` among compatible installed presets.
3. Exact vendor + material + subtype + product. Product text that is only a
   colour (`Yellow`), only the vendor/material (`PETG Pink`), or the material
   plus a grade word (`PETG Basic`, `Premium PCTG`) does not count as a product
   constraint. Meaningful names such as `ABS Pastel` or `PETG Matte` still do.
4. A fuzzy name/colour match above a confidence threshold. Vendor presets that
   carry the variant only in their name (for example `Snapmaker PLA SnapSpeed`)
   are compared by name tokens when the preset has no explicit subtype setting.
5. An Open Filament Database candidate (enabled by default).
6. The exact compatible `Generic <material>` preset, not a variant such as
   `Generic PLA-CF`, unless no exact generic material preset exists.

History is stored in Orca's per-plugin data folder and survives restarts and
plugin reinstalls:

```text
macOS:   ~/Library/Application Support/OrcaSlicer/orca_plugins/plugin_data/filament_match/history.json
Linux:   ~/.config/OrcaSlicer/orca_plugins/plugin_data/filament_match/history.json
Windows: %APPDATA%\OrcaSlicer\orca_plugins\plugin_data\filament_match\history.json
```

Only user-confirmed matches are recorded; automatic generic fallbacks are not.

## Slot count mismatch

OrcaSlicer 2.5.2 has no plugin API to add, remove or resize project filament
slots, so the plugin never silently changes Orca's slot count:

- if the printer reports more feeds than the project has slots, the extra feeds
  are shown as **No Orca slot** and skipped by **Apply**; the panel shows exactly
  how many slots to add;
- if the project has more slots than the printer reports, the extra project
  slots are left untouched; the panel shows how many to remove.

Add or remove the stated number of filaments in Orca's **Filament** tab, then
press **↻**. The plugin applies every feed that has a slot.

## Limitations

- OrcaSlicer's plugin API cannot add a button next to the native **Synchronize
  filament list from AMS** button. The plugin opens a docked panel instead;
  adding that exact button needs a C++ change to OrcaSlicer.
- The plugin cannot override Orca's native AMS/filament sync. There is no
  sync-complete event or tray-payload plugin API.
- The plugin cannot open Orca's native Filament Settings dialog. The pencil
  button shows the preset's settings in the plugin's own popup; edit the preset
  itself in Orca.
- `begin_update` is shipped but undocumented; applying a match reports failures
  instead of silently ignoring them.
- System presets are often served from `.opc` caches, so the plugin does not
  rely on reading their JSON or on their `filament_id`.
- Generic Moonraker reports material/colour/filament ID, not vendor/subtype.
  Those fields stay blank unless Spoolman or a richer printer API supplies them.
- Do not run another plugin that also changes filament slots at the same time.

## Troubleshooting

**The plugin does not appear.** Restart Orca, open **File → Plugins**, and check
that **Filament Match** is enabled. If you installed from source, confirm that
the plugin folder contains exactly one direct `.py` file:

```text
orca_plugins/filament_match/filament_match.py
```

**A network request was denied.** Orca asks once per base origin: the printer
address, the Spoolman address and the OFD. Approve the prompt and press **↻**.
The in-flight request may fail while the dialog is open.

**An old version keeps loading.** Older source installs sometimes left a folder
named `orca_plugins/filament_match.py/`. Orca loads that folder instead of
`orca_plugins/filament_match/`. Remove the legacy folder:

```sh
# macOS
rm -rf "$HOME/Library/Application Support/OrcaSlicer/orca_plugins/filament_match.py"
# Linux
rm -rf "$HOME/.config/OrcaSlicer/orca_plugins/filament_match.py"
```

```powershell
# Windows
Remove-Item -Recurse -Force (Join-Path $env:APPDATA "OrcaSlicer\orca_plugins\filament_match.py")
```

Current versions of `plugin/install.sh` remove it automatically (the Windows
steps above do the same). Orca must be restarted after changing plugin files.

**A price copy does not appear.** Make sure Orca is restarted after installing
the plugin. A price copy of a user preset must inherit a system parent; older
copies that inherited a user preset were skipped by Orca's local bundle loader.
Re-apply the price with the current version to regenerate the copy.

## Development

```text
plugin/
  filament_match.py          # Orca entry point, action and UI
  filament_match_core.py     # pure matching and history logic
  filament_match_sources.py  # printer, Spoolman and OFD adapters
  tests/                     # unittest suites
  dev/mock_printer.py        # editable mock Snapmaker/Moonraker printer
  build_single_file.py       # flatten to one installable .py
  build_hub_file.py          # generate the Orca Cloud upload artifact
  check_version.py           # release version consistency check
  check_hub_description.py   # Hub character/byte limit check
  check_artifact.py          # validate a built flat/Hub artifact
  dev/orca_hub.py            # read/update the Orca Cloud listing metadata
  dev/ORCA_HUB.md            # one-time sign-in and metadata commands
  install.sh                 # build and install locally
  PLAN.md                    # design and phases
  API_NOTES.md               # verified Orca 2.5.2 plugin API notes
  TESTING.md                 # live test checklist
  MOCK_PRINTER.md            # mock printer setup and editor
```

Build the single-file plugin:

```sh
python3 plugin/build_single_file.py
```

Build the Orca Cloud artifact:

```sh
python3 plugin/build_hub_file.py
# or, for a release version:
python3 plugin/build_hub_file.py --version <release-version>
```

The Hub artifact is written to `plugin/hub/filament_match_any.py`. The `_any`
suffix tells Orca Cloud the plugin is pure Python and runs on every supported
operating system.

Check the release version and Hub description limits before publishing:

```sh
python3 plugin/check_version.py
python3 plugin/check_hub_description.py plugin/HUB_DESCRIPTION.md
```

`plugin/filament_match.py` is the single version source: its PEP 723
`version` and `PLUGIN_VERSION` must agree, and `check_version.py` also checks
the built artifacts and release-facing docs.

Run the tests:

```sh
python3 plugin/build_single_file.py
python3 plugin/build_hub_file.py
python3 plugin/check_version.py
python3 plugin/check_hub_description.py plugin/HUB_DESCRIPTION.md
python3 -m unittest discover -s plugin/tests -p 'test_*.py' -v
```

To test feeder counts and filament combinations without a real printer, see
[plugin/MOCK_PRINTER.md](plugin/MOCK_PRINTER.md).

## Publishing

Every stable GitHub release publishes a new Orca Cloud version through OIDC
trusted publishing. Prereleases are skipped. No repository tokens or secrets
are stored in the workflow.

- Workflow: [`.github/workflows/publish-orcacloud.yml`](.github/workflows/publish-orcacloud.yml)
- Artifact: `plugin/hub/filament_match_any.py`
- Release assets: the artifact and `filament_match_any.py.sha256`
- Trigger: a published, non-prerelease GitHub release
- Version: the release tag without the leading `v`
- Changelog: the GitHub release notes, truncated by character to Orca Cloud's limit
- Gate: the test suite runs first; the publish job needs a successful test job

The release tag must be a semantic version higher than the currently published
plugin version. The workflow builds the artifact with the tag version before
uploading, so the file header and `PLUGIN_VERSION` always match the release. It
validates the OIDC response before publishing and attaches the SHA-256 checksum
to the GitHub release.

`plugin/filament_match.py` is the canonical version. After changing it, update
the Hub listing version and changelog in
[plugin/HUB_PUBLISHING.md](plugin/HUB_PUBLISHING.md), then run
`python3 plugin/check_version.py`.

See [plugin/HUB_PUBLISHING.md](plugin/HUB_PUBLISHING.md) for the Hub listing
fields, release assets, credentials and the GitHub publishing setup.

GitHub OIDC can publish releases but cannot edit listing metadata. To read or
update the Hub listing description, tags or visibility, use the account-session
helper in [plugin/dev/ORCA_HUB.md](plugin/dev/ORCA_HUB.md):

```sh
python3 plugin/dev/orca_hub.py login
python3 plugin/dev/orca_hub.py show
python3 plugin/dev/orca_hub.py update --description-file plugin/HUB_DESCRIPTION.md
```

### Credentials

- Never commit `.env` or `.secrets/`; both are git-ignored. `.env.example` is
  the only tracked placeholder.
- Rotate any personal access token that was stored in a plaintext `.env`,
  especially an admin-scoped token, then revoke the old token. Prefer
  `gh auth login` for local work or a fine-grained token limited to this
  repository.
- The publish workflow uses GitHub OIDC, not a stored PAT. The Orca Cloud
  account session is separate and is written with mode `0600`.
- The Hub description limit is 10,000 characters **and** 10,000 UTF-8 bytes;
  `python3 plugin/check_hub_description.py` enforces both.
