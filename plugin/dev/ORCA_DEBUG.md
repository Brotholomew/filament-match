# Debugging a running OrcaSlicer plugin

OrcaSlicer 2.5.2 has no remote-control or plugin-debug port. These are the
options that were tested on macOS, and the method that works.

## What does not work

- **CLI**: `OrcaSlicer --help` exposes slicing/export options only. There is no
  command to open the plugin panel, run a capability, or read the preset bundle.
- **LLDB attach**: the app is Developer-ID signed with the hardened runtime;
  `lldb -p <pid>` fails with `Not allowed to attach to process`.
- **AppleScript/System Events**: driving the plugin panel needs macOS
  Accessibility permission for the terminal app. Without it,
  `tell process "OrcaSlicer"` fails with `-1728`.
- **Screen capture**: works only after granting Screen Recording permission, and
  the plugin panel is a web view whose contents are not readable through
  Accessibility anyway.

## What works: an isolated second instance

Run a second Orca with a cloned data directory. It cannot disturb the open
project or the running instance, and it can be killed at any time.

```sh
DEBUG=${TMPDIR:-/tmp}/orca_debug
rm -rf "$DEBUG"
mkdir -p "$DEBUG"
cp -R "$HOME/Library/Application Support/OrcaSlicer/." "$DEBUG/"
rm -rf "$DEBUG/log" "$DEBUG/cache" "$DEBUG/user_backup-v2.5.2"
rm -f "$DEBUG/user.lock" "$DEBUG/OrcaSlicer.conf.lock"
```

Write the observations you want matched (the mock printer's `/__state` is a
convenient source) to `$DEBUG/debug_observations.json`:

```json
[
  {"slot": 6, "present": true, "vendor": "3DJake", "material": "ABS",
   "subtype": "", "product": "ABS Pastel Pink", "color": "#F49BAD",
   "spool_id": 6, "source": "snapmaker"}
]
```

Patch **the copy** of the plugin in
`$DEBUG/orca_plugins/filament_match/filament_match.py`. Add a function before
`FilamentMatchPlugin` that runs in `register_capabilities()` (after the preset
bundle is loaded) and writes a JSON dump:

```python
def _debug_dump_on_load():
    datadir = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    storage = os.path.join(datadir, "orca_plugins", "plugin_data",
                           "filament_match")
    settings = _load_settings(storage)
    snapshot = _preset_snapshot(orca.host.preset_bundle())
    with open(os.path.join(datadir, "debug_observations.json"),
              encoding="utf-8") as handle:
        observations = [_make_observation(item)
                        for item in json.load(handle)]
    warnings = []
    history = _history_store(storage, warnings)
    rows = [_build_row(obs, snapshot, history, settings, storage, {},
                       warnings) for obs in observations]
    _write_json_atomic(
        os.path.join(datadir, "filament_match_debug.out.json"), {
            "snapshot": {
                "printer": snapshot.get("printer"),
                "slots": snapshot.get("slots"),
                "presets": [dataclasses.asdict(item)
                            for item in snapshot.get("presets") or []],
                "compatible": [dataclasses.asdict(item)
                               for item in snapshot.get("compatible") or []],
            },
            "settings": settings,
            "rows": rows,
            "warnings": warnings,
        })
```

Call it from the copied plugin's registration:

```python
@orca.plugin
class FilamentMatchPlugin(orca.base):
    def register_capabilities(self):
        try:
            _debug_dump_on_load()
        except Exception:
            pass
        orca.register_capability(FilamentMatch)
```

Launch the isolated instance and wait for the dump:

```sh
nohup /Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer \
  --datadir "$DEBUG" --debug 4 --logfile "$DEBUG/debug.log" \
  > "$DEBUG/stdout.log" 2>&1 &
echo $! > "$DEBUG/pid"

sleep 15
python3 - <<'PY'
import json
p = "${TMPDIR:-/tmp}/orca_debug/filament_match_debug.out.json"
data = json.load(open(p))
print("presets", len(data["snapshot"]["presets"]),
      "compatible", len(data["snapshot"]["compatible"]))
for row in data["rows"]:
    obs = row["observation"]
    print(row["slot"], obs["vendor"], obs["material"],
          "=>", row["preset_name"] or "(choose)",
          row["source"],
          [c["preset_name"] for c in row["candidates"]][:3])
PY
```

Stop it when finished:

```sh
kill "$(cat "$DEBUG/pid")"
```

## What this proved

The isolated dump used the real Orca preset bundle (407 compatible presets).
For 3DJake ABS it showed:

```text
Generic ABS @System  source=generic
candidates: Generic ABS @System, 3DJAKE ABS (OFD)
```

The OFD candidate was present in the row, but the matcher's source was
`generic`. That pointed at the matching code, not at permissions or the OFD
cache. The cause was the flattened build's duplicate `_library_candidates`
name: the entry function replaced the core candidate normalizer, its
`TypeError` was swallowed, and the library stage always saw an empty list.
See API_NOTES.md section 10.6.
