#!/usr/bin/env python3
"""Independent checks for the audit fixes.

Run from the repository root after the workstreams are merged:

    python3 plugin/dev/audit_verify.py

This is deliberately not part of unittest discovery. It exercises the exact
repros from the audit and prints PASS/FAIL so the integration branch can be
checked independently of the workers' own tests.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugin"))
sys.path.insert(0, str(ROOT / "plugin" / "tests"))

import filament_match_core as core  # noqa: E402
import filament_match_sources as sources  # noqa: E402
from test_core import make_obs, make_preset  # noqa: E402

RESULTS = []


def check(name):
    def decorate(fn):
        try:
            fn()
        except Exception as error:  # noqa: BLE001 - report the repro, do not stop
            RESULTS.append((name, False, "%s: %s" % (type(error).__name__, error)))
        else:
            RESULTS.append((name, True, ""))
        return fn
    return decorate


def _history(obs, preset_name):
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "history.json")
        store = core.HistoryStore(path)
        store.remember(obs, "Audit printer", preset_name)
        return store


@check("non-ASCII vendor normalization is not empty and distinguishes vendors")
def _():
    first = core.normalize_vendor("Полимер")
    second = core.normalize_vendor("Другой")
    assert first, "Cyrillic vendor normalized to empty"
    assert second, "second Cyrillic vendor normalized to empty"
    assert first != second, "different Cyrillic vendors collapsed together"


@check("history does not cross vendors")
def _():
    first = make_preset("Полимер PETG", vendor="Полимер", material="PETG")
    second = make_preset("Другой PETG", vendor="Другой", material="PETG")
    obs_first = make_obs(vendor="Полимер", material="PETG", product="Pink", color="#ff0000")
    obs_second = make_obs(vendor="Другой", material="PETG", product="Pink", color="#ff0000")
    store = _history(obs_first, first.name)
    result = core.match_observation(obs_second, [first, second], history=store,
                                    printer="Audit printer")
    assert result.preset_name != first.name, "history leaked to the wrong vendor"


@check("fuzzy matching does not auto-select a different vendor")
def _():
    preset = make_preset("ACME PETG Matte", vendor="ACME", material="PETG",
                         subtype="Matte", product="Matte", color="#ff0000")
    obs = make_obs(vendor="eSUN", material="PETG", subtype="Matte",
                   product="Matte", color="#ff0000")
    result = core.match_observation(obs, [preset])
    assert result.preset_name != preset.name, "wrong vendor was auto-selected"


@check("plain PLA does not fall back to Generic PLA-CF")
def _():
    preset = make_preset("Generic PLA-CF @System", base="Generic PLA-CF",
                         vendor="Generic", material="PLA")
    obs = make_obs(material="PLA", product="Pink", color="#ff0000")
    result = core.match_observation(obs, [preset])
    assert result.preset_name != preset.name, "PLA-CF selected for plain PLA"


@check("PLA+ matches a PLA family preset")
def _():
    preset = make_preset("eSUN PLA @System", vendor="eSUN", material="PLA",
                         color="#00ff00")
    obs = make_obs(vendor="eSUN", material="PLA+", product="Green",
                   color="#00ff00")
    result = core.match_observation(obs, [preset])
    assert result.preset_name == preset.name, "PLA+ did not match PLA"


@check("filament_id does not bypass vendor/material/colour checks")
def _():
    preset = make_preset("ACME PETG @U1", vendor="ACME", material="PETG",
                         filament_id="FID", color="#00ff00")
    obs = make_obs(vendor="eSUN", material="PETG", filament_id="FID",
                   color="#ff0000")
    result = core.match_observation(obs, [preset])
    assert result.preset_name != preset.name, "filament_id bypassed metadata"


@check("exact metadata does not select a wrong-colour preset")
def _():
    preset = make_preset("CC3D PETG @U1", vendor="CC3D", material="PETG",
                         color="#00ff00")
    obs = make_obs(vendor="CC3D", material="PETG", product="Pink",
                   color="#ff0000")
    result = core.match_observation(obs, [preset])
    assert result.preset_name != preset.name, "wrong-colour preset selected"


@check("invalid UTF-8 history is handled without raising")
def _():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "history.json")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe\x00")
        store = core.HistoryStore(path)
        assert store.load_error, "load_error was not recorded"


@check("arbitrary leading parentheses are not provenance markers")
def _():
    value = core.strip_bundle_prefix("(Matte) eSUN PETG")
    assert "Matte" in value, "a real name was stripped as provenance"


@check("Snapmaker 1-based maps do not duplicate the first slot")
def _():
    assert sources._at({"1": "A", "2": "B"}, 1) == "B", "slot 1 read key 1 again"


@check("Moonraker numeric lane values are skipped like Orca")
def _():
    observations = sources._parse_moonraker({
        "result": {"value": {"lane1": {"lane": 0, "material": "PLA"}}}
    })
    assert not observations, "numeric lane was accepted despite native behavior"


@check("Spoolman URL with /api/v1 is normalized")
def _():
    value = sources.normalize_base_url("http://spoolman:7912/api/v1")
    assert not value.endswith("/api/v1"), value


@check("bare IPv6 hosts are bracketed")
def _():
    value = sources.normalize_base_url("fd00::1")
    assert "[" in value and "]" in value, value


@check("Windows-unsafe preset names are canonicalized")
def _():
    assert sources.__name__  # keep import used
    from importlib.util import module_from_spec, spec_from_file_location
    import types
    fake = types.ModuleType("orca")
    fake.script = types.SimpleNamespace(ScriptPluginCapabilityBase=object)
    fake.base = object
    fake.plugin = lambda cls: cls
    fake.register_capability = lambda cls: None
    fake.host = types.SimpleNamespace()
    sys.modules["orca"] = fake
    try:
        spec = spec_from_file_location("fm_audit_entry", ROOT / "plugin" / "filament_match.py")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module._safe_filename("CON.json") != "CON.json"
        assert "/" not in module._safe_filename("AzureFilm TPU/TPE (OFD)")
    finally:
        sys.modules.pop("orca", None)


def main():
    failed = 0
    for name, ok, detail in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
        if detail:
            print("     " + detail)
        failed += 0 if ok else 1
    print("\n%d/%d checks passed" % (len(RESULTS) - failed, len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
