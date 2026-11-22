# OrcaSlicer 2.5.2 plugin API notes

**Verified against**
- Installed app: `/Applications/OrcaSlicer.app` (`CFBundleShortVersionString` = **2.5.2**).
- Data dir: `~/Library/Application Support/OrcaSlicer` (macOS).
- Official source tag `v2.5.2` (`f92a7c065c5718b8ef1baac1cd704df2911238f4`) and the official wiki
  (`github.com/OrcaSlicer/OrcaSlicer_WIKI`, fetched 2026-11-21).
- The installed Mach-O binary's pybind docstrings, which match the source.
- `~/Library/Application Support/OrcaSlicer/orca_plugins` was empty at the start of testing.
  Behaviour below is source/binary-verified; it has not been exercised by a running plugin on
  this machine. Items that need a live check are marked **UNVERIFIED**.

## 0. Corrections to `PLAN.md` before implementation

1. **The flat three-`.py` layout will not load.** A plugin folder may contain exactly one
   direct `.py` entry file. `filament_match.py`, `filament_match_core.py`, and
   `filament_match_sources.py` in the same folder produce
   `Plugin directory contains multiple .py files` and the plugin is shown as invalid.
2. **Merely copying the folder is not enough for auto-load.** A folder with no
   `.install_state.json` is discovered but defaults to `enabled = false`. Activate it once in
   Plugins, or pre-create the sidecar. Installing through the UI copies **only the selected
   `.py` file**, not sibling modules.
   The current `plugin/install.sh` copies all three flat `.py` files into
   `orca_plugins/filament_match/`, so it will be rejected as “multiple .py files”. Use the
   one-file output of `plugin/build_single_file.py`, or move `*_core.py` / `*_sources.py`
   into a subpackage. The current `build_single_file.py` is the simplest fix for local testing.
3. **`execute()` runs on a background Script worker in 2.5.2, not the UI thread.** The wiki's
   `host.md` says UI thread; the source, the installed binary docstring, and working plugins all
   say background worker. Read live presets/model on the UI thread (window `on_message` or a
   preset lifecycle event), not directly from `execute()`.
4. **`begin_update` is shipped but not in the public wiki API.** It is 1-based and, when given a
   preset name, changes the slot assignment. `.begin_update(slot, name).commit()` is a valid
   selector; `.commit()` returns a `PresetUpdateResult`.
5. **No host API exposes a preset's `filament_id`.** Stock 2.5.2 on macOS also has no system
   filament JSON files to read: system presets are served from `.opc` vendor caches.
6. **There is no native "AMS/filament sync completed" lifecycle event.** `PresetSelected` is
   the closest signal; the sync path itself emits one for slot 1 after a full sync, and none for
   a colour-only sync.
7. **`reload_local(id)` only reads the active user's `_local` folder.** On this machine the
   active `preset_folder` is a UUID (`090d1dbc-…`), not `default`; writing to
   `user/default/_local/…` and reloading the id will fail.

## 1. Local plugin packaging and discovery (macOS)

### Where it is scanned

- Local root: `~/Library/Application Support/OrcaSlicer/orca_plugins/`.
- Discovery scans **direct child directories only**. A bare `.py` dropped directly in
  `orca_plugins/` is ignored.
- Each direct child is one plugin. Its folder name is arbitrary for a manual install; the
  operational `plugin_key` is derived from the **entry file stem** (sanitized to
  `[A-Za-z0-9_.-]`), e.g. `filament_match.py` → `filament_match`.
- Entry-file rules in `PluginFsUtils.cpp::find_installed_plugin_entry()`:
  - exactly one direct `.py`, **or** exactly one direct `.whl`;
  - zero `.py`/`.whl` → invalid;
  - more than one `.py` → invalid (`multiple .py files`);
  - more than one `.whl` → invalid;
  - both `.py` and `.whl` → invalid.
- Direct child directories whose names start with `.` or `__` are ignored, as are
  `_subscribed/` and `plugin_data/`.
- The entry file is imported by stem; the loader first adds the plugin folder itself to
  `sys.path` (`PythonInterpreter::load_module_from_file`). Subdirectories are not added
  automatically.
- Load/discovery runs at startup and on Plugins-dialog rescan/reload. Editing a loaded file
  does not take effect until reload/rescan or restart.

### Metadata: PEP 723 block (required for a proper install)

`read_python_plugin_metadata()` only recognises the exact delimiters. Minimal block:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Filament Match"
# description = "Match printer filaments to Orca presets."
# author = "Your Name"
# version = "0.1.0"
# ///
```

- `name` is required by the **UI local installer**; a `.py` with no PEP 723 `name` is
  rejected with “missing required PEP 723 metadata: 'name' is required”.
- `requires-python` is parsed but not enforced. `dependencies` are installed with bundled `uv`
  into `…/python/packages/cp312/`, not into the plugin folder.
- There is no `type` and no `id` field. Types come from the registered capabilities.
- The block is found by scanning for the exact `# /// script` / `# ///` lines; convention puts
  it at the top. Inside the block, content lines should start with `#` or `# `; malformed lines
  are skipped by the parser.

### Multi-module layouts

| Layout | Result |
|---|---|
| One entry `.py` + other `.py` files in the **same folder** | **Rejected** (multiple entry files). |
| One entry `.py` + a **subpackage** folder, e.g. `filament_match_lib/__init__.py`, `core.py`, `sources.py` | Works by ordinary Python import because the plugin root is on `sys.path`; **not an officially documented layout — test once**. |
| One entry `.py` + `sys.path.insert(0, <subdir>)` then top-level imports | Works by Python semantics; the insertion itself is not audited. Still keep only one direct `.py`. |
| One `.whl` entry containing a package | **Official supported multi-module form.** Wheel is extracted to `__whl_extracted__/<pkg>/` and imported. |
| Entry `.py` + extra `.whl` dependencies in the same folder | Loader adds each `.whl` to `sys.path`; not needed for local modules. |

For this project: use
```
orca_plugins/filament_match/
  filament_match.py          # the only direct .py
  filament_match_lib/
    __init__.py
    core.py
    sources.py
```
and `from filament_match_lib import core, sources`. Do not rely on `sys.path` insertion to
make flat sibling `.py` files legal; discovery rejects them before import. For distribution,
build a wheel.

### Activation and `.install_state.json`

- `PluginDescriptor.enabled` is read from `<plugin folder>/.install_state.json`; default is
  **false** when the sidecar is absent (`PluginDescriptor.hpp`).
- A manually copied folder is therefore listed but not loaded. Activate it in Plugins (this
  writes the sidecar with `enabled: true` and loads it). A minimal sidecar for local testing is
  `{"enabled": true}`; Orca tolerates missing keys.
- Installing through Plugins → Install local plugin:
  - copies only `source.py` to `orca_plugins/<source.py filename>/<source.py filename>`;
  - writes a sidecar with `enabled: true`;
  - loads it.
  It does **not** copy a sibling package/subfolder. Use a manual folder copy or a wheel for
  multi-file plugins.
- A successful reinstall resets sidecar permissions/capability choices; grants are requested
  again.

### Plugin key / storage caveat

The local key is the entry filename stem. For a wheel named
`filament_match-0.1.0-py3-none-any.whl`, the key includes the version. Renaming the file or
changing a versioned wheel name moves `plugin.storage()` and loses history. Keep a stable
entry filename (and wheel filename if distributing locally).

## 2. `orca.host.ui.create_window`

Exact 2.5.2 binding (installed binary docstring matches source):

```python
win = orca.host.ui.create_window(
    html: str,
    title: str = "OrcaSlicer",
    width: int = 820,
    height: int = 600,
    on_message=None,
    on_close=None,
    style=orca.host.ui.WINDOW_MODELESS,   # WINDOW_MODELESS or WINDOW_MODAL
    on_submit=None,
) -> orca.host.ui.UiWindow
```

`UiWindow` members (there is **no** `show()` / `hide()` on a window):

| Member | Behaviour |
|---|---|
| `win.id` | opaque integer id (read-only). |
| `win.post(data)` | Queue a JSON-serializable payload to the page. Delivered on the UI thread to the page's `orca.onMessage(cb)`. Returns `None`; does not block. Can be called from the Script worker. |
| `win.is_open()` | `True` while the window is registered/open. Thread-safe presence check. A handle is considered open from creation request until close/teardown, so it can be `True` before the native window has materialised. |
| `win.close()` | Request close; fires `on_close` if the close comes from user/JS/this call. |

Behaviour details:

- `create_window()` is **non-blocking**: it reserves a handle, schedules real window creation
  on the next UI-loop turn, and returns immediately. `post()` calls made first are queued in
  order after creation.
- The window is shown automatically when created. `style=WINDOW_MODELESS` is the default;
  `WINDOW_MODAL` blocks in a modal loop and is for one-shot dialogs. Any other style bit raises
  `ValueError` (“unsupported ... style flags”).
- `on_message(data)` runs on the **UI thread**; `data` is the decoded JSON value. Exceptions are
  logged and swallowed. Keep it quick; offload work to your own thread.
- `on_close()` takes no arguments and runs on the UI thread. It fires only for a user/JS/`close()`
  close. When the plugin is unloaded/reloaded, the app language changes, or Orca exits, the host
  destroys plugin windows **without** calling `on_close`.
- `on_submit(data)` is only relevant to modal windows / `show_dialog`; it receives the
  `orca.submit()` payload.
- Page side: `orca.postMessage(obj)`, `orca.onMessage(cb)`, `orca.close()`, and (modal)
  `orca.submit(obj)`.
- A `post()` sent before the page has installed its `onMessage` handler can be lost. Use a
  handshake: page sends `{"type":"ready"}` after registering, plugin replies with data. This is
  what Spooler and BumpMesh do.
- For hide/show, use `create_dock_panel()` instead; `UiDockPanel` has `show()`, `hide()`,
  `post()`, `is_open()`, `close()`. Dock panels belong to Prepare/Preview and are hidden on
  other tabs.
- Working pattern:
  ```python
  self._win = orca.host.ui.create_window(
      html=PAGE, title="Filament Match",
      on_message=self._on_message, on_close=self._on_close)
  # later, from any thread:
  if self._win is not None and self._win.is_open():
      self._win.post({"type": "data", "rows": rows})
  ```
- `create_window` is safe from `execute()` (background worker); it marshals to the UI thread.
  Do not call `orca.host.ui.*` from a slicing-pipeline hook (documented deadlock risk).

## 3. `orca.host.preset.filament.begin_update(slot, name).commit()`

### Signature and semantics

```python
orca.host.preset.filament.begin_update(
    slot_number: int, preset_name: str = ""
) -> PresetTransaction
```

- `orca.host.preset.filament` is a `PresetEditor` for filament. The `slot_number` overload exists
  only there; `process`/`printer` take `begin_update(preset_name="")`.
- **Slots are 1-based**, matching the UI and `PresetBundle::filament_presets`. Valid range is
  `1..len(bundle.current_filament_preset_names())`.
- `preset_name=""` (omitted) targets the preset currently assigned to that slot and does not
  reassign it.
- A non-empty `preset_name`:
  1. resolves against the **slot's own sidebar selector** by the name shown (display label or
     item alias, with a trailing `(modified)` removed);
  2. selects it through the normal `Tab::select_preset()` flow, including unsaved-edit prompts;
  3. records the new slot assignment and the same side effects as a sidebar selection
     (project/selection refresh, dynamic filament list, support-flush recalc, slot combo refresh,
     AMS sync badge).
  So `.begin_update(slot, name).commit()` **does select the preset for the slot**, not merely
  edit an existing preset.
- `.commit()` applies any staged `.set(key, value)` values through the normal tab setting-change
  path and returns `PresetUpdateResult`:
  `.type`, `.target_name`, `.selection_changed`, `.slot_number`, `.slot_assignment_changed`,
  `.shared_slots`, `.keys`, `.dirty`, `.category`.
  With no `.set()`, it still validates the target and applies an empty batch (this is how the
  slot-selector pattern works).
- The transaction edits the **shared** preset. If several slots use the same preset, changing its
  settings affects all of them. Selecting a different preset for one slot does not edit the
  other slots.
- Settings applied by `.set()` are not written to disk; the edited preset remains dirty until
  the user saves it. Selecting a different preset through `begin_update` also leaves the previous
  dirty preset under the normal save/discard/transfer prompt.
- After `commit()` the transaction is closed; any further `.set()`/`.commit()` raises
  `RuntimeError`.

### Errors

| Condition | Exception |
|---|---|
| slot < 1 or beyond slot count | `IndexError` (`std::out_of_range`) |
| slot has no assigned/unresolvable preset | `RuntimeError` |
| mixed-color virtual slot | `ValueError` |
| requested name not offered by that slot's selector | `ValueError` |
| name shown by two entries (ambiguous) | `ValueError` |
| invalid staged setting key/value, or the same key staged twice | `ValueError`; no staged value is applied |
| user cancels unsaved-edit/selection prompt | `RuntimeError` |
| target preset or slot mapping changed between begin and commit | `RuntimeError` |
| app closing / main window / preset bundle unavailable | `RuntimeError` |

### Thread requirement

- The shipped contract, pybind docstring, HLSD, and working plugins say: call it from the
  **background Script worker**, never from the UI thread or a slicing-pipeline hook.
- In 2.5.2, every Script `execute()` (sync `def` or async) is dispatched to a dedicated Script
  worker (`PluginManager::launch_script_capability`), so calling `begin_update()` directly in
  `execute()` satisfies the contract.
- Source observation: 2.5.2 does **not** enforce a thread check; Spooler also calls it from its
  own background thread. Do not rely on that tolerance; use `execute()`'s worker.
- `begin_update()` marshals target selection to the UI thread and blocks the worker until any
  selection/unsaved-edit dialog is answered. The UI remains responsive. Never call it from a
  UI-thread callback (it would block the UI).

### Undocumented status

`begin_update()` is **not documented in the public wiki Host API page**. It is present in:
- `docs/HLSD/preset-plugin-api.md` in the v2.5.2 source tree;
- the installed binary's pybind docstrings;
- real plugins (Spooler uses exactly `orca.host.preset.filament.begin_update(slot, name).commit()`;
  FilamentStandard, whose request is issue #17357, also uses `begin_update(slot, preset_name)`).

Treat it as shipped-but-unsupported: wrap it, log the exception text, and do not assume it will
never change. There is no plugin API to create/remove slots or set a slot colour
(issue #17357).

### Name resolution caveat for `_local` bundles

For a preset loaded from `_local/<id>/`, the internal `Preset.name` is prefixed
`_local/<id>/<base name>`, while the selector label/alias is the base name. Do not blindly pass
`preset.name` for a bundle preset. Resolve with the visible label/alias
(`preset.label()` / `preset.alias`) and test; an ambiguous alias across nozzle variants can
also be rejected. Do not assume `preset.name == preset.label()` for root filament presets:
Orca's alias rule can drop an `@Printer` suffix, and the selector may then be ambiguous.

## 4. `orca.host.preset.bundles.reload_local(id)`

### Path and active user

`reload_local(bundle_id)` reads:

```
<data_dir>/user/<preset_folder>/_local/<bundle_id>/
```

- `<data_dir>` on macOS = `~/Library/Application Support/OrcaSlicer`.
- `<preset_folder>` is the active `AppConfig` key `preset_folder`; empty means `default`.
  On this machine it is the UUID `090d1dbc-a6d4-40eb-81e0-b9e5df8e0c47`.
- `bundle_id` must be a single path component: no `/`, `\`, `.` or `..`.
- The same id must not exist under `_subscribed/`.
- There is **no host API that returns `preset_folder`**. To write a bundle, infer it from a
  user preset's `preset.file` (`.../user/<folder>/...`), from an existing local bundle path,
  from the selected printer preset's `file` (usually a user machine preset under
  `user/<folder>/machine/`), or from listing `user/*` and testing `list_ids()`. Hardcoding
  `default` is wrong on this install. The printer-preset path is the fallback used when no user
  filament preset exists.

### Required metadata file

The file is **`bundle_metadata.json`** (not `bundle_structure.json`). Minimum:

```json
{
  "id": "filament_match",
  "name": "Filament Match presets",
  "version": "0.1.0"
}
```

- `id` is required and must **exactly equal the folder name**; otherwise
  `check_local_bundle()` fails with “metadata is missing, malformed, or has a different id”.
- `name`, `version`, `description`, `author`, `imported_time`, `updated_time` are optional.
  The `print_presets` / `filament_presets` / `printer_presets` arrays are optional; the loader
  clears and recomputes them from the files.
- `bundle_structure.json` is an archive-import manifest only. It is not read for a folder that
  already sits in `_local/`.

### Preset layout

```
user/<folder>/_local/filament_match/
  bundle_metadata.json
  filament/*.json
  process/*.json      # only if you ship process presets
  machine/*.json      # only if you ship printer presets
```

Each `filament/*.json` is a normal Orca preset JSON. Required/normal fields include `type`,
`name`, `from`, `instantiation`, `version`, and optionally `inherits`. A root preset that must
work with native AMS sync should have `"inherits": ""` and its own `filament_id` (see §6).
Unknown config keys, a missing `inherits` parent, or a malformed value reject the whole bundle
reload (Spooler relies on pre-validating every key).

### Behaviour

- `reload_local(id)` re-reads that bundle from disk, replaces the presets that belong to the
  bundle, and refreshes the sidebar/tabs/combos. No restart.
- It preserves the current selection and edited values where possible, and it does **not** save
  or create presets; it only re-reads files. The host API has no create/delete preset call.
- It marshals to the UI thread, so it is safe to call from the Script worker.
- On success returns `None`. On failure raises `RuntimeError` with the reason: unknown id
  (“Local bundle directory does not exist”), metadata id mismatch, subscribed-id collision,
  parse error, missing parent, invalid keys, app closing, or preset bundle/main window not
  available.
- `orca.host.preset.bundles.list_ids()` returns the ids `reload_local()` accepts in the active
  user's `_local` folder, including bundles added after startup. Re-list; do not cache.
- Bundle presets are internally named `_local/<id>/<base>`; `is_from_bundle()` is true and
  `is_system` is false. Their `preset.file` is a real readable file.

## 5. `orca.host.plugin.storage()`

- Implementation path:
  `~/Library/Application Support/OrcaSlicer/orca_plugins/plugin_data/<plugin_key>/`.
  For this plugin with entry `filament_match.py`, the key is `filament_match`, so:
  `…/orca_plugins/plugin_data/filament_match/`.
- Cloud plugins use
  `…/orca_plugins/plugin_data/_subscribed/<user_id>/<cloud_uuid>/`; this raises if no user is
  signed in.
- The folder is created on demand. `storage()` must be called from a plugin callback
  (`execute()`, `on_load()`, message handler, lifecycle handler), not at import time; otherwise
  it raises `RuntimeError`.
- It is the **right place for persistent history/settings/index**. It survives app restarts,
  plugin reloads, and plugin uninstall/reinstall: `delete_plugin()` removes the plugin root but
  not `plugin_data/`. It is not cloud-synced or backed up by Orca, so history is per-machine.
- Because the key is the entry stem, keep the entry filename stable. A versioned wheel filename
  changes the storage path between versions.
- Use atomic write (`tempfile` in the same dir + `os.replace`) and a lock if multiple threads
  write. `data_dir()` is an allowed audit folder, so normal reads/writes there do not prompt.
- The pybind docstring says “Return the installed folder of the current plugin”, but the
  implementation returns the `plugin_data` folder; trust the path above.

## 6. Reading `filament_id` and system preset files

### Host API

- `Preset` exposes `type`, `name`, `alias`, `file`, `bundle_id`, `is_*`, `is_user()`,
  `is_from_bundle()`, `label()`, `config_keys()`, `config_value()`. It does **not** expose
  `filament_id`, `setting_id`, `base_id`, or `key_values`.
- `filament_id` is parsed into the C++ `Preset::filament_id` metadata field; it is not a
  `PrintConfig` option and is not part of `Preset.config`. Therefore
  `preset.config_value("filament_id")` returns `None` (and `"filament_id"` should not appear in
  `config_keys()`). **UNVERIFIED at runtime; source-verified.** Test by printing
  `preset.config_keys()` for a system preset.
- `bundle.full_config_value("filament_ids")` returns the `filament_ids` array for the
  **currently selected filament slots**, index-aligned with the slots. This is the only
  host-readable form of `filament_id`, and only for current slots—not the whole catalog.
- `bundle.current_filament_preset_names()` / `current_filament_presets()` give the current slot
  preset names/objects.

### Preset files

| Preset source | `preset.file` | Readable JSON? |
|---|---|---|
| User preset | real `user/<folder>/filament/<name>.json` | yes |
| Local/subscribed bundle preset | real `_local/<id>/filament/<name>.json` | yes |
| System preset | `data_dir()/system/<vendor>/<subpath>` (e.g. `.../system/OrcaFilamentLibrary/filament/...json`) | **normally no** |

Why system JSON is absent on stock macOS 2.5.2:
- The app resources contain 72 `<Vendor>.opc` files and only six loose `.json` files; filament
  presets are inside the `.opc` vendor caches.
- This machine's `~/Library/Application Support/OrcaSlicer/system/` contains only
  `Snapmaker.opc`, `Custom.opc`, and `OrcaFilamentLibrary.opc` — no per-vendor `.json` tree.
- `preset.file` for a system preset is constructed as
  `data_dir()/system/<vendor>/<entry.sub_path>`, whether the vendor was parsed from JSON or
  served from `.opc`, and no JSON is written for cache-served presets.
- Result: `open(preset.file)` for a system preset is expected to raise `FileNotFoundError`.
  The `.opc` bytes are readable (app resources and data dir are audit-allowed), but `.opc` is a
  cereal binary cache with a CRC header, not a documented/plugin-parseable format. Do not parse
  it.

### Practical recommendation

- Match installed presets by `name`, `config_value("filament_vendor")`,
  `config_value("filament_type")`, `config_value("filament_settings_id")`, colour fields, and
  `is_compatible`; store the preset **name** in history, not an unavailable ID.
- If you need an exact `filament_id` for native AMS sync, **mint your own** stable ID when you
  write a root (`inherits: ""`) preset into a local bundle, as Spooler does
  (`FILAMENT_ID_PREFIX + sha256(...)` and `filament_id` in the JSON). Native sync only considers
  root presets whose `get_preset_base()` is themselves, matched by `filament_id`; a preset that
  inherits a system generic will resolve to the parent, not your preset.
- For current slots, use `full_config_value("filament_ids")` to correlate with the printer's
  reported ID. For the catalog, there is no host API.

## 6a. Active printer and its address

- `orca.host.selected_printer()` returns a dict for the printer selected in the
  **Device** tab: `agent`, `model_id`, `connection` (`lan`/`cloud`), `online`,
  `connected`, `firmware`. It returns `None` when nothing is selected. It is
  **UI-thread only** (`RuntimeError` otherwise) and deliberately exposes no
  name, serial, address or access code.
- `print_host`, `print_host_webui`, `printhost_port` and `printhost_apikey` are
  the connection fields in the selected printer preset. Read them with
  `current_printer_preset().config_value(key)` or
  `preset_bundle().full_config_value(key)` on the UI thread. `full_config()`
  includes them; `full_config_secure()` strips them.
- On this machine the U1 preset stores `print_host = 10.0.0.176` with no scheme
  or port. The same Moonraker API answers on `http://10.0.0.176` (port 80) and
  `http://10.0.0.176:7125`. For a bare host, try the Snapmaker web port first
  when `selected_printer()["agent"]` is `snapmaker`, otherwise Moonraker's 7125
  first, then port 80.
- Do not expect `selected_printer()` to give the host. Cross-check the selected
  preset's `print_host`; if both are absent, tell the user to connect a printer.
- `orca.host.ui.message(text, title=..., buttons="ok", icon="warning")` is a
  native popup and is safe from the UI-thread message handler.

## 6b. Snapmaker filament variants are only in the preset name

The U1's Moonraker `print_task_config` has `filament_type` and
`filament_sub_type` (for example `PLA` + `SnapSpeed`), but the installed
Snapmaker filament presets do **not** expose a matching `filament_sub_type`
config value. The variant is only in the preset name/alias
(`Snapmaker PLA SnapSpeed @U1`). `Preset.config_value("filament_type")` is
`PLA`; the subtype key is absent.

Matching must therefore fall back to the preset name: normalize the name,
remove the `@Printer` suffix, vendor tokens and material tokens, and compare the
remaining tokens with the printer-reported subtype. Nozzle variants such as
`@U1 0.2 nozzle` should be filtered against the active printer's nozzle so the
base/0.4 preset is selected instead of producing a four-way chooser.

## 6c. `plugin.storage()` in 2.5.2

`storage()` checks `PluginAuditManager::current_plugin()`, which is set by the
trampoline around a host-to-Python call. In the live 2.5.2 app, calling it from
the window `on_message` handler raised
`plugin.storage() must be called from a plugin callback`, even though the wiki
lists message handlers as valid callbacks. Resolve it once at the top of
`execute()` (which does have a context), cache the path, and use only the cache
from `on_message` and the worker loop.

## 6c. Dock panel as the plugin UI

`orca.host.ui.create_dock_panel(html, title=..., width=..., height=...,
on_message=..., on_close=..., dock="right")` returns a `UiDockPanel` with
`post`, `hide`, `show`, `is_open`, `close`. It is the closest supported UI to
the filament sidebar: it shares the sidebar dock area, can be moved/floated/
resized/closed, and is hidden while the Device/Project/Calibration tabs are
active. `is_open()` is still true when the panel is hidden, so re-running the
action should call `show()` rather than creating a second panel. `on_close`
fires only for a user/page close, not when the host unloads the plugin.

The 2.5.2 plugin API has **no** way to add a button to the native filament
title bar. That would require a C++ change to `Sidebar` in `Plater.cpp`.

## 7. Lifecycle events in 2.5.2

Every enabled capability gets `on_lifecycle_event(event, ctx)` (root `orca.PythonPluginBase`).
It is delivered synchronously on the thread that raised the event. Keep handlers short; never
block; exceptions are logged and swallowed.

`orca.LifecycleEvent` values in 2.5.2:

- Project: `NewProject`, `ProjectOpened`, `ProjectBeforeSave`, `ProjectAfterSave`,
  `ProjectClosed`, `ProjectDirtyChanged`
- Slicing: `SliceStarted`, `SliceGeometryFinished`, `GCodeExportStarted`,
  `GCodeExportFinished`, `SlicingJobComplete`
- Plate/model: `ObjectAdded`, `ObjectDeleted`, `ObjectTransformed`, `ObjectChanged`,
  `ObjectRenamed`, `PlateCreated`, `PlateDeleted`, `PlateSelected`, `PlateRenamed`
- Presets: `PresetSelected`, `PresetSaved`
- Printer/device: `PrintStateChanged`, `DeviceOnline`, `DeviceOffline`, `DeviceDiscovered`,
  `DeviceSelected`, `UploadStarted`, `UploadFinished`
- Jobs: `PrintJobStarted`, `PrintJobFinished`, `SendJobStarted`, `SendJobFinished`

Payload fields: `name`, `code` (`Ok`/`Error`/`Warn`), `msg`, `id`, `previous_name`,
`device_id`, `job_id`, `source`, `index`, `dirty`. For `PresetSelected`, `name` = preset,
`msg` = `print`/`filament`/`printer`/`sla_print`/`sla_material`. For `PresetSaved`, `msg` =
`new`/`overwrite`.

### Native filament/AMS sync

- **There is no filament-sync or AMS-sync completion event.** The enum above is the complete
  2.5.2 set; no `AmsSync*` / `FilamentSync*` exists.
- `PresetSelected` is fired from `Tab::select_preset()` after `load_current_preset()`.
  The native “Synchronize filament list from AMS” path (`Sidebar::sync_ams_list`) calls
  `Tab::select_preset(bundle.filament_presets[0])` only for a full sync
  (`!sync_color_only`). That produces a `PresetSelected` for slot 1 after the sync, not one per
  changed slot; a colour-only sync emits none from this path. Other dialogs in the sync flow can
  cause additional `PresetSelected` events, so do not count them as the completion signal.
- Therefore you cannot reliably detect “native sync finished” or each slot change from
  lifecycle events alone. Practical options:
  1. Poll `bundle.current_filament_preset_names()` (or `current_filament_presets()`) and diff
     against a stored snapshot. Read on the UI thread: use a window page `setInterval` →
     `orca.postMessage({"type":"poll"})` → `on_message` (UI thread), or a `PresetSelected`
     handler (UI thread). Do not read the live bundle from `execute()`'s worker without a
     queue/snapshot.
  2. Use `PresetSelected` / `PresetSaved` / `ProjectOpened` / `ProjectDirtyChanged` as
     “something changed, take a snapshot” triggers, then diff.
  3. `PrintStateChanged` / `DeviceOnline` tell you about printer state, not filament sync.
- Native sync can rewrite slots without any event (slots 2..N, colour-only). Polling is the
  only complete detection method.
- `PresetSelected` also fires for ordinary user selections, project loads, printer changes, and
  plugin `begin_update` selections. Debounce and compare actual slot names before reacting.
- Slot names from `_local` bundles are internally prefixed `_local/<id>/...`; compare/store the
  same form or normalize consistently.

## 7a. Permissions and the audit hook (network retry behaviour)

Source: official wiki `plugin_audit_hook.html` and `registry.html`, fetched 2026-11-21, matching
the 2.5.2 binary.

- Plugin I/O is gated by a CPython audit hook. `execute()` and window callbacks run inside an
  audit context; Orca prompts Yes/No before a filesystem, network, process or thread operation.
- `orca.request_permissions(...)` can only pre-approve exact targets and may only be called from
  `register_capabilities()`. There is **no network wildcard**, so a plugin that talks to a
  user-entered Moonraker/Spoolman host cannot declare it at load time.
- When an audited call happens on a thread the UI thread is not waiting on (including the
  background Script worker), the prompt is posted asynchronously and the in-flight call is
  **denied** with `PermissionError` (fail closed). The grant is persisted; the next attempt
  succeeds. The plugin must tell the user to approve and retry — a single automatic retry can
  still race the user.
- `threading=True` grants only thread creation; the new thread's work is unaudited. This plugin
  does not create threads (the host provides the Script worker), so it does not request it.
- The plugin's storage folder under `data_dir()` is an allowed root, so history, the OFD cache,
  and preset JSON reads/writes there do not prompt.

## 8. 2.5.2 vs 2.5.1 (API-relevant)

Tag compare `v2.5.1...v2.5.2` (187 files) shows **no changes** to:
`src/slic3r/plugin/host/PluginHostUi.cpp`, `PluginHostPresets.cpp`,
`src/slic3r/plugin/PluginLoader.cpp`, `PythonPluginBridge.cpp`,
`src/libslic3r/LifecycleEvents.hpp`, or any preset JSON schema.

Plugin-related changes in 2.5.2 are fixes only:

- `PluginFsUtils.cpp`: Windows-only retry for transient plugin-folder deletion failures; Windows
  long-path handling for ResourceTexture packs. No macOS/API effect.
- `PluginManager.cpp`: install-state sidecar persistence moved from load/unload callbacks to
  explicit writes after a successful load and after unload. A manually copied plugin with no
  sidecar still defaults to disabled; first activation writes it.
- `PluginPages.cpp`: hide a page removed from the overflow tab (UI fix).
- Release notes also list: cloud plugin updates on Windows, plugin-page overflow fix, Windows
  long-path texture packs, and a Creality CFS Lite filament-sync fix. None add plugin API.
- Profiles changed (Anker, Prusa CORE One process presets, Snapmaker U1 machine clearances);
  no plugin API.

Documentation discrepancy to ignore:

- The wiki `host.md`/`plugin_development.md` says Script plugins run on the main/UI thread.
  The 2.5.1 and 2.5.2 source, the installed 2.5.2 binary docstrings, and working plugins all say
  Script `execute()` runs on a **background Script worker**. Use the source behaviour. The wiki
  `begin_update` omission is also a docs gap, not a 2.5.2 removal.

No native sync event was added in 2.5.2. The `.opc` system-preset loading path and the
`LifecycleEvents.hpp` enum are unchanged between 2.5.1 and 2.5.2.

## 9. Minimum live checks before relying on this

1. **Folder + subpackage:** create `orca_plugins/filament_match/filament_match.py` plus
   `filament_match_lib/core.py`; activate; confirm the log shows the plugin loaded and
   `import filament_match_lib.core` succeeds. This verifies the non-flat local layout.
2. **Activation:** copy the folder with no sidecar, restart, confirm it is listed as Inactive,
   activate it, restart, confirm it auto-loads. This verifies the `enabled` default.
3. **`begin_update`:** in `execute()`, call `begin_update(1, existing_preset_name).commit()` and
   log `result.slot_number`, `slot_assignment_changed`, and the exception type on an invalid
   slot/name. Confirms 1-based indexing and selector-name rules.
4. **`filament_id`:** for a system preset and a user preset, log `preset.file`,
   `os.path.exists(preset.file)`, `preset.config_value("filament_id")`, and
   `preset.config_keys()`. Expect system file missing and `None` for the ID.
5. **Bundle reload:** write `_local/filament_match/` under the **active** user folder, call
   `list_ids()`, `reload_local("filament_match")`, and verify the preset appears. Then try a
   wrong folder to confirm the unknown-id error.
6. **Native sync:** trigger “Synchronize filament list from AMS” with a log-only lifecycle
   handler. Confirm there is no dedicated sync event (a `PresetSelected` for slot 1 may appear,
   and dialogs may add more); confirm the polling snapshot sees all changed slots.

If any of these fail, the notes above are source-level expectations, not runtime guarantees.

## 10. Known integration issues

### 10.1 A single OFD/library candidate is shown as “No match” (fixed in 0.3.1)

Live evidence from the U1 and the current OFD index:

- printer slot 3: `vendor=3DJake`, `material=ABS`, `subtype=""`, `color=#4DB6AC`,
  `spool_id=7`;
- OFD lookup returns exactly one candidate:
  `3DJAKE ABS (OFD)`, source `library`, score 55;
- `_build_row()` returns `source="library"`, `preset_name=""`,
  `select_name=""`, `choose=False`, `candidates=[3DJAKE ABS (OFD)]`;
- the new panel sets `selected` from `row.preset_name` and only falls back to a
  candidate when `row.choose` is true. A single library candidate therefore
  leaves the preset input blank. `statusFor()` also falls through to “No match”
  because it only checks `preset_name`/`choose`, not `source == "library"`.

The preset is found; it was the display/selection path that discarded it. In
0.3.1 a lone library candidate is selected in the preset input and labelled
“Library match” instead of “No match”. The input value is `lib:<name>`; the
Match button sends that selected value and installs/selects the candidate. It
is still not auto-installed on scan.

### 10.2 Repeated Spoolman URL permission prompts (fixed in 0.4.1)

Orca’s reactive audit grants are persisted as the exact URL target, not as a
host. The sidecar showed separate entries for
`http://spoolman.blachut.me/api/v1/spool/3` and `/spool/7`, so each new spool
ID (and each scheme/redirect) prompted again.

The plugin now requests each base **origin** before any API path:

- printer origin from the active Orca printer preset (`print_host`/port),
- Spoolman origin from the plugin setting,
- Open Filament Database origin from the configured index URL,
- no hardcoded addresses.

A request to the bare origin (`http://host:7125`, no path) makes Orca persist a
canonical `scheme://host` grant. `PluginAuditManager::network_permission_matches`
treats that entry as host-wide, so later `/server/info`, `/printer/...`,
`/api/v1/spool/1`, `/api/v1/spool/2`, etc. are covered. The first scan may
prompt once per origin; approve it and press **↻**. If Spoolman is denied, the
plugin skips all spool requests for that scan instead of prompting per spool.
Reinstalling/updating still clears grants, so one prompt per origin returns
after an update.

`orca.request_permissions(...)` at plugin load could also produce a host-wide
declaration, but only in `register_capabilities()` and only for hosts known at
load. The origin preflight works with user-configured addresses without that
hardcoding.

### 10.3 History and Spoolman product names blocked edits (fixed in 0.4.0)

Two related matching problems:

- `match_observation` returned the history preset before looking at the current
  fields, so editing a previously applied feed and pressing Match could never
  change the candidate.
- Spoolman filament names are often only a colour ("Yellow") or material
  ("PETG Pink"). `_exact_metadata` treated those as a required product line, so
  a valid vendor/material preset failed exact matching and fell through to the
  generic preset.

Fixes:

- history entries are used only when the mapped preset still fits the current
  vendor/material/subtype; with no reported subtype, a variant-named history
  preset must also match the product. A changed refill falls through.
- the explicit Match path calls `_build_row(..., use_history=False)`, so the
  expanded fields always drive that re-match and can produce a candidate popup.
- colour-only and vendor/material-only product text is ignored for exact and
  fuzzy product scoring, but kept in the identity fingerprint.
- generic fallback now prefers the exact `Generic <material>` preset over a
  colour-closer variant such as `Generic PLA-CF`.
- when several exact vendor/material presets match and the reading has no
  subtype, the base preset (`Snapmaker PLA`) sorts before variants
  (`Snapmaker PLA Basic`, `Snapmaker PLA SnapSpeed`).

### 10.4 Scan and Match must not apply (fixed in 0.5.0)

The panel originally applied confident scan results through
`orca.host.preset.filament.begin_update` and applied the selected candidate from
Match. That made review meaningless: simply opening the panel or expanding a
row changed the slicer state, and a history entry could keep re-applying a
stale preset.

The flow is now explicit:

- `scan` builds rows only; no `begin_update`, no history write;
- `match` rebuilds one row with `use_history=False` and changes only the UI
  selection;
- the preset `<select>` and the candidate chooser change only the UI
  selection;
- the top **Apply** button posts `apply_all` with every changed row;
- `apply_all` calls the existing `_apply_selection` once per row, collects
  successes and per-slot errors, continues after a failure, installs library
  candidates, and only then writes confirmed history.

`applied` in a scan row therefore stays false; only an Apply result sets it.
The UI marks a row "Selected — press Apply" until then.

### 10.5 OFD and filesystem audit failures (fixed in 0.5.1)

Three independent problems made every non-Snapmaker spool fall back to Generic:

- `_library_candidates()` preflighted `https://api.openfilamentdatabase.org`
  before every lookup. That preflight is audited and denied on a background
  Script worker, so the function returned no candidates **before** reading the
  already-cached OFD index. Matching must read the local index first; network is
  only needed when the index is absent/stale or a preset JSON is not cached.
- `OfdLibrary.fetch()` had no on-disk preset cache. A cached index could name
  `3DJAKE ABS (OFD)`, but applying it always required a network fetch. Preset
  JSON is now cached under `<plugin storage>/ofd_presets/` and checked before
  any request; `_fetch_library_preset()` preflights only on a cache miss.
- Atomic JSON writes used `tempfile.mkstemp()` followed by
  `os.fdopen(descriptor, "w")`. CPython's audit `open` event for a file
  descriptor has no target, so Orca reports `targets=[<none>]` and denies it
  instead of applying the plugin-storage allowed-root shortcut. The fd is now
  closed and the temp path re-opened by name before `os.replace()`. History,
  settings, the OFD index and library presets all use that path.

Product matching also stopped treating grade words as product lines: `Basic`,
`Standard`, `Premium`, `Value`, `Regular` and `Performance` are removed from
`_observed_product_text()`. Thus Spoolman's `PETG Basic Transparent Green`
matches the installed `eSUN PETG`, while `PETG Matte` still constrains to the
matte preset. A grade word that is also the reported subtype is retained.

Spoolman's `extra["variant"]` (JSON-encoded, e.g. `"SnapSpeed"`) now fills a
missing `Observation.subtype`; the printer's own subtype stays authoritative.

### 10.6 Flattened build shadowed the matcher (fixed in 0.5.2)

The single-file build concatenates `filament_match_core.py`,
`filament_match_sources.py` and `filament_match.py` into one global namespace.
Both the core and the entry defined a top-level `_library_candidates`:

- core: `_library_candidates(value)` normalized candidates returned by
  `library_lookup`;
- entry: `_library_candidates(observation, settings, storage, session,
  warnings)` performed the OFD lookup.

The entry definition landed later and replaced the core helper. Inside
`match_observation`, `_library_candidates(library_lookup(obs))` then raised
`TypeError: missing 4 required positional arguments`, which was swallowed by
the stage's `except Exception`, so the matcher always saw an empty library and
fell through to `Generic <material>`. The three-module development layout and
the tests that monkeypatched the entry helper did not expose it.

Fixes:

- core's helper is now `_normalized_library_candidates`; core's private
  `_text` helper is now `_core_text` (the entry has its own `_text`).
- `build_single_file.py` parses all three modules and refuses to build if two
  modules define the same top-level function/class/assignment name.
- `test_integration_sim.py` now exercises the real library stage in both the
  source and flattened builds without monkeypatching `_library_candidates`.

The debug harness that found it launches a second Orca with
`--datadir <clone>`, an instrumented copy of the plugin in
`register_capabilities()`, and a dumped `filament_match_debug.out.json`. That
shows the real 407 compatible presets, the real OFD candidates, and each row's
final source. It does not touch the user's open project.

### 10.7 Project filament-slot count (0.6.0)

OrcaSlicer 2.5.2 exposes **no** API to add, remove or resize project filament
slots. Verified against the live bindings in an isolated second instance:

- `PresetBundle` has only `current_filament_preset_names()`,
  `current_filament_presets()`, `full_config_keys()`, `full_config_value()` and
  the preset collections. No setter.
- `Plater` has `model()`, `project_path()`, `export_3mf_copy()`,
  `is_project_dirty()`, `is_presets_dirty()`, `inside_snapshot_capture()`.
  No slot methods.
- `PresetCollection` and `Preset` are read-only.
- `PresetEditor.begin_update()` edits an existing preset; it cannot add a slot.
  Passing slot 5 when only four exist raises `IndexError`.
- OrcaSlicer issue **#17357** ("Plugin API: set filament slot colour and slot
  count") is still open.

A destructive experiment confirmed the workaround is not safe: setting the
active printer preset's `nozzle_diameter` vector through
`orca.host.preset.printer.begin_update("").set(...).commit()` is accepted by
the transaction but collapsed the project from four filaments to one; a
comma-separated nine-value attempt crashed the test instance. Do not manipulate
`nozzle_diameter` to fake a slot count.

Production behavior instead:

- `_slot_mismatch(feeders, slots)` returns counts and an actionable message.
- `scan` reports `slot_mismatch`; rows beyond the project slot count keep
  `slot_exists=False`.
- the panel shows **Project has fewer/more filament slots than the printer**,
  marks extra feeds **No Orca slot**, and excludes them from Apply.
- `apply_all` skips entries beyond the project slot count and returns
  `apply_skipped` plus one summary instead of one error per feed.
- extra project slots (feeders < slots) are left unchanged; the message states
  how many to remove.

A plugin cannot make Orca's slot count equal the feeder count without a C++
change to OrcaSlicer.

### 10.8 Compatible OFD parent remap and cache clear (0.6.1)

Live U1 preset flags from the isolated debug instance:

```text
Generic PETG @System           compatible=False
Generic PETG @U1 0.4 nozzle    compatible=True
Generic PLA  @System           compatible=False
Generic PLA  @U1 0.4 nozzle    compatible=True
Generic ABS  @System           compatible=True
```

The U1 vendor bundle shadows the system PLA/PETG generics with its 0.4-nozzle
variants, so the OFD presets that inherit the system generics were rejected by
`_validated_library_preset()` even though the OFD JSON was valid.

`_compatible_library_parent()` now resolves the declared parent as follows:

1. use the declared parent when it is present and compatible;
2. otherwise, if the declared base is `Generic <material>`, find a compatible
   preset with the same base and material;
3. prefer the variant whose nozzle suffix matches `snapshot["printer_nozzle"]`,
   then a system preset, then the lowest rank/name;
4. rewrite `preset["inherits"]` to the resolved parent and add a warning.

Non-generic parents are never remapped; they still fail when incompatible.

The Settings tile also has **Clear OFD cache**. The `clear_cache` command runs on
the UI thread and deletes:

```text
<plugin storage>/ofd_index.json
<plugin storage>/ofd_presets/
```

It resets `session["ofd_library"]`/`session["ofd_key"]`, so the next scan builds
a new `OfdLibrary` and downloads fresh data. It deliberately keeps
`history.json`, `settings.json` and installed presets.

### 10.9 Filament price synchronization (0.7.0)

Orca's price field is `filament_cost`, in **currency per kilogram**.
Spoolman's `spool.price` and `filament.price` are whole-spool prices. The
mapping is:

```text
cost_per_kg = (spool.price ?? filament.price)
              / ((spool.initial_weight ?? filament.weight) / 1000)
```

The scan already fetched each spool in `_enrich_spoolman()`, so price extraction
adds no request. `spoolman_status` and `spool_prices` are stored in the plugin
session; the scan reply exposes them. Rows carry `current_price` and
`spool_price`.

The expanded feed section has current price (read-only), new price (editable),
Fetch price and Save price. New price is auto-filled on the first scan, preserved
across rescans when touched, and unchanged when the preset changes. Save stages
`price_pending`; Apply sends the price.

Applying a price:

- OFD library preset: `filament_cost` is added before writing the local JSON.
- plugin-owned local preset: its JSON is read, `filament_cost` is set, written,
  and `reload_local()` is called.
- system or user preset: a local copy named `<base> (local)` is created,
  inheriting the chosen preset. This is required because Orca 2.5.2 has no
  plugin API to save an existing system/user preset. The copy includes explicit
  `filament_vendor`, `filament_type`, `filament_colour` and
  `filament_cost`, is written under `_local/filament_match`, reloaded, and
  selected. History is written against the copy's internal name.

The core matcher strips a trailing `(local)` from preset names/base when
computing product texts, so the price copy still matches the original vendor
filament and a spool history mapping to the copy survives rescans. `filament_cost`
is not used by any matching stage; there is a regression test for that.

Fetch price bypasses the per-session Spoolman spool cache so an updated price is
visible immediately. A missing price/weight yields a clear error rather than
writing a wrong per-kg value. With no Spoolman URL or an unreachable URL,
Fetch is disabled in the UI and manual prices still work.

### 10.10 Local bundle names in matching (0.7.1)

Orca exposes a local bundle preset's internal `Preset.name` as
`_local/<bundle_id>/<base>`, while the selector uses `<base>`. Core matching
must normalise that prefix, otherwise:

- `_name_without_vendor()` leaves `_local/filament_match/eSUN PETG`;
- `_preset_product_texts()` never produces the product text `PETG`;
- `_exact_metadata()` rejects the local price copy;
- the original system preset becomes the only exact candidate, so no chooser
  opens.

`strip_bundle_prefix()` now removes `_local/<id>/` and `_subscribed/<id>/`
before the existing marker normalisation. This makes `_local/filament_match/eSUN
PETG (local)` match as `eSUN PETG (local)` / `PETG`, so:

- the local copy is an exact candidate;
- its rank 1 sorts before the system preset;
- scan preselects it silently;
- Match returns both and opens the chooser because `choose=True`.

### 10.11 User-preset price copies and custom labels (0.7.2)

Two additional failures surfaced with the CC3D custom preset:

1. **Legacy install path.** Orca was loading
   `orca_plugins/filament_match.py/filament_match.py` (0.7.0) instead of the
   installed `orca_plugins/filament_match/filament_match.py` (0.7.1). The
   older folder shadowed every later fix. `install.sh` now removes
   `orca_plugins/filament_match.py` before installing.

2. **User-preset parent.** A price copy of a user preset was written as
   `inherits: "CC3D PETG (custom)"`. Orca's local bundle loader runs before
   user presets are resolvable and logs
   `can not find parent CC3D PETG (custom)`, then skips the file. The copy was
   therefore never a candidate. For a user preset, `_create_price_copy()` now
   flattens the user preset's own config (and any user ancestors) into the
   copy and inherits only the first system/non-user parent, normally
   `Generic PETG @System`. A system preset copy still inherits the system
   preset directly.

3. **Custom labels.** `CC3D PETG (custom) (local)` did not exact-match
   `PETG Pink`. Core now strips repeated trailing labels `(local)`, `(custom)`,
   `(OFD)` and `(copy)` for product/name matching. A `(local)` price copy has
   explicit precedence in exact and fuzzy sorting, so it ranks before the
   original user/system preset. With multiple exact candidates, `choose=True`
   and Match opens the chooser.
