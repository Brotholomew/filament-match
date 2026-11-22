# Install Filament Match

## Requirements

- OrcaSlicer **2.5.2 or newer** (2.5.2 is the tested version).
- Installing a prebuilt artifact needs only OrcaSlicer.
- Building from source needs **Python 3.12 or newer** on the machine that runs
  the build. OrcaSlicer ships its own Python for running the plugin; that
  bundled interpreter is not used by `install.sh`.
- Optional: a Spoolman server and network access to the printer and the Open
  Filament Database.

## macOS

```sh
cd /path/to/filament-match
./plugin/install.sh
```

The installer detects `~/Library/Application Support/OrcaSlicer` by default.
Pass a data directory to install into a clone or a non-default location:

```sh
./plugin/install.sh "$HOME/Library/Application Support/OrcaSlicer"
```

The script builds `plugin/dist/filament_match.py`, then replaces
`orca_plugins/filament_match/` as a unit. A failed build leaves the previous
installation untouched.

Then restart OrcaSlicer and open **File → Plugins**. Enable **Filament Match**,
allow network access when Orca asks, and run **Plugins → Run → Filament Match**.
The plugin opens as a docked panel beside the sidebar (or a window on hosts
without dock panels). The plugin does not start threads, so no thread permission
is needed. Orca prompts once per base printer/Spoolman/OFD origin, not per URL path.
If the first network prompt appears behind the main window, approve it and press
**↻** again; subsequent spool and printer paths use that host grant.

The installed layout must be exactly one direct entry file:

```
~/Library/Application Support/OrcaSlicer/orca_plugins/filament_match/
  filament_match.py
  .install_state.json
```

Do **not** copy the development modules (`filament_match_core.py`,
`filament_match_sources.py`) into that folder — Orca rejects a plugin folder with
multiple direct `.py` files. `install.sh` removes stale direct `.py` and `.whl`
files, `__pycache__`, and the legacy `orca_plugins/filament_match.py` path.

## Windows

### Use a prebuilt release artifact

Every stable GitHub release attaches `filament_match_any.py` and its SHA-256 checksum.
That file is the same single-file build. To install without Python:

1. Download `filament_match_any.py` and `filament_match_any.py.sha256`.
2. Verify the checksum in PowerShell:

   ```powershell
   Get-FileHash .\filament_match_any.py -Algorithm SHA256
   Get-Content .\filament_match_any.py.sha256
   ```

3. Copy/rename the file to the plugin entry name:

   ```powershell
   $orca = Join-Path $env:APPDATA "OrcaSlicer"
   $plugins = Join-Path $orca "orca_plugins"
   $dest = Join-Path $plugins "filament_match"
   $legacy = Join-Path $plugins "filament_match.py"

   if (Test-Path $legacy) { Remove-Item -Recurse -Force $legacy }
   New-Item -ItemType Directory -Force -Path $dest | Out-Null
   Get-ChildItem $dest -File |
     Where-Object { ($_.Extension -in ".py", ".whl") -and $_.Name -ne "filament_match.py" } |
     Remove-Item -Force
   Remove-Item -Recurse -Force (Join-Path $dest "__pycache__") -ErrorAction SilentlyContinue
   Copy-Item .\filament_match_any.py (Join-Path $dest "filament_match.py") -Force
   '{"enabled": true}' | Set-Content -Encoding ascii (Join-Path $dest ".install_state.json")
   ```

### Build from source

Building needs Python 3.12 or newer. Use an explicit `python` invocation; do
not rely on the `.py` file association or double-clicking the script.

```powershell
cd path\to\filament-match
python --version
python plugin\build_single_file.py
```

Then install the built file:

```powershell
$orca = Join-Path $env:APPDATA "OrcaSlicer"
$plugins = Join-Path $orca "orca_plugins"
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

Restart Orca, enable the plugin in **File → Plugins**, then run it.

Older archives sometimes left a folder named `orca_plugins\filament_match.py`.
That folder shadows the real `orca_plugins\filament_match` plugin and makes an
old version keep loading. The `if (Test-Path $legacy)` line removes it.

## Linux

```sh
cd /path/to/filament-match
./plugin/install.sh
```

The installer detects `$HOME/.config/OrcaSlicer`. Pass a different data
directory explicitly if needed:

```sh
./plugin/install.sh "$HOME/.config/OrcaSlicer"
```

Or copy `plugin/dist/filament_match.py` to:

```
~/.config/OrcaSlicer/orca_plugins/filament_match/filament_match.py
```

Restart Orca and enable the plugin.

## Manual install from the Orca UI

Use **File → Plugins → Install local plugin** and select the single-file
`plugin/dist/filament_match.py`. Orca copies only that file, so do not select
one of the development modules.

## Supported printer and library scope

- **Snapmaker** printers that expose `print_task_config` and
  `filament_detect`: lane count, vendor, material, subtype, colour, spool ID
  and optional temperatures are read from the native arrays.
- **Moonraker**: Filament Match reads the native lane payloads
  (`lane_data`, `mmu`, then `box`). It does not invent vendor, product, spool or
  tag fields that Moonraker does not report; Spoolman can fill some of them.
  Vendor-specific managers work only to the extent that they populate those
  native lane fields. An unsupported or empty payload produces no feeds, not
  fabricated ones.
- **Open Filament Database**: enabled by default. The index and individual
  preset JSONs are cached locally, so installed-vendor matching can work
  offline. Fetching a missing index or preset needs network access.
- **Spoolman**: optional enrichment and per-kg price sync. It is not required
  for matching and does not provide Orca's preset list.

See [../README.md](../README.md#limitations) for the current plugin API
limitations.

## Updating

### macOS and Linux

Re-run the installer, restart Orca, and reactivate if Orca asks:

```sh
./plugin/install.sh
```

The installer preserves `.install_state.json` and anything in
`orca_plugins/plugin_data/filament_match/`, including `history.json` and
`settings.json`.

### Windows

Rebuild or download the new `filament_match_any.py`, then repeat the install
steps above. `Copy-Item -Force` replaces the entry file, `New-Item
-ItemType Directory -Force` keeps the folder, the stale-file cleanup removes
old direct modules, and the plugin data/history folder is not touched.

## Uninstalling

Delete the plugin folder. Optionally delete the history/settings folder too.

macOS:

```sh
rm -rf "$HOME/Library/Application Support/OrcaSlicer/orca_plugins/filament_match"
# optional: history and settings
rm -rf "$HOME/Library/Application Support/OrcaSlicer/orca_plugins/plugin_data/filament_match"
```

Linux:

```sh
rm -rf "$HOME/.config/OrcaSlicer/orca_plugins/filament_match"
# optional: history and settings
rm -rf "$HOME/.config/OrcaSlicer/orca_plugins/plugin_data/filament_match"
```

Windows (PowerShell):

```powershell
$plugins = Join-Path $env:APPDATA "OrcaSlicer\orca_plugins"
Remove-Item -Recurse -Force (Join-Path $plugins "filament_match")
# optional: history and settings
Remove-Item -Recurse -Force (Join-Path $plugins "plugin_data\filament_match")
```
