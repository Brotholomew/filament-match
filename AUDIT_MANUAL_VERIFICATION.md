# Audit fix — manual verification checklist

Use this on branch `fix/audit-2026-11-22`. Nothing here creates a release.

## 0. Automated gate

Run from the repository root:

```sh
python3 plugin/dev/audit_verify.py
python3 -m unittest discover -s plugin/tests -p 'test_*.py' -v
python3 plugin/check_version.py
python3 plugin/check_hub_description.py plugin/HUB_DESCRIPTION.md
```

Expected:

- `14/14 checks passed`
- `Ran 440 tests ... OK`
- `version 0.8.1 is consistent ...`
- Hub description under both limits

## 1. Safe cloned-Orca setup

Do not test against your only live profile.

```sh
cd /Users/brotholomew/workdir/filament-match
CLONE="$(mktemp -d "${TMPDIR:-/tmp}/fm-audit.XXXXXX")"
cp -R "$HOME/Library/Application Support/OrcaSlicer/." "$CLONE/"
rm -rf "$CLONE/log" "$CLONE/cache" "$CLONE/user_backup-v2.5.2"
rm -f "$CLONE/user.lock" "$CLONE/OrcaSlicer.conf.lock"
./plugin/install.sh "$CLONE"
```

Start the mock printer in another terminal:

```sh
cd /Users/brotholomew/workdir/filament-match
python3 plugin/dev/mock_printer.py --port 7125
```

Start the cloned Orca:

```sh
open -a OrcaSlicer --args --datadir "$CLONE"
```

Select a printer preset whose `print_host` is `127.0.0.1` and set
`printhost_port` to `7125`. Then run Filament Match from
**File → Plugins → Filament Match → Run plugin**.

## 2. Existing features must still work

- [ ] Panel opens and scans automatically.
- [ ] Four mock feeds appear with the expected vendors/materials/colours.
- [ ] Scan selects candidates but writes nothing.
- [ ] **Apply** writes the selections and remembers the mappings.
- [ ] Restart the clone; the same mappings return from history.
- [ ] The slot-count mismatch warning still appears when the project has a
      different number of slots.
- [ ] The CC3D custom/local preset still resolves and applies.

Expected: no “can not find parent”, no duplicate plugin load, no missing
local bundle.

## 3. OFD default and install

- [ ] In a fresh clone/settings file, **Open Filament Database** is checked.
- [ ] A feed with no good installed preset shows an OFD/library candidate.
- [ ] Press **Apply** on the library row.
- [ ] Orca logs `load preset: _local/filament_match/...` and reports no parent
      error.
- [ ] The preset appears in the slot selector with the same label the plugin
      reported.
- [ ] Restart the clone; the installed OFD preset and history still resolve.

## 4. Slash/quote local names

- [ ] Apply an OFD profile whose name contains `/` or `"`, for example
      `AzureFilm TPU/TPE (OFD)`.
- [ ] Confirm the file stem, JSON `name`, `filament_settings_id`, slot
      selection and history all use the canonical sanitized form.
- [ ] Restart and rescan; history still resolves.

Expected canonical example:

```text
file:  AzureFilm TPU_TPE (OFD).json
name:  _local/filament_match/AzureFilm TPU_TPE (OFD)
label: AzureFilm TPU_TPE (OFD)
```

## 5. System-only profile

- [ ] Use a clone with only system presets (no readable user preset).
- [ ] Apply an OFD candidate.
- [ ] Confirm the plugin discovers the existing `user/default` or UUID folder
      and writes the local preset there.

Expected: no “Could not determine the active Orca user profile folder”.

## 6. Matching regressions

The automated harness already checks these, but verify in the panel if you want:

- [ ] Two Cyrillic vendor presets with the same material/colour do not share
      history.
- [ ] An eSUN feed does not auto-select an ACME-only preset.
- [ ] `PLA+` matches a `PLA` family preset.
- [ ] Plain PLA does not silently select `Generic PLA-CF`.
- [ ] A wrong-colour exact preset is not auto-applied.
- [ ] A conflicting `filament_id` is not applied at 100.

## 7. Ambiguous matches

- [ ] Force two viable candidates.
- [ ] Confirm **Apply** is disabled until you pick one in the chooser.
- [ ] Confirm the chosen candidate, not candidate 1, is applied and remembered.
- [ ] Close and reopen the chooser; no candidate is preselected.

## 8. Prices and Spoolman

- [ ] Fetch a Spoolman price, press **Save price**, then **Apply**.
- [ ] Confirm the preset JSON contains the per-kg price.
- [ ] Apply a price to a user preset such as `CC3D PETG (custom)`.
- [ ] Confirm the local copy inherits the **system** parent (for example
      `Generic PETG @U1 0.4 nozzle`), preserves custom temperatures and applies
      the price.
- [ ] Change the spool and rescan; confirm the old staged price is not reused.
- [ ] Enter a Spoolman URL with `/api/v1` and with a bare host; Fetch still
      works.

Expected CC3D copy:

```json
"inherits": "Generic PETG @U1 0.4 nozzle",
"filament_cost": ["10.07"],
"nozzle_temperature": "240,240,240,240,240,240"
```

## 9. History corruption

- [ ] Replace the clone’s `history.json` with invalid UTF-8.
- [ ] Rescan/apply.
- [ ] Confirm the plugin warns, does not overwrite the corrupt file, and does
      not claim the mapping was remembered.
- [ ] Replace it with a BOM-prefixed valid file; confirm it loads.

## 10. OFD cache

- [ ] Change the OFD index URL to a local fixture.
- [ ] Confirm the old index cache is not used.
- [ ] Press **Clear OFD cache**; confirm legacy and URL-scoped index/preset
      files are removed.
- [ ] Rescan; confirm fresh data is fetched.

## 11. UI controls

- [ ] **Refresh presets** reloads the preset list.
- [ ] Mute/unmute works on a feed.
- [ ] An Apply failure leaves the row unapplied, not armed for retry.
- [ ] No stale “Generic fallback” or “Library match” status after changing the
      selected preset.

## 12. Cross-platform

CI already passes on Ubuntu, macOS and Windows. On a Windows machine, verify:

```powershell
python plugin\build_single_file.py
$dest = "$env:APPDATA\OrcaSlicer\orca_plugins\filament_match"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item plugin\dist\filament_match.py "$dest\filament_match.py"
'{"enabled": true}' | Set-Content "$dest\.install_state.json"
```

Confirm no legacy `filament_match.py` folder remains and a slash/quote preset
still installs and selects.

## 13. Release gate

Do not merge to `main`, tag, create a GitHub release, publish to Orca Cloud, or
update the Hub listing until:

- [ ] All automated checks pass.
- [ ] All sections above pass on a clone.
- [ ] The existing U1/Spoolman/OFD/price-copy behavior is unchanged.
- [ ] You explicitly approve the release.

Proposed next version after sign-off: `0.9.0`.
