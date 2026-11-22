# Install Filament Match

## Requirements

- OrcaSlicer 2.5.0 or newer (2.5.2 tested).
- Python is bundled with Orca; you do not install Python packages.
- Optional: a Spoolman server and network access to the printer.

## macOS

```sh
cd /path/to/filament-match
./plugin/install.sh
```

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
multiple direct `.py` files.

## Windows

```powershell
cd path\to\filament-match
.\plugin\build_single_file.py
```

Copy `plugin\dist\filament_match.py` to:

```
%APPDATA%\OrcaSlicer\orca_plugins\filament_match\filament_match.py
```

Restart Orca, enable the plugin in **File → Plugins**, then run it.

## Linux

```sh
cd /path/to/filament-match
./plugin/install.sh "$HOME/.config/OrcaSlicer"
```

Or copy `dist/filament_match.py` to:

```
~/.config/OrcaSlicer/orca_plugins/filament_match/filament_match.py
```

Restart Orca and enable the plugin.

## Manual install from the Orca UI

Use **File → Plugins → Install local plugin** and select the single-file
`plugin/dist/filament_match.py`. Orca copies only that file, so do not select
one of the development modules.

## Building a wheel

For public distribution, a pure-Python wheel is the officially supported
multi-module form. Until a wheel build is added, the single-file `.py` is the
supported local/cloud artifact. The plugin can be published to Orca Cloud later
with platform suffixes such as `filament_match_any.py`.

## Updating

Re-run `./plugin/install.sh`, restart Orca, and reactivate if Orca asks. History
is in `orca_plugins/plugin_data/filament_match/history.json` and is not removed
by the installer.

## Uninstalling

Delete the plugin folder and optionally the history folder:

```sh
rm -rf "$HOME/Library/Application Support/OrcaSlicer/orca_plugins/filament_match"
# optional:
rm -rf "$HOME/Library/Application Support/OrcaSlicer/orca_plugins/plugin_data/filament_match"
```
