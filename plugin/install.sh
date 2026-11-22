#!/usr/bin/env bash
# Install the Filament Match plugin into OrcaSlicer's local plugin folder.
#
# Usage:
#   ./install.sh [ORCA_DATA_DIR]
#
# With no argument the OrcaSlicer data directory is detected from the OS:
#   macOS:  ~/Library/Application Support/OrcaSlicer
#   Linux:  $HOME/.config/OrcaSlicer
# An explicit argument always wins, which is also the way to install on an
# unsupported OS or into a cloned/test data directory.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"

usage() {
  cat <<'EOF'
Usage: ./install.sh [ORCA_DATA_DIR]

Builds plugin/dist/filament_match.py and installs it as the single direct
plugin file in ORCA_DATA_DIR/orca_plugins/filament_match/.

With no ORCA_DATA_DIR, detects:
  macOS:  ~/Library/Application Support/OrcaSlicer
  Linux:  $HOME/.config/OrcaSlicer
EOF
}

if [ "$#" -gt 1 ]; then
  usage >&2
  exit 2
fi
case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

if [ "$#" -eq 1 ] && [ -n "${1:-}" ]; then
  DATA_DIR="$1"
else
  case "$(uname -s)" in
    Darwin)
      DATA_DIR="$HOME/Library/Application Support/OrcaSlicer"
      ;;
    Linux)
      DATA_DIR="$HOME/.config/OrcaSlicer"
      ;;
    *)
      echo "Unsupported OS: $(uname -s)." >&2
      echo "Pass the OrcaSlicer data directory explicitly: ./install.sh /path/to/OrcaSlicer" >&2
      exit 1
      ;;
  esac
fi

if [ ! -d "$DATA_DIR" ]; then
  echo "OrcaSlicer data dir not found: $DATA_DIR" >&2
  echo "Pass it explicitly, e.g. ./install.sh \"$DATA_DIR\"" >&2
  exit 1
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3.12 or newer is required to build Filament Match." >&2
  echo "Install Python from https://www.python.org/downloads/ or set PYTHON=/path/to/python3." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "Python 3.12 or newer is required to build Filament Match; found:" >&2
  "$PYTHON" --version >&2 || true
  echo "Install a newer Python or run the build with a 3.12+ interpreter, then retry." >&2
  exit 1
fi

echo "Building the single-file plugin ..."
"$PYTHON" "$HERE/build_single_file.py"
BUILT="$HERE/dist/filament_match.py"
if [ ! -f "$BUILT" ]; then
  echo "Build did not produce $BUILT" >&2
  exit 1
fi

PLUGINS_DIR="$DATA_DIR/orca_plugins"
DEST="$PLUGINS_DIR/filament_match"
LEGACY="$PLUGINS_DIR/filament_match.py"
mkdir -p "$PLUGINS_DIR"

# Stage the complete replacement first. The live folder is only replaced after
# the build and every copy succeeded, so a failed build never leaves a partial
# plugin behind.
STAGE="$(mktemp -d "$PLUGINS_DIR/.filament_match.install.XXXXXX")"
BACKUP=""
cleanup() {
  rm -rf "$STAGE"
  if [ -n "$BACKUP" ] && [ -e "$BACKUP" ] && [ ! -e "$DEST" ]; then
    mv "$BACKUP" "$DEST" || true
  fi
  if [ -n "$BACKUP" ]; then
    rm -rf "$BACKUP"
  fi
}
trap cleanup EXIT

cp "$BUILT" "$STAGE/filament_match.py"
# A manually copied plugin defaults to disabled. Preserve an existing state
# file (including an explicit disable) across updates; otherwise mark the new
# install enabled. Orca still asks for permissions on first load.
if [ -f "$DEST/.install_state.json" ]; then
  cp "$DEST/.install_state.json" "$STAGE/.install_state.json"
else
  printf '{"enabled": true}\n' > "$STAGE/.install_state.json"
fi
# Preserve a history/settings file if an older install kept one next to the
# plugin. The normal location is orca_plugins/plugin_data/filament_match/,
# which this script never touches.
for sidecar in history.json settings.json; do
  if [ -f "$DEST/$sidecar" ]; then
    cp "$DEST/$sidecar" "$STAGE/$sidecar"
  fi
done

# Older installs created or left a folder named "filament_match.py". Orca loads
# that folder instead of the intended "filament_match" folder, so the installed
# code is silently shadowed. Remove the legacy path before replacing DEST.
if [ -e "$LEGACY" ] || [ -L "$LEGACY" ]; then
  rm -rf "$LEGACY"
  echo "Removed legacy plugin path: $LEGACY"
fi

if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  BACKUP="$(mktemp -d "$PLUGINS_DIR/.filament_match.backup.XXXXXX")"
  rmdir "$BACKUP"
  mv "$DEST" "$BACKUP"
fi

if ! mv "$STAGE" "$DEST"; then
  if [ -n "$BACKUP" ]; then
    mv "$BACKUP" "$DEST"
    BACKUP=""
  fi
  echo "Could not move the staged plugin into place: $DEST" >&2
  exit 1
fi
rm -rf "$BACKUP"
BACKUP=""

# Replacement also drops stale direct .py/.whl files and __pycache__ that Orca
# would otherwise treat as extra plugin entry modules.
echo "Installed to: $DEST"
echo "The plugin folder contains exactly one direct .py file."
echo "Restart OrcaSlicer, open File > Plugins, and check that 'Filament Match' is activated."
echo "Then run it from Plugins > Run > Filament Match."
