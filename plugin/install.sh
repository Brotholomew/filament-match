#!/usr/bin/env bash
# Install the Filament Match plugin into OrcaSlicer's local plugin folder.
# Usage: ./install.sh [ORCA_DATA_DIR]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${1:-$HOME/Library/Application Support/OrcaSlicer}"
DEST="$DATA_DIR/orca_plugins/filament_match"
LEGACY="$DATA_DIR/orca_plugins/filament_match.py"

if [ ! -d "$DATA_DIR" ]; then
  echo "OrcaSlicer data dir not found: $DATA_DIR" >&2
  echo "Pass it explicitly, e.g. ./install.sh \"\$HOME/Library/Application Support/OrcaSlicer\"" >&2
  exit 1
fi

python3 "$HERE/build_single_file.py"

# Older installs created or left a plugin folder named "filament_match.py".
# Orca loads that folder instead of the intended "filament_match" folder, so
# the installed code is silently shadowed. Remove the legacy path.
if [ -e "$LEGACY" ]; then
  rm -rf "$LEGACY"
  echo "Removed legacy plugin path: $LEGACY"
fi

mkdir -p "$DEST"
rm -rf "$DEST/__pycache__"
cp "$HERE/dist/filament_match.py" "$DEST/filament_match.py"
# A manually copied plugin defaults to disabled. Mark it enabled; Orca still
# asks for permissions on first load. Remove this line if you prefer to click
# Activate manually in File > Plugins.
printf '{"enabled": true}\n' > "$DEST/.install_state.json"

echo "Installed to: $DEST"
echo "Restart OrcaSlicer, open File > Plugins, and check that 'Filament Match' is activated."
echo "Then run it from Plugins > Run > Filament Match."
