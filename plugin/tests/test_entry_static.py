"""Static smoke tests for the Orca entry point, without a running OrcaSlicer.

The real plugin imports the embedded ``orca`` module. Here we inject a small
fake module so the entry file can be imported and its plugin/capability
registration checked. This does not test any host call.
"""
from __future__ import annotations

import importlib.util
import sys
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
        self.assertIn('version = "0.8.0"', text)

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
