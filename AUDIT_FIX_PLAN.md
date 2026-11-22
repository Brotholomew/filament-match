# Filament Match — Audit Fix Implementation Plan

Status: **implementation complete on `fix/audit-2026-11-22`; awaiting manual sign-off and release approval**. No tag or release has been created.

Target: fix every confirmed finding from the audit, keep the currently working Snapmaker U1 / Spoolman / OFD / price-copy / slot-mismatch behavior intact, and add regression coverage for every repaired path.

Current verification evidence:

- Full local suite: **440 tests, OK**
- Independent audit harness: **14/14 checks pass**
- CI matrix: **Ubuntu, macOS and Windows, all green**
- Real Orca clone:
  - canonical slash/quote local preset names load and select;
  - OFD candidate installs, reloads and selects;
  - CC3D user-preset price copy inherits `Generic PETG @U1 0.4 nozzle`, preserves temperatures and applies `10.07/kg`;
  - live scan against the mock printer returns the expected candidates.

---

## 1. Release freeze and branching

- Freeze `main`.
- Create an integration branch:

  ```sh
  git switch -c fix/audit-2026-11-22
  ```

- Use one git worktree per parallel workstream so workers never edit the same checkout.
- No `git tag`, GitHub release, or Orca Cloud publish until Stage 8 passes and you explicitly approve.
- Version bump is deferred to Stage 8. Proposed release version: `0.9.0` (behavioral changes + migration).

---

## 2. Parallel workstreams and file ownership

| Worker | Owns | Must not edit |
| --- | --- | --- |
| **Core** | `plugin/filament_match_core.py`, `plugin/tests/test_core.py` | entry, sources, build |
| **Sources** | `plugin/filament_match_sources.py`, `plugin/tests/test_sources.py` | core, entry |
| **Entry** | `plugin/filament_match.py`, `plugin/tests/test_integration_sim.py`, `plugin/tests/test_entry_static.py` | core, sources |
| **Build** | build scripts, `install.sh`, workflows, docs | runtime modules |
| **Verify** | read-only test runs, clone/Orca E2E, audit repro scripts | all production files |

Core and Sources start in parallel after Stage 0. Entry starts with the preset-filesystem fixes, then the UI fixes sequentially because both touch `filament_match.py`. Build starts in parallel from Stage 0. Verify runs after each integration gate and owns the final independent check.

### Worker execution protocol

Use one worktree per worker and never let two workers edit the same file:

```sh
git worktree add ../filament-match-core    -b fix/core    fix/audit-2026-11-22
git worktree add ../filament-match-sources -b fix/sources fix/audit-2026-11-22
git worktree add ../filament-match-entry   -b fix/entry   fix/audit-2026-11-22
git worktree add ../filament-match-build   -b fix/build   fix/audit-2026-11-22
```

Each worker must:

1. Read this plan and the relevant audit findings.
2. Add failing regression tests first.
3. Change only its owned files.
4. Run its focused tests plus the full suite.
5. Report: files changed, tests run, test output, remaining risks.
6. Never tag, release, publish, merge to `main`, or edit another worker's files.

An integration worker merges in the stated order and resolves conflicts. A separate verification worker re-runs the audit repros and E2E matrix on the merged branch; it may report failures but must not fix them itself.

Suggested parallel schedule:

| Stage | Core | Sources | Entry | Build | Verify |
| --- | --- | --- | --- | --- | --- |
| 0 | baseline + repro tests | baseline + repro tests | baseline + repro tests | baseline | harness |
| 1 | normalization/history | OFD/printer work | PF preset filesystem | install/CI work | harness |
| 2 | matching gates | OFD/printer work | PF preset filesystem | release plumbing | review |
| 3 | review | OFD/printer work | UI fixes | release plumbing | review |
| 4 | review | review | UI fixes | docs/CI | E2E prep |
| 5 | done | done | UI verification | done | E2E |
| 6 | — | — | fixes from E2E | fixes from E2E | full matrix |
| 7 | — | — | — | — | independent sign-off |

Core and Sources never touch the same file. Entry owns all `filament_match.py` work, so PF and UI are sequential within that worker. Build can run from the start because it owns only build/install/docs/workflow files.

Integration merge order: **Core → Sources → Entry → Build → Verify**.

Freeze these interfaces before parallel work:

- Core exports `normalize_text`, `normalize_vendor`, `normalize_material`, `identity_key`, `HistoryStore`, `match_observation`, `Candidate`, `PresetInfo`, `Observation`.
- Entry must not duplicate normalization or identity logic.
- `HistoryStore` keeps its public methods; new legacy-key fallback is internal.
- Any history schema change bumps `version` and keeps a read path for the old format.

---

## 3. Stage 0 — Baseline, repro tests, and E2E harness

### Tasks

- **S0-1 Baseline capture**
  - Run the full 221-test suite.
  - Build `plugin/dist/filament_match.py` and `plugin/hub/filament_match_any.py`; record SHA-256.
  - Capture current snapshots for: Snapmaker 4-slot scan, CC3D custom/local, OFD install, price copy, Spoolman price, slot mismatch, history.
- **S0-2 Add failing audit regression tests**
  - Add `plugin/tests/test_audit_regressions.py` (or split by area) covering the confirmed repros:
    - non-ASCII vendor collision/history;
    - local preset name vs sanitized filename;
    - system-only folder inference;
    - eSUN-vs-ACME fuzzy auto-selection;
    - plain PLA vs `Generic PLA-CF`;
    - `PLA+` vs `PLA`;
    - `filament_id` wrong-vendor bypass;
    - exact wrong-colour selection;
    - invalid UTF-8 history and false “remembered”;
    - OFD stale cache;
    - 1-based Snapmaker maps and `lane0`/`lane1`;
    - Spoolman `/api/v1` duplication and IPv6;
    - ambiguous Apply;
    - staged price across spool swap.
  - Initially these tests must **fail** for the expected reason.
- **S0-3 E2E harness**
  - Add a repeatable script that clones the Orca data directory, writes a test local bundle, starts Orca with `--datadir`, and dumps `Preset.name`, `alias`, `label()`, `file`, `bundle_id`.
  - Add a mock-printer scenario runner that can switch between: Snapmaker arrays, 1-based maps, Moonraker lane maps, non-ASCII spools, PLA+, generic-only, and OFD slash/quote names.
- **S0-4 Scope decision: Moonraker**
  - Confirm whether full generic Moonraker support is in scope.
  - Minimum acceptable fix: make the endpoint configurable and return an explicit “unsupported Moonraker filament manager” error rather than silently reporting no filaments.
  - Preferred fix: fixture-driven adapters for common managers (AFC, Happy Hare, `mmu`, `lane_data`).
  - Record the decision in `plugin/API_NOTES.md`.
- **S0-5 Orca-native fallback/upstream API investigation**
  - Confirm the 2.5.2 plugin bindings expose only `current_filament_preset_names()` / `current_filament_presets()` and `full_config_value("filament_ids")`, not raw AMS trays or a native sync trigger.
  - Decide whether the no-network fallback (SA-12) is in scope and whether to draft the upstream API proposal (SA-14).
  - Record the findings in `plugin/API_NOTES.md`.

### Gate 0

- Baseline suite passes.
- New repro tests fail with the documented messages.
- E2E harness can start a cloned Orca and dump the preset bundle.
- No production code changed.

---

## 4. Stage 1 — Core normalization, identity, and history

Owner: **Core**. Can run in parallel with Sources and Build.

### CM-01 / HS-05 — Unicode normalization and legacy history

- Replace `[^0-9a-z]` normalization with Unicode-safe normalization:
  - NFKD decomposition;
  - strip combining marks;
  - map `™`, `®`, `©` to empty;
  - retain Unicode letters/numbers;
  - casefold and collapse whitespace.
- Never let an empty normalized vendor/product silently bypass the history vendor check.
- Include the raw value in `identity_key` when normalization would otherwise be empty.
- Add history `version: 2` and a legacy-key fallback:
  - look up the new key first;
  - on miss, compute the pre-fix key and try it;
  - write only the new key on `remember()`.
- Tests: Cyrillic, Greek, CJK, accented Latin, trademark names, two different non-ASCII vendors, exact matching, history no cross-vendor returns.

### HS-01 / HS-02 — History file and remember semantics

- Catch `UnicodeError`, not only `OSError`.
- Read with `utf-8-sig`; accept a BOM.
- Do not overwrite an unreadable/corrupt history file until it has been parsed.
- Make `_EmptyHistory.remember()` failure observable; `_apply_selection()` must check the return value before saying “remembered”.
- Surface `load_error` in the UI warning.
- Tests: invalid UTF-8, BOM, read-only file, corrupt JSON, failed remember message.

### HS-03 / HS-04 — History cascade and printer fallback

- `_lookup_history()` tries spool → tag → filament identity in order.
- `HistoryStore.get()` tries the printer-specific entry, then the default.
- If the printer-specific preset no longer exists/is incompatible, retry the default before falling through to normal matching.
- Tests: spool ID appears after tag history; stale printer entry with valid default; deleted preset.

### CM-11 — Provenance stripping

- `strip_bundle_prefix()` must remove only known markers:
  - `_local/<bundle>/`
  - `_subscribed/<bundle>/`
  - `[OFD]`
  - `bundle:`, `local:`, `user:`, `system:`
- Do not strip arbitrary leading `(...)`.
- Test `(Matte) eSUN PETG` remains distinguishable from `eSUN PETG`.

### CM-12 / CM-13 / CM-15 — Low-level identity hygiene

- Hex parser: reject bare six-letter words such as `facade`; accept integers and `0x` prefixes.
- `_compatible` must parse `"false"`/`"0"` as false, not `bool("false")`.
- `_preset_name` must not persist `repr(preset)` for a malformed preset.
- Reject all-zero/placeholder tag UIDs.
- Tests for each.

### Gate 1

- All core tests pass.
- All 221 existing tests still pass.
- New Unicode/history tests pass.
- Flat build imports; no duplicate-name regressions.
- Entry and sources untouched.

---

## 5. Stage 2 — Matching gates and selection quality

Owner: **Core**. Continues after Gate 1; Sources/Build remain parallel.

### CM-02 — Vendor/material hard gates

- When observed vendor is known, require canonical vendor equality for auto-selection.
- When observed vendor is unknown, do not auto-select a branded preset unless product/subtype evidence is strong; return candidates instead.
- Material mismatch is a veto, not just a score reduction.
- Tests: eSUN vs ACME (currently 71), unknown vendor, material mismatch.

### CM-03 — Generic variant rejection

- Parse the generic name after `Generic <material>`.
- Reject residual variant tokens unless the observation supports them: `CF`, `GF`, `Silk`, `Matte`, `HF`, `SnapSpeed`, `Basic` where appropriate.
- Rank an exact base generic ahead of every variant.
- Normalize `PET-G` in the generic name.
- Tests: plain PLA with only `Generic PLA-CF`; `Generic PET-G`; exact base vs variant.

### CM-04 — Material family aliases

- Canonicalize common families:
  - `PLA+`, `PLA Plus` → `PLA` (preserve `+` as a subtype/detail if needed);
  - `PETG-CF`, `PLA-CF` → material family + subtype;
  - `PA`, `Nylon` → same family;
  - `TPU-95A`, `TPU 95A` → TPU + hardness.
- Use the same canonical material in exact matching, generic lookup and identity.
- Tests for each alias.

### CM-05 — `filament_id` validation

- Do not return confidence 100 solely on ID.
- Validate vendor/material/colour, or treat ID as a strong candidate and require a chooser on conflict.
- Tests: wrong vendor, wrong material, wrong colour, exact match.

### CM-06 — Exact colour veto

- Use colour distance as a veto or tie-break for automatic selection.
- Return candidates when only colour differs.
- Tests: unique wrong-colour preset; multiple colour variants.

### CM-07 — Product/qualifier semantics

- Decide and document the `PLA Basic` policy.
- Use `_observed_product_text()` in history validation instead of the raw product.
- Keep colour-only and material-only product names unconstrained.
- Tests: `PLA Basic`, `PETG Pink`, `White`, `ABS Pastel`, `PETG Matte`.

### CM-08 — Duplicate preset names

- Key candidate metadata by object identity or `(name, bundle_id, file)`.
- Deduplicate identical presets before matching.
- Tests: two presets with the same name, different scores/reasons; chooser labels.

### CM-09 — Library candidate normalization

- Normalize `reasons`: string → one-item list; `None` → empty.
- Accept 0–1 and 0–100 scores.
- Deduplicate by `(preset_name, library_url)`.
- Tests.

### CM-10 — Near-tie margin

- Compare the top score against the next scored candidate even when the next is below the threshold.
- Tests: 70 vs 69 must open a chooser, not auto-select.

### Gate 2

- All core tests pass.
- Golden match table reviewed for the existing U1 scenarios.
- No unexpected changes to Snapmaker/CC3D/eSUN behavior.
- Flat build parity test passes.

---

## 6. Stage 3 — Sources, OFD, and Spoolman

Owner: **Sources**. Starts after Gate 0 in parallel with Core/Build.

### SA-01 / SA-02 — OFD cache correctness

- Track `_loaded_at`; enforce `cache_ttl` on memory hits, not only on first load.
- `refresh()` clears `_presets`, disk preset cache, and memory index before fetching.
- Scope `ofd_index.json` and `ofd_presets/` by a hash of `index_url`.
- Validate cached preset JSON before returning it; re-fetch invalid/empty files.
- Tests with a fake clock and two index URLs.

### SA-03 — Printer schemas: mirror Orca's native code

- Use [plugin/dev/ORCA_PRINTER_DATA.md](plugin/dev/ORCA_PRINTER_DATA.md) as the authoritative schema reference.
- Snapmaker: `/printer/objects/query?print_task_config&filament_detect`; slot count from `filament_exist`; arrays for type/subtype/colour/vendor; optional `filament_detect.info` temperatures.
- Moonraker: try `/server/database/item?namespace=lane_data`, then `/printer/objects/query?mmu`, then `/printer/objects/query?box`, first success wins.
- Use the configured host/port from Orca; do not probe port 80/7125 blindly.
- Do not invent vendor/product/spool/tag fields for Moonraker; native only reads `lane`, `material`, `color`, `bed_temp`, `nozzle_temp`, string `filament_id`.
- Tests: exact fixtures for Snapmaker arrays, lane_data, Happy Hare, and Creality CFS; unsupported/empty payloads.

### SA-04 / SA-05 — Lane mappings

- Snapmaker: use array positions and `filament_exist` length; do not apply 1-based mapping heuristics.
- Moonraker: use the `lane` field as a **string**, exactly like native `safe_json_string`; skip numeric/empty/invalid/negative `lane`.
- Do not use the `lane_data` object key for numbering.
- Happy Hare: use the gate array index.
- Creality CFS: use `(tray_idx - 1) * 4 + slot_i`.
- Tests for: Snapmaker arrays, string `lane`, numeric `lane` (must skip), keys that disagree with `lane`, HH gates, CFS T1/T2.

### SA-06 — URL normalization (Spoolman and printer)

- Strip a trailing `/api/v1` (and `/api/v1/`) before joining Spoolman paths.
- Accept bare hosts in the entry validation.
- Bracket IPv6 hosts, with or without port, for Spoolman and printer base URLs.
- Make a non-standard printer port configurable rather than relying only on 7125/80.
- Tests: `host`, `host:7912`, `host/api/v1`, `fd00::1`, `[fd00::1]:7912`, printer at `:7126`.

### SA-07 — Spoolman extras

- Add pagination/filter fallback for `find_filament_by_extra`.
- Add type-aware comparison.
- Tests with a capped/paginated fixture.

### SA-08 — Local OFD index on Windows

- Detect absolute/drive paths before URL parsing.
- Use `os.path.join` for local preset paths.
- Tests with a Windows-style path (can run on Linux with `ntpath`).

### SA-09 / SA-10 — OFD validation and thread safety

- Reject non-filament `type` values.
- Allow legitimate user `filament_id` fields or document the restriction.
- Assign `_profiles` before `_index` under a lock, or do not share the instance across threads.
- Tests.

### SA-12 — Orca-native slot fallback

- Add an explicit fallback that reads `bundle.current_filament_preset_names()` / `current_filament_presets()` when the printer network request is denied or unavailable.
- This fallback can show the current project slots and their assigned presets, but it cannot recover the raw AMS vendor/product/colour/spool ID, so it must **not** pretend to perform OFD matching from tray data.
- Label it clearly in the UI: "Using Orca's current slots; rescan with printer access for OFD matching."
- Tests with a fake bundle.

### SA-13 — Permission-prompt reduction

- Query exactly the native source for the detected agent:
  - Snapmaker → `print_task_config&filament_detect`
  - Moonraker → `lane_data` → `mmu` → `box`, first success wins.
- Use `print_host` + configured `printhost_port`; do not probe port 80 and 7125 when the port is known. Native `normalize_base_url` has no default port.
- Preflight each origin exactly once, then reuse the grant.
- Preflight the OFD index origin before the first `library.find()`; do not issue one request per row.
- Cache successful printer/OFD/Spoolman data for the scan; avoid repeated prompts.
- Add an explicit "no network / use current Orca slots" setting.
- Tests: only one preflight per origin, one printer endpoint, no OFD request when network mode is off.

### SA-14 — Upstream Orca API proposal (documentation)

- Document that Orca's native `Sidebar::sync_ams_list` / printer-agent tray data is not exposed to Python.
- Draft an upstream issue/PR for a read-only API such as `orca.host.printer.filament_trays()` and an optional `sync_filaments()`/completion event.
- Do not block the release on upstream acceptance; the HTTP path remains the current source of raw AMS data.

### SA-11 — Network hardening

- For private/loopback hosts, avoid environment proxy routing where appropriate.
- Reject or strip auth headers on cross-origin redirects.
- Add one retry for transient errors.
- Keep timeouts finite.
- Tests with a local redirect/proxy stub.

### Gate 3

- All source tests pass.
- No network in CI; fake opener for every OFD/Spoolman test.
- Existing printer fixtures still parse identically.
- OFD cache tests prove refresh and URL switching.

---

## 7. Stage 4 — Preset filesystem and local bundle

Owner: **Entry**. Starts after Gate 0 with PF tasks, in parallel with Core/Sources/Build.

### PF-01 — Canonical local preset names

- Define one canonical name:
  - `canonical = _safe_filename(display_name)`.
- Write the canonical value to:
  - the JSON filename stem;
  - JSON `name`;
  - `filament_settings_id`;
  - history;
  - `select_name` when appropriate.
- Add a compatibility read path for existing presets whose file stem and history name differ.
- Add an E2E test using `AzureFilm TPU/TPE (OFD)` and a quote-containing name:
  - file is created;
  - Orca loads it;
  - `begin_update` selects it;
  - history resolves after restart;
  - `label()`/`name` are recorded.

### PF-02 — User-folder discovery

- Add `_discover_user_folder(data_dir, snapshot)`:
  - check the selected printer preset path;
  - enumerate `data_dir/user/*`;
  - score folders by `machine`, `filament`, `process`, and recent files;
  - recognize `_local` and `_subscribed`;
  - prefer the active cloud user folder when identifiable;
  - fall back to an existing `user/default`.
- Remove the hard `folder_inferred` failure when a safe folder can be discovered.
- Add the missing **Refresh presets** UI action.
- Tests: system-only printer, subscribed printer, default folder, UUID/cloud folder.

### PF-03 — User price-copy flattening

- If the source user preset file is unreadable, fail with a clear error instead of silently dropping settings.
- Prefer the live preset config for current values.
- Tests: unreadable file, cloud-only preset, parent user.

### PF-04 — Price-copy collisions

- Include a source discriminator in the copy identity: full source name/nozzle or a stable hash.
- `taken()` must consider existing plugin copies and their `inherits`.
- Track created stems within one `apply_all` batch.
- Tests: two presets with the same base, two nozzle variants, two slots in one Apply.

### PF-05 — Local parent restriction

- `_compatible_library_parent()` must not return a user preset for a local bundle.
- Restrict to system/bundle parents or flatten the user parent.
- Use numeric nozzle comparison (`0.4` == `0.40`).
- Tests.

### PF-06 — Write/reload rollback

- Validate every config key against `preset.config_keys()` / `full_config_keys()` before writing.
- Keep a backup of the target file.
- On `reload_local()` failure, restore/delete the file and reload again.
- Apply the same protection to price updates.
- Tests with a fake reload that raises.

### PF-07 — Windows-safe filenames

- Truncate by UTF-8 bytes, not characters.
- Check reserved names before the extension (`CON.json`, `PRN.json`, etc.).
- Strip trailing dots/spaces after truncation.
- Add a collision hash.
- Tests on macOS/Linux and Windows CI.

### PF-08 — JSON reads

- Read preset/settings/history JSON with `utf-8-sig`.
- Do not overwrite a corrupt file until a successful parse.
- Tests with BOM and malformed JSON.

### PF-09 — Settings schema

- Preserve unknown settings keys instead of rebuilding only four fields.
- Read the stored `version` for forward compatibility.
- No legacy-user migration is required; there are no existing users.
- Missing/invalid settings default `ofd_enabled` to true; an explicit saved false remains an opt-out.
- Tests: unknown key survives Save/mute; malformed settings JSON; missing/null/false OFD values.

### PF-10 — Permission hints and defensive host reads

- Recognize Windows `[WinError 5]` / “Access is denied” and macOS `errno 1`.
- Guard `preset.config_value("inherits")` with `getattr`/`try` so a host API change cannot crash the scan.
- Tests with a minimal preset object that lacks `config_value`.

### Gate 4

- Integration simulation passes.
- Cloned Orca E2E proves slash/quote names install and select.
- System-only folder scenario installs an OFD preset.
- Existing CC3D custom/local price copies still work.
- No local bundle file is left behind after a simulated reload failure.
- Settings migration and unknown-key preservation tests pass.

---

## 8. Stage 5 — UI and worker state

Owner: **Entry** (sequential after PF). Same file, no parallel edits.

### UI-01 — Ambiguous Apply

- When `row.choose` is true, do not preselect/first-candidate / do not set `dirty`.
- Exclude choose rows from `applyCount()` and `applyAll()`.
- Require an explicit chooser pick.
- Tests with a DOM/state harness and manual.

### UI-02 — Applied/dirty state

- Set `applied` by comparing `current_name`/`current` with the selected preset, normalizing `_local` names.
- Do not mark every scan row dirty.
- Tests.

### UI-03 — Staged price identity

- Key staged price by `(identity, selected preset)`.
- Clear when the spool/preset changes or a rescan detects a different filament.
- Tests.

### UI-04 / UI-05 — Status and failure state

- `choosePreset()` updates `source`, `preset_name`, `selected`, `dirty`.
- `statusFor()` uses `selected`, not stale `source`.
- On Apply failure, recompute dirty and clear `price_pending`.
- Tests.

### UI-06 / UI-07 — Missing controls

- Add Mute/Unmute controls for ignored identities.
- Add the Refresh presets button.
- Wire both to the existing server commands.
- Manual and state tests.

### UI-08 — Feed count

- Pass the printer’s actual slot count separately from loaded lanes.
- Suppress mismatch when total is unknown.
- Tests with 4-slot printer / 2 loaded.

### UI-09 — Connected gate

- Require only a host.
- Attempt the request; report the actual network error.
- Use connected/online only as hints.
- Tests with missing flags.

### UI-10 — Spoolman cache reset

- Clear `spoolman_cache` per scan (or TTL).
- Tests.

### UI-11 — Worker race and messages

- Capture the window/queue locally; clear `self.window` only if it is still current.
- Handle the “already open” message in the page.
- Guard `print(..., file=sys.stderr)` for GUI builds with no stderr.
- Escape `lib:` values in options.
- Tests/manual.

### UI-12 — OFD preflight

- Preflight the OFD origin once per scan before `library.find()`.
- Cache failure/empty result per scan.
- Tests with a fake permission denial.

### Gate 5

- JS syntax check passes.
- UI state tests pass.
- Manual panel smoke on a clone:
  - ambiguous match cannot Apply;
  - applied rows are not dirty;
  - refresh/mute/unmute work;
  - OFD first scan prompts once, then rescan works.

---

## 9. Stage 6 — Build, install, release plumbing, docs

Owner: **Build**. Starts in parallel from Stage 0.

### BR-01 — install.sh

- Detect OS:
  - macOS: `~/Library/Application Support/OrcaSlicer`
  - Linux: `$HOME/.config/OrcaSlicer`
  - explicit argument always wins.
- Refuse/clean stale direct `.py`/`.whl` files in `$DEST`; install into a temp directory and replace atomically.
- Check `python3` and print a clear prerequisite error.
- Preserve `.install_state.json` and history.
- Tests with temporary data dirs on macOS/Linux.

### BR-02 — Windows install docs

- Use `python plugin\build_single_file.py`, not file association.
- Add `New-Item -ItemType Directory -Force`.
- Add removal of the legacy `orca_plugins/filament_match.py` folder.
- Document the Python prerequisite or ship a prebuilt artifact.
- Test instructions in a Windows CI shell if available.

### BR-03 — Release workflow

- Add `contents: write` if attaching assets.
- Attach `filament_match_any.py` and a SHA-256 file to the GitHub release.
- Add `if: github.event.release.prerelease == false`.
- Validate the OIDC response before using it.
- Use `--form-string` (already fixed).
- Truncate changelogs by characters, not bytes.

### BR-04 — CI

- Matrix: Ubuntu, macOS, Windows.
- Run the full suite on all three.
- Import the Hub artifact with a fake `orca` module and assert `PLUGIN_VERSION`.
- Run tests in the publish job or require a successful test workflow.
- Add a dry-run build with `--version 9.9.9` and assert both metadata fields.

### BR-05 — Build assertions

- `build_single_file.py`:
  - fail if the PEP 723 header is missing/invalid;
  - detect annotated/nested/conditional duplicate definitions;
  - add a flat-vs-multi parity test.
- `build_hub_file.py`:
  - assert both version substitutions changed the text;
  - assert name/version/description metadata exist.

### BR-06 — Single version source

- Choose one canonical version source (e.g., `plugin/filament_match.py`).
- Generate/check docs and tests from it, or add a CI check that all version strings match.
- Update `README.md`, `plugin/HUB_PUBLISHING.md`, `plugin/HUB_DESCRIPTION.md`, `test_entry_static.py`.

### BR-07 — Hub description limit

- Enforce both character and byte checks in `orca_hub.py update`.
- Add a CI check for `plugin/HUB_DESCRIPTION.md` <= 10,000 characters and <= 10,000 UTF-8 bytes if the API is byte-limited.

### BR-08 — Credentials

- Do not commit `.env`.
- Recommend rotating the current admin PAT and using a fine-grained/scoped token or `gh auth`.
- Write the Orca session file with `os.open(..., 0o600)`/`mkstemp` rather than write-then-chmod.

### BR-09 — Publish quoting

- Quote `$PLUGIN_FILES` or use an array.
- Validate the OIDC response.
- Avoid splitting UTF-8 when truncating.

### BR-10 — Docs consistency

- Align minimum Orca version (`2.5.0` vs `2.5.2`).
- Remove stale “can be published later” text.
- Add Windows update steps.
- Document the actual OFD/Moonraker support scope.

### Gate 6

- macOS/Linux install tests pass.
- Windows CI unit tests pass.
- CI matrix green.
- Hub artifact imports and reports the requested version.
- Workflow dry-run produces the expected release asset list (no publish).
- No hardcoded version mismatch.

---

## 10. Stage 7 — Integration, E2E, and independent verification

Owner: **Verify** (read-only). Integration branch only.

### Tasks

- Merge Core → Sources → Entry → Build.
- Resolve conflicts; re-run the full suite after every merge.
- Run on macOS, Ubuntu, Windows.
- Build flat and Hub artifacts; import both with a fake `orca`.
- Run the cloned-Orca E2E matrix:
  - Snapmaker arrays;
  - 1-based maps;
  - Moonraker lane maps;
  - non-ASCII vendor;
  - PLA+ / generic variants;
  - ambiguous match;
  - OFD slash/quote name;
  - system-only folder;
  - price copy;
  - Spoolman `/api/v1`;
  - slot mismatch.
- Compare before/after golden outputs for the existing U1 scenarios.
- Re-run every audit repro; all must pass.
- Run the manual checklist in Section 12 on a clean clone.

### Gate 7

- Full suite green on 3 OSes.
- E2E matrix green.
- No regression in existing scenarios.
- Independent verifier signs off.
- No release tag exists.

---

## 11. Stage 8 — Release candidate (still no publish)

- Bump to `0.9.0` in one canonical place.
- Update changelog and Hub description.
- Build artifacts; record SHA-256.
- Dry-run the release workflow with a fake tag; do not publish.
- Prepare the final user checklist from Section 12 with the exact build hash.
- Wait for explicit approval.
- Only then: merge to `main`, tag `v0.9.0`, create the GitHub release, publish to Orca Cloud, update the Hub description.

---

## 12. Final manual verification (run after implementation)

Use a **cloned** Orca data directory. Do not test against your only live profile.

1. **Backup and clone**
   - Quit Orca.
   - Copy `~/Library/Application Support/OrcaSlicer` to a temp directory.
   - Remove `log`, `cache`, `user.lock`, and `OrcaSlicer.conf.lock`.
2. **Build and install into the clone**
   - Run `./plugin/install.sh "$CLONE"`.
   - Confirm the installed `filament_match.py` reports the test version.
   - Confirm there is no legacy `orca_plugins/filament_match.py` folder and no extra direct `.py` files.
3. **Start the clone and mock printer**
   - Start Orca with `--datadir "$CLONE"`.
   - Start `plugin/dev/mock_printer.py`.
   - Set the printer’s `print_host` to `127.0.0.1:7125`.
   - Run Filament Match.
4. **Existing-feature regression**
   - Scan four feeds.
   - Verify the same matches as the pre-fix golden run.
   - Apply all; restart Orca; verify history restores the same presets.
   - Verify slot-count mismatch messaging still works.
5. **OFD default**
   - Remove/rename `settings.json` to simulate a fresh profile.
   - Open Settings; confirm **Open Filament Database** is checked.
   - Scan a filament with no installed preset; confirm an OFD candidate.
   - Apply it; confirm the preset is installed and selected.
6. **No-network fallback**
   - Deny the printer/OFD permission prompt.
   - Confirm the panel offers/uses Orca's current slots without pretending to have raw AMS data.
   - Confirm a normal rescan with permission restores OFD matching.
7. **Permission prompt count**
   - Clear Orca's plugin-grant state for the test clone.
   - Scan once and count prompts: one per distinct origin, not one per endpoint/path/row.
   - Confirm a rescan does not re-prompt for the same origins.
8. **OFD name edge case**
   - Apply an OFD profile whose name contains `/` or `"` (e.g. `AzureFilm TPU/TPE (OFD)`).
   - Confirm the local file, Orca preset name, slot selection and history all agree.
   - Restart Orca and rescan; confirm history still resolves.
9. **System-only profile**
   - Use a clone with only system printer/filament presets.
   - Confirm an OFD install works without manually saving a user preset.
10. **Non-ASCII vendor**
   - Configure two mock spools/presets with different Cyrillic vendors and the same material/colour.
   - Confirm each matches its own vendor.
   - Confirm history for one never returns the other.
11. **Vendor mismatch**
   - Use an eSUN observation against an ACME-only preset.
   - Confirm no automatic wrong-brand selection; expect a chooser or no match.
12. **Material/generic cases**
    - `PLA+` spool against a `PLA` preset: expect a match or a clear subtype-aware candidate, not “no match”.
    - Plain PLA with only `Generic PLA-CF`: confirm it does not silently select PLA-CF.
    - `PLA Basic` scenario: confirm the chosen preset follows the documented policy.
13. **Ambiguous match**
    - Force multiple candidates.
    - Confirm Apply is disabled until you pick one; confirm the chooser selection is what gets applied and remembered.
14. **Prices**
    - Fetch a Spoolman price; press Save price; press Apply.
    - Verify the preset JSON carries the per-kg price.
    - Change the spool and rescan; confirm a new price is fetched, not the old staged value.
    - Enter a Spoolman URL ending in `/api/v1`; confirm fetch still works.
15. **History failure**
    - Corrupt `history.json` with invalid UTF-8.
    - Confirm the plugin warns and does not claim “remembered”.
    - Confirm a BOM-prefixed file loads.
16. **OFD cache**
    - Change the OFD index URL to a local fixture.
    - Confirm old cache does not leak into the new index.
    - Refresh/clear; confirm fresh presets are fetched.
17. **UI controls**
    - Verify Refresh presets, Mute and Unmute work.
    - Force an Apply failure; confirm the row is not left armed.
18. **Windows paths**
    - On Windows, install from source using the documented commands.
    - Verify the destination directory is created and the legacy folder is removed.
    - Verify a long/slash/quote preset name installs and selects.
19. **Regression sweep**
    - CC3D custom/local price copy.
    - Spoolman enrichment and `orca_preset` hint.
    - OFD candidate install.
    - Slot mismatch: 4 printer feeds / 2 project slots and vice versa.
    - Restart Orca; verify settings, history and local presets.
20. **Artifact checks**
    - Import the Hub artifact with a fake `orca`.
    - Confirm `PLUGIN_VERSION` and metadata header match the build.
    - Confirm SHA-256 matches the recorded build.
21. **Release gate**
    - Only after all above pass: approve merging to `main`, tagging, GitHub release, Orca Cloud publish, and Hub description update.

---

## 13. Coverage matrix (audit finding → task)

| Audit finding | Task |
| --- | --- |
| Non-ASCII normalization / wrong history | CM-01, HS-05 |
| Local name vs file stem | PF-01 |
| System-only folder inference | PF-02 |
| Fuzzy wrong vendor | CM-02 |
| Generic variant fallback | CM-03 |
| Material aliases | CM-04 |
| Ambiguous Apply | UI-01 |
| `filament_id` bypass | CM-05 |
| Exact colour ignored | CM-06 |
| History invalid UTF-8 / false remembered | HS-01, HS-02 |
| OFD cache stale / shared by URL | SA-01, SA-02 |
| Generic Moonraker unsupported | SA-03 |
| User price copy loses settings | PF-03 |
| Local parent is a user preset | PF-05 |
| 1-based / lane0 mappings | SA-04, SA-05 |
| Orca-native fallback / permission prompts | SA-12, SA-13, SA-14 |
| Authoritative printer endpoint/field schemas | SA-03, SA-04, SA-05; `plugin/dev/ORCA_PRINTER_DATA.md` |
| Spoolman `/api/v1` + IPv6 | SA-06 |
| Scan blocked by connected flag | UI-09 |
| Spoolman cache stale | UI-10 |
| Staged price survives swap | UI-03 |
| Every scan dirty | UI-02 |
| Feed count false mismatch | UI-08 |
| Settings schema/migration | PF-09 |
| Windows filename handling | PF-07 |
| No rollback on reload failure | PF-06 |
| UI stale source/apply failure | UI-04, UI-05 |
| Mute/unmute unreachable | UI-06 |
| Refresh button missing | UI-07 |
| Worker race | UI-11 |
| First OFD preflight | UI-12 |
| Library normalization | CM-09 |
| Duplicate preset names | CM-08 |
| History single identity / printer fallback | HS-03, HS-04 |
| Fuzzy margin below threshold | CM-10 |
| Prefix stripping over-broad | CM-11 |
| Colour parser bare words | CM-12 |
| Bool/name coercion | CM-13 |
| Tag sentinel identity | CM-15 |
| Spoolman extra pagination | SA-07 |
| Local OFD index Windows | SA-08 |
| OFD validation/thread safety | SA-09 |
| Proxy/redirect hardening | SA-11 |
| install.sh OS/stale modules/Python | BR-01 |
| Windows docs | BR-02 |
| Release asset/prerelease | BR-03 |
| CI matrix/hub import/version | BR-04 |
| Build assertions/duplicate checker | BR-05 |
| Version drift/docs | BR-06, BR-10 |
| Hub description limit | BR-07 |
| `.env` plaintext PAT | BR-08 |
| Publish quoting/UTF-8 | BR-09 |

---

## 14. Release gate

Do not merge to `main`, tag, publish, or update the Hub listing unless all are true:

- Every task above is complete.
- Full suite passes on macOS, Ubuntu and Windows.
- The cloned-Orca E2E matrix passes.
- Every audit repro passes.
- The existing U1/Spoolman/OFD/price-copy golden scenarios are unchanged or intentionally improved.
- You have completed the manual checklist.
- You explicitly approve the release.

Until then, the version stays at `0.8.1` and the release workflow is not triggered.
