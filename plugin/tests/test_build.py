"""Build-pipeline tests: metadata, duplicate detection, parity and artifacts.

These tests do not need a running OrcaSlicer. The flat and Hub artifacts are
rendered/imported with a fake ``orca`` module, the same technique used by the
entry smoke test.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load("fm_build_single_file", PLUGIN_DIR / "build_single_file.py")
hub = _load("fm_build_hub_file", PLUGIN_DIR / "build_hub_file.py")
version_check = _load("fm_check_version", PLUGIN_DIR / "check_version.py")
artifact_check = _load("fm_check_artifact", PLUGIN_DIR / "check_artifact.py")
description_check = _load(
    "fm_check_hub_description", PLUGIN_DIR / "check_hub_description.py")

ENTRY = PLUGIN_DIR / "filament_match.py"


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
            return cls("success", message)

        @classmethod
        def failure(cls, status, message=""):
            return cls(status, message)

    module.script = types.SimpleNamespace(
        ScriptPluginCapabilityBase=ScriptPluginCapabilityBase)
    module.base = PluginBase
    module.plugin = lambda cls: cls
    module.register_capability = lambda cls: registered.append(cls)
    module.PluginResult = PluginResult
    module.ExecutionResult = ExecutionResult
    module.host = types.SimpleNamespace()
    module.PythonPluginBase = PythonPluginBase
    module._registered = registered
    return module


class MetadataTests(unittest.TestCase):
    def test_entry_metadata_and_constant_agree(self):
        text = ENTRY.read_text(encoding="utf-8")
        _header, _body, metadata = build.parse_metadata(text)
        constants = re.findall(r'^PLUGIN_VERSION = "(.*)"$', text, re.MULTILINE)
        self.assertEqual(constants, [metadata["version"]])

    def test_missing_header_is_rejected(self):
        with self.assertRaises(SystemExit):
            build.parse_metadata("PLUGIN_VERSION = \"1.0.0\"\n")

    def test_unterminated_header_is_rejected(self):
        with self.assertRaises(SystemExit):
            build.parse_metadata('# /// script\n# dependencies = []\n')

    def test_invalid_header_toml_is_rejected(self):
        text = (
            "# /// script\n"
            "# dependencies = [\n"
            "# ///\n"
            "PLUGIN_VERSION = \"1.0.0\"\n"
        )
        with self.assertRaises(SystemExit):
            build.parse_metadata(text)

    def test_missing_metadata_field_is_rejected(self):
        text = (
            "# /// script\n"
            "# requires-python = \">=3.12\"\n"
            "# dependencies = []\n"
            "# [tool.orcaslicer.plugin]\n"
            "# id = \"filament-match\"\n"
            "# name = \"Filament Match\"\n"
            "# description = \"x\"\n"
            "# author = \"x\"\n"
            "# ///\n"
        )
        with self.assertRaises(SystemExit):
            build.parse_metadata(text)

    def test_header_and_plugin_version_must_agree(self):
        text = ENTRY.read_text(encoding="utf-8")
        mismatched, count = re.subn(
            r'^PLUGIN_VERSION = ".*"$', 'PLUGIN_VERSION = "9.9.9"',
            text, count=1, flags=re.MULTILINE)
        self.assertEqual(count, 1)
        with self.assertRaises(SystemExit):
            build._check_output(mismatched)

    def test_canonical_version_rejects_mismatched_constant(self):
        text = ENTRY.read_text(encoding="utf-8")
        mismatched, count = re.subn(
            r'^PLUGIN_VERSION = ".*"$', 'PLUGIN_VERSION = "9.9.9"',
            text, count=1, flags=re.MULTILINE)
        self.assertEqual(count, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "filament_match.py"
            path.write_text(mismatched, encoding="utf-8")
            with self.assertRaises(ValueError):
                version_check.canonical_version(path)


class DuplicateDetectionTests(unittest.TestCase):
    def test_annotated_and_conditional_bindings_are_found(self):
        source = (
            "x: int = 1\n"
            "if True:\n"
            "    def conditional():\n"
            "        pass\n"
            "try:\n"
            "    class Fallback:\n"
            "        pass\n"
            "except ImportError:\n"
            "    Fallback = None\n"
            "for item in ():\n"
            "    nested = 1\n"
        )
        names = {name for name, _line, _guarded in build.module_bindings(source)}
        self.assertTrue({"x", "conditional", "Fallback", "nested"} <= names)

    def test_annotated_duplicate_is_rejected(self):
        duplicates = build.find_duplicate_bindings([
            ("a.py", "shared: int = 1\n"),
            ("b.py", "if True:\n    def shared():\n        pass\n"),
        ])
        self.assertEqual(len(duplicates), 1)
        self.assertIn("shared", duplicates[0])

    def test_guarded_fallback_is_allowed(self):
        duplicates = build.find_duplicate_bindings([
            ("core.py", "def normalize_color(value):\n    return value\n"),
            ("sources.py",
             'if "normalize_color" not in globals():\n'
             "    def normalize_color(value):\n"
             "        return value\n"),
        ])
        self.assertEqual(duplicates, [])

    def test_all_export_lists_are_allowed(self):
        duplicates = build.find_duplicate_bindings([
            ("a.py", "__all__ = [\"one\"]\n"),
            ("b.py", "if \"__all__\" in globals():\n    __all__ = []\n"),
        ])
        self.assertEqual(duplicates, [])

    def test_real_sources_have_no_unexpected_duplicates(self):
        build.check_duplicate_names()


class FlatBuildTests(unittest.TestCase):
    def test_flat_build_keeps_every_source_binding(self):
        output = build.render()
        source_names = set()
        for filename in build.FILES:
            source = (PLUGIN_DIR / filename).read_text(encoding="utf-8")
            source_names.update(
                name for name, _line, _guarded in build.module_bindings(source, filename))
        flat_names = {name for name, _line, _guarded in build.module_bindings(output)}
        self.assertEqual(source_names - flat_names, set())
        self.assertEqual(
            re.findall(r'^PLUGIN_VERSION = "(.*)"$', output, re.MULTILINE),
            [version_check.canonical_version()])

    def test_flat_build_imports_with_fake_orca(self):
        output = build.render()
        fake = _fake_orca()
        sys.modules["orca"] = fake
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "filament_match.py"
                path.write_text(output, encoding="utf-8")
                spec = importlib.util.spec_from_file_location("fm_flat_test", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(hasattr(module, "FilamentMatchPlugin"))
                self.assertTrue(module._DEPS)
                self.assertEqual(
                    module.PLUGIN_VERSION, version_check.canonical_version())
        finally:
            sys.modules.pop("orca", None)


class HubBuildTests(unittest.TestCase):
    def test_hub_build_overrides_both_version_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = hub.build("9.9.9", Path(directory) / "filament_match_any.py")
            text = path.read_text(encoding="utf-8")
            self.assertIn('# version = "9.9.9"', text)
            self.assertIn('PLUGIN_VERSION = "9.9.9"', text)
            metadata = hub._metadata(text)
            self.assertEqual(metadata["version"], "9.9.9")
            self.assertEqual(metadata["name"], "Filament Match")
            self.assertEqual(metadata["id"], "filament-match")
            self.assertEqual(metadata["author"], hub.AUTHOR)
            self.assertEqual(metadata["description"], hub.DESCRIPTION)
            self.assertNotIn("Generated by plugin/build_single_file.py", text)

            fake = _fake_orca()
            sys.modules["orca"] = fake
            try:
                spec = importlib.util.spec_from_file_location(
                    "fm_hub_version_test", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertEqual(module.PLUGIN_VERSION, "9.9.9")
            finally:
                sys.modules.pop("orca", None)

    def test_hub_build_defaults_to_canonical_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = hub.build(out=Path(directory) / "filament_match_any.py")
            text = path.read_text(encoding="utf-8")
            canonical = version_check.canonical_version()
            self.assertEqual(hub._metadata(text)["version"], canonical)
            self.assertEqual(
                re.findall(r'^PLUGIN_VERSION = "(.*)"$', text, re.MULTILINE),
                [canonical])

    def test_invalid_version_override_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                hub.build("not-a-version",
                          Path(directory) / "filament_match_any.py")

    def test_artifact_checker_validates_version_and_syntax(self):
        with tempfile.TemporaryDirectory() as directory:
            path = hub.build("9.9.9", Path(directory) / "filament_match_any.py")
            self.assertEqual(artifact_check.check(path, "9.9.9"), "9.9.9")
            with self.assertRaises(ValueError):
                artifact_check.check(path, "9.9.8")


class ConsistencyTests(unittest.TestCase):
    def test_release_versions_are_consistent(self):
        self.assertEqual(version_check.main(), 0)

    def test_hub_description_fits_both_limits(self):
        characters, byte_count = description_check.check()
        self.assertLessEqual(characters, description_check.MAX_CHARS)
        self.assertLessEqual(byte_count, description_check.MAX_BYTES)

    def test_hub_description_checker_rejects_character_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "description.md"
            path.write_text("a" * 10_001, encoding="utf-8")
            with self.assertRaises(ValueError):
                description_check.check(path)

    def test_hub_description_checker_rejects_byte_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "description.md"
            path.write_text("é" * 5_001, encoding="utf-8")
            with self.assertRaises(ValueError):
                description_check.check(path)


if __name__ == "__main__":
    unittest.main()
