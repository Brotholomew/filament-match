"""Static smoke tests for the Orca entry point, without a running OrcaSlicer.

The real plugin imports the embedded ``orca`` module. Here we inject a small
fake module so the entry file can be imported and its plugin/capability
registration checked. This does not test any host call.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ENTRY = PLUGIN_DIR / "filament_match.py"
DIST = PLUGIN_DIR / "dist" / "filament_match.py"


def _fake_orca():
    module = types.ModuleType("orca")
    registered = []

    class PythonPluginBase:
        pass

    class ScriptPluginCapabilityBase(PythonPluginBase):
        pass

    class PluginBase:
        pass

    class PluginResult:
        Success = "success"
        Skipped = "skipped"
        RecoverableError = "recoverable"
        FatalError = "fatal"

    class ExecutionResult:
        def __init__(self, status, message=""):
            self.status = status
            self.message = message

        @classmethod
        def success(cls, message=""):
            return cls(cls.Success if hasattr(cls, "Success") else "success", message)

        @classmethod
        def failure(cls, status, message=""):
            return cls(status, message)

    module.script = types.SimpleNamespace(ScriptPluginCapabilityBase=ScriptPluginCapabilityBase)
    module.base = PluginBase
    module.plugin = lambda cls: cls
    module.register_capability = lambda cls: registered.append(cls)
    module.PluginResult = PluginResult
    module.ExecutionResult = ExecutionResult
    module.host = types.SimpleNamespace()
    module.PythonPluginBase = PythonPluginBase
    module._registered = registered
    return module


class EntrySmokeTests(unittest.TestCase):
    def test_metadata_header(self):
        text = ENTRY.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# /// script"))
        self.assertIn('[tool.orcaslicer.plugin]', text)
        self.assertIn('name = "Filament Match"', text)
        self.assertIn('version = "0.8.1"', text)

    def test_open_filament_database_is_enabled_by_default(self):
        fake = _fake_orca()
        sys.modules["orca"] = fake
        try:
            spec = importlib.util.spec_from_file_location(
                "filament_match_defaults", ENTRY)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertTrue(module._default_settings()["ofd_enabled"])
            self.assertTrue(module._normalize_settings({})["ofd_enabled"])
            self.assertTrue(module._normalize_settings(
                {"ofd_enabled": None})["ofd_enabled"])
            self.assertFalse(module._normalize_settings(
                {"ofd_enabled": False})["ofd_enabled"])
        finally:
            sys.modules.pop("orca", None)

    def test_entry_imports_and_registers(self):
        fake = _fake_orca()
        sys.modules["orca"] = fake
        try:
            name = "filament_match_smoke"
            spec = importlib.util.spec_from_file_location(name, ENTRY)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertTrue(hasattr(module, "FilamentMatch"))
            self.assertTrue(hasattr(module, "FilamentMatchPlugin"))
            plugin = module.FilamentMatchPlugin()
            plugin.register_capabilities()
            self.assertIn(module.FilamentMatch, fake._registered)
            self.assertEqual(module.FilamentMatch().get_name(), "Filament Match")
        finally:
            sys.modules.pop("orca", None)

    def _entry_module(self):
        fake = _fake_orca()
        sys.modules["orca"] = fake
        spec = importlib.util.spec_from_file_location(
            "filament_match_ui_helpers", ENTRY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.addCleanup(sys.modules.pop, "orca", None)
        return module

    def _run_node(self, source, check=False):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                    "w", suffix=".js", delete=False, encoding="utf-8") as stream:
                stream.write(source)
                path = stream.name
            command = [node, "--check", path] if check else [node, path]
            return subprocess.run(
                command, capture_output=True, text=True, timeout=30)
        finally:
            if path:
                Path(path).unlink(missing_ok=True)

    def test_page_javascript_parses(self):
        module = self._entry_module()
        start = module.PAGE.index("<script>") + len("<script>")
        end = module.PAGE.index("</script>", start)
        result = self._run_node(module.PAGE[start:end], check=True)
        self.assertEqual(
            result.returncode, 0,
            "JavaScript syntax error:\n%s\n%s" % (result.stdout, result.stderr))

    def test_ui_state_helpers(self):
        module = self._entry_module()
        start = module.PAGE.index("// #region FM_STATE_HELPERS")
        end = module.PAGE.index("// #endregion FM_STATE_HELPERS", start)
        helpers = module.PAGE[start:end]
        assertions = r"""
const assert = require('assert');
const ambiguous = {choose: true, selected: '', dirty: true, slot_exists: true,
  candidates: [{preset_name: 'A'}, {preset_name: 'B'}]};
assert.strictEqual(fmSelectedValue(ambiguous), '');
assert.strictEqual(fmRowApplyable(ambiguous), false);
assert.strictEqual(fmApplyCount([ambiguous]), 0);
assert.strictEqual(fmStatusFor(ambiguous).kind, 'warn');
assert.ok(fmStatusFor(ambiguous).text.includes('pick'));
const library = {preset_name: '', source: 'library',
  candidates: [{preset_name: 'CC3D PETG (OFD)', source: 'library'}]};
assert.strictEqual(fmSelectedValue(library), 'lib:CC3D PETG (OFD)');
assert.ok(fmRowApplyable({...library, selected: 'lib:CC3D PETG (OFD)',
  preset_name: 'CC3D PETG (OFD)', source: 'library', dirty: true}));
assert.strictEqual(fmIsApplied({current_name: '_local/filament_match/X',
  preset_name: '_local/filament_match/X'}), true);
assert.strictEqual(fmIsApplied({current: 'eSUN PETG',
  preset_name: 'eSUN PETG @U1'}), true);
assert.strictEqual(fmIsApplied({current: 'Other', preset_name: 'eSUN PETG'}), false);
assert.strictEqual(fmRowDirty({selected: 'X', preset_name: 'X',
  current_name: 'X'}), false);
assert.strictEqual(fmRowDirty({selected: 'X', preset_name: 'X',
  current_name: 'Y'}), true);
assert.strictEqual(fmStatusFor({selected: 'X', preset_name: 'X',
  current_name: 'X'}).text, 'Already in slot');
assert.strictEqual(fmStatusFor({selected: 'X', preset_name: 'X',
  current_name: 'Y', source: 'installed', dirty: true}).kind, 'warn');
assert.strictEqual(fmStatusFor({selected: 'lib:X', preset_name: 'X',
  source: 'library', dirty: true}).kind, 'warn');
assert.notStrictEqual(
  fmPriceStageKey({identity: 'spool:1', selected: 'A'}),
  fmPriceStageKey({identity: 'spool:1', selected: 'B'}));
assert.notStrictEqual(
  fmPriceStageKey({identity: 'spool:1', selected: 'A'}),
  fmPriceStageKey({identity: 'spool:2', selected: 'A'}));
const failed = {selected: 'X', preset_name: 'X', current_name: 'Y',
  price_pending: true, pending: true};
fmApplyErrorState(failed);
assert.strictEqual(failed.price_pending, false);
assert.strictEqual(failed.pending, false);
assert.strictEqual(failed.dirty, true);
console.log('ok');
"""
        result = self._run_node(helpers + "\n" + assertions)
        self.assertEqual(
            result.returncode, 0,
            "UI helper test failed:\n%s\n%s" % (result.stdout, result.stderr))

    def test_missing_ui_actions_are_wired(self):
        module = self._entry_module()
        page = module.PAGE
        self.assertIn('id="refreshPresets"', page)
        self.assertIn("post('refresh')", page)
        self.assertIn("row.ignored ? 'unmute' : 'mute'", page)
        self.assertIn("row.ignored ? 'Unmute' : 'Mute'", page)
        self.assertIn("post(mute ? 'mute' : 'unmute'", page)
        self.assertIn("feeders_known: state.feedersKnown", page)
        self.assertIn("fmPriceStageKey", page)
        self.assertIn("fmApplyErrorState", page)
        self.assertIn("const value = 'lib:' + cand.preset_name;", page)
        self.assertIn("esc(value)", page)
        self.assertNotIn("first.checked = true", page)
        self.assertIn("already open", page)

    def test_flat_build_has_no_duplicate_globals(self):
        script = PLUGIN_DIR / "build_single_file.py"
        spec = importlib.util.spec_from_file_location("fm_build", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.check_duplicate_names()

    @unittest.skipUnless(DIST.exists(), "single-file build not present")
    def test_single_file_build_imports(self):
        fake = _fake_orca()
        sys.modules["orca"] = fake
        try:
            spec = importlib.util.spec_from_file_location("filament_match_dist_smoke", DIST)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertTrue(hasattr(module, "FilamentMatchPlugin"))
            self.assertTrue(module._DEPS, "flattened build has no dependencies")
        finally:
            sys.modules.pop("orca", None)


if __name__ == "__main__":
    unittest.main()
