# Filament Match — implementation plan

**Goal:** an OrcaSlicer 2.5.2+ plugin that reads the filaments loaded in a printer,
matches each one to an Orca filament preset, lets the user review/edit the
detected fields and the chosen preset, and remembers confirmed
spool/tag/filament → preset mappings across Orca restarts.

**Non-goals (v0.1):** replacing Orca’s native AMS sync button, building a full
preset editor, generating profiles from Spoolman, or silently auto-applying
low-confidence matches.

---

## Why this shape

- Orca has no durable “this spool was used with preset X” history. The plugin
  must own that store.
- Native AMS sync cannot be overridden by a Python plugin. The plugin exposes an
  explicit **Filament Match** action (Plugins → Run) and can optionally correct
  slots after a native sync.
- Printer data is incomplete and heuristic. The UI is review-first: show what
  was detected, what matched, why, and let the user confirm.

---

## File layout

```
plugin/
  filament_match.py          # Orca entry, action, UI, host API calls
  filament_match_core.py     # pure Python: types, matching, history
  filament_match_sources.py  # printer / Spoolman / OFD adapters
  tests/
    test_core.py
    test_sources.py
    fixtures/…
  README.md
  INSTALL.md
  PLAN.md                    # this file
```

`filament_match.py` adds its own directory to `sys.path` and imports the two
modules. The whole folder is copied into
`<Orca data dir>/orca_plugins/filament_match/` for local testing.

---

## Core types (`filament_match_core.py`)

```python
@dataclass
class Observation:
    slot: int                    # 1-based Orca slot
    present: bool = True
    vendor: str = ""
    material: str = ""           # PLA, PETG, ABS…
    subtype: str = ""            # SnapSpeed, Silk, Matte…
    product: str = ""            # "PolyLite PLA", "PETG-Basic", …
    color: str = ""              # "#RRGGBB"
    filament_id: str = ""        # Orca filament_id, when the printer reports it
    spool_id: int | None = None
    tag_uid: str = ""
    bed_temp: int | None = None
    nozzle_temp: int | None = None
    source: str = ""             # snapmaker / moonraker
    raw: dict = field(default_factory=dict)

@dataclass
class PresetInfo:
    name: str
    base: str
    vendor: str
    material: str
    subtype: str
    color: str
    filament_id: str
    compatible: bool
    system: bool
    user: bool
    bundle_id: str
    file: str
    rank: int                    # user=0, bundle=1, system=2
    config: dict = field(default_factory=dict)

@dataclass
class Candidate:
    preset_name: str
    score: int
    reasons: list[str]
    source: str                  # history / installed / library / generic
    preset: PresetInfo | None = None
    library_url: str = ""
    library_meta: dict = field(default_factory=dict)

@dataclass
class MatchResult:
    observation: Observation
    preset_name: str = ""
    source: str = ""
    confidence: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    error: str = ""
```

### Identity and history

`identity_key(obs)` order:

1. `spool:<id>` if `spool_id` is set.
2. `tag:<uid>` if `tag_uid` is set.
3. `filament:<sha1(vendor|material|subtype|product|color)>` otherwise.

`HistoryStore(path)`:

```json
{
  "version": 1,
  "spools": {
    "42": {
      "default": "CC3D PETG @U1 0.4",
      "printers": {"Snapmaker U1 (0.4 nozzle)": "CC3D PETG @U1 0.4"},
      "updated": 1795280000
    }
  },
  "tags": { "04A7…": { … } },
  "filaments": { "cc3d|petg|||f06292": { … } }
}
```

- `get(obs, printer)` → printer-specific mapping, else `default`.
- `remember(obs, printer, preset)` → writes the mapping atomically.
- `forget(obs)` / `clear()`.
- Only user-confirmed matches are stored; never Orca’s generic fallback.

### Matching pipeline

`match_observation(obs, presets, history, printer, library_lookup=None)`:

1. **History** — exact key, preset still exists/compatible, and the preset
   still fits the current vendor/material/subtype/product/colour. A stale refill
   mapping falls through. Explicit Match ignores history entirely.
2. **Exact `filament_id`** — among compatible installed presets; one → select,
   many → candidates.
3. **Exact vendor + material + subtype + product** — one → select, many →
   candidates. Colour-only or vendor/material-only product names are treated
   as unknown for this constraint.
4. **Fuzzy** — score vendor/material/subtype/product token overlap, colour
   distance, name prefix. Threshold 70; require a margin of 10 over the next
   candidate for automatic selection.
5. **Library** — call `library_lookup(obs)`; return `Candidate(source="library")`
   with URL/metadata; do not auto-install. The index and any previously fetched
   preset JSON are cached under plugin storage, so this stage works without
   Orca network permission. A network request is needed only for an uncached
   preset or an absent/stale index.
6. **Generic** — the exact compatible `Generic <material>` preset; variants
   such as `Generic PLA-CF` are only used when no exact material generic exists.
7. **Error** — no match.

`score_preset(obs, preset)` returns `(score, reasons)`. Normalize case,
punctuation, `+`, colour words, `@Printer`, and bundle prefixes.

---

## Sources (`filament_match_sources.py`)

### `read_printer(base_url, api_key="") -> list[Observation]`

Try both, merge by slot:

1. **Snapmaker**:
   `GET /printer/objects/query?print_task_config&filament_detect`
   - `filament_exist`, `filament_type`, `filament_sub_type`,
     `filament_color_rgba`, `filament_vendor`
   - `filament_spool_id` when present
   - `filament_detect.info[i].BED_TEMP`, `.HOTEND_MAX_TEMP`
2. **Moonraker**:
   `GET /server/database/item?namespace=lane_data`
   - `lane`, `material`, `color`, `filament_id`, `spool_id`,
     `bed_temp`, `nozzle_temp`
3. If only one source works, use it. If neither, return `[]` with an error.

`product` is filled from Spoolman when available, else left blank.

### `SpoolmanClient(base_url)`

- `get_spool(id)` → `/api/v1/spool/{id}`
- `get_filament(id)` → `/api/v1/filament/{id}`
- `find_filament_by_extra(key, value)` → `/api/v1/filament?…` fallback; use
  `orca_filament_preset` / `orca_preset` if present.
- Values in `extra` are JSON-encoded; decode.
- All requests timeout, no credentials by default.

### `OfdLibrary(index_url, cache_path)`

- Default index:
  `https://api.openfilamentdatabase.org/orcaslicer/index.json`
- Cache the index in plugin storage; refresh if older than 24 h or forced.
- `find(vendor, material, product, subtype)` → list of candidates with
  `brand`, `filament`, `profile_name`, `path`, `source`.
- `fetch(candidate)` → one preset dict:
  `https://api.openfilamentdatabase.org/orcaslicer/{path}`
- Do not fetch the whole library; only the chosen preset.
- Validate: `name`, `from`, `inherits`, no system-only keys; parent exists
  is checked by the entry before install.

---

## Orca entry/UI (`filament_match.py`)

### Action

- Script capability `get_name() -> "Filament Match"`.
- Plugins → Run / Speed Dial opens a docked panel beside the sidebar.
- Settings: optional Spoolman URL, library enabled/index URL; printer address
  comes from the active Orca printer. Store in `storage()/settings.json`.

### Panel

- Filament feeds first; a collapsed **Settings** tile at the bottom.
- Scanning starts automatically when the panel loads and only **selects**
  candidates; it never changes an Orca slot.
- Column per detected filament (slot T1…Tn):
  - **Preset input**: compatible installed presets plus library candidates;
    selected = match. Choosing another entry changes the selection only.
  - **Edit** button: opens a popup with the preset's settings (read-only; the
    plugin cannot open Orca's native editor).
  - **Expand**: fields used for matching (vendor, material, subtype, product,
    colour, filament_id, spool_id, tag_uid).
  - **Match** button at the bottom: re-run pipeline and change the selection.
  - Red error / amber pending / green applied label per feed.
  - Multiple viable matches on the first scan: select the best candidate, no
    popup. Pressing Match opens a candidate popup only if Match produces more
    than one candidate; choosing one changes the selection. A single library
    candidate is preselected and labelled “Library match”.
  - A top **Apply** button next to **↻** writes every changed selection to
    Orca, installs library candidates, and writes the confirmed history
    entries. Per-row failures do not stop the remaining rows.

### Host calls

- Read presets on UI thread:
  `orca.host.preset_bundle().filaments`, `preset_names()`, `find_preset()`,
  `preset.config_value(...)`, `preset.file`, `preset.is_compatible`,
  `preset.is_visible`, `preset.is_system`, `preset.bundle_id`, `preset.is_user()`.
  Read `filament_id` from the preset JSON file when available; otherwise leave
  blank and match by name/metadata.
- Select: `orca.host.preset.filament.begin_update(slot, name).commit()`.
  This is undocumented; wrap errors and report them.
- Reload: `orca.host.preset.bundles.reload_local("filament_match")`.
- Install library preset: write JSON to
  `user/<folder>/_local/filament_match/filament/<name>.json` +
  `bundle_metadata.json`, reload, then select.
- Storage: `orca.host.plugin.storage()` for `history.json`, `settings.json`,
  `ofd_index.json`.
- Threading: `execute()` runs on the script worker; `begin_update` and network
  there; preset snapshot on the UI thread. Use a queue like
  `orca_spoolman_presets.py`.

### Native sync

- Do not try to override it.
- Optionally add a “Correct generic slots” action that runs after native sync:
  read current slot presets, find slots whose preset is `Generic …`, run match
  and offer the dialog. MVP: explicit action only.

---

## MVP phases

1. **Core + history + installed presets + UI action**
   - Read printer, list observations.
   - Match installed presets, review dialog, apply, remember.
   - Persist history across restarts.
2. **Spoolman optional**
   - Resolve `spool_id` to vendor/product/temps.
   - Read `orca_filament_preset` / `orca_preset` as a first-class history hint.
3. **OFD library**
   - Index cache and preset-JSON cache; one-preset fetch only on a cache miss;
     validate/install, then select.
4. **Post-native-sync correction**
   - Detect generic slots and offer review.

---

## Testing

- `test_core.py`: identity, history precedence, exact/fuzzy/generic matches,
  multiple candidates, no-match.
- `test_sources.py`: parse sample Snapmaker `print_task_config`, Moonraker
  `lane_data`, Spoolman spool/filament, OFD index/profile.
- No Orca dependency in core/sources; entry import guarded with `try: import orca`.
- Manual acceptance in Orca 2.5.2:
  - install folder, restart, activate, run action;
  - detects four U1 slots;
  - dropdown lists compatible presets;
  - Apply changes slots;
  - history survives restart;
  - printer/Spoolman down produces an error, not a crash.

## Known limitations to state in README

- Cannot override Orca’s native AMS sync.
- Cannot open Orca’s native preset editor.
- `begin_update` is undocumented.
- Generic Moonraker gives no vendor/subtype; those fields stay blank.
- Library presets must inherit a parent that exists; otherwise they are refused.
- Do not run alongside another slot-selecting plugin.
