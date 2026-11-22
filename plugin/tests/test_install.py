"""install.sh tests on temporary Orca data directories.

The installer is POSIX shell; Windows uses the documented PowerShell steps and
is covered by the normal Python test suite on the Windows CI matrix.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
INSTALL = PLUGIN_DIR / "install.sh"
POSIX = os.name == "posix" and shutil.which("bash") is not None


def run_install(*args: str, env=None):
    command = ["bash", str(INSTALL), *args]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@unittest.skipUnless(POSIX, "install.sh requires a POSIX shell")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = dict(os.environ)
        self.env["PYTHON"] = sys.executable

    def tearDown(self):
        self.temp.cleanup()

    def _data_dir(self, name="OrcaSlicer"):
        path = self.root / name
        path.mkdir(parents=True)
        return path

    def _plugins_dir(self, data):
        return data / "orca_plugins"

    def test_explicit_data_dir_replaces_cleanly_and_preserves_state(self):
        data = self._data_dir("Orca Data With Spaces")
        plugins = self._plugins_dir(data)
        dest = plugins / "filament_match"
        dest.mkdir(parents=True)
        (dest / "stale.py").write_text("old entry\n", encoding="utf-8")
        (dest / "stale.whl").write_text("old wheel\n", encoding="utf-8")
        (dest / "__pycache__").mkdir()
        (dest / "__pycache__" / "stale.pyc").write_text("old", encoding="utf-8")
        state = '{"enabled": false, "note": "keep me"}\n'
        (dest / ".install_state.json").write_text(state, encoding="utf-8")
        (dest / "history.json").write_text('{"version": 1}\n', encoding="utf-8")

        history_dir = plugins / "plugin_data" / "filament_match"
        history_dir.mkdir(parents=True)
        history = history_dir / "history.json"
        history.write_text('{"version": 2, "spools": {}}\n', encoding="utf-8")

        legacy = plugins / "filament_match.py"
        legacy.mkdir()
        (legacy / "old.py").write_text("old\n", encoding="utf-8")

        result = run_install(str(data), env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((dest / "filament_match.py").is_file())
        self.assertEqual(
            (dest / "filament_match.py").read_bytes(),
            (PLUGIN_DIR / "dist" / "filament_match.py").read_bytes(),
        )
        self.assertEqual(
            (dest / ".install_state.json").read_text(encoding="utf-8"), state)
        self.assertEqual(
            (dest / "history.json").read_text(encoding="utf-8"),
            '{"version": 1}\n',
        )
        self.assertFalse(legacy.exists())
        self.assertFalse((dest / "stale.py").exists())
        self.assertFalse((dest / "stale.whl").exists())
        self.assertFalse((dest / "__pycache__").exists())
        self.assertEqual(
            sorted(path.name for path in dest.iterdir() if path.suffix == ".py"),
            ["filament_match.py"],
        )
        self.assertEqual(
            history.read_text(encoding="utf-8"),
            '{"version": 2, "spools": {}}\n',
        )
        leftovers = [
            path.name for path in plugins.iterdir()
            if path.name.startswith(".filament_match.")
        ]
        self.assertEqual(leftovers, [])

    def test_new_install_is_enabled_and_has_no_legacy_path(self):
        data = self._data_dir()
        plugins = self._plugins_dir(data)
        plugins.mkdir()
        legacy_file = plugins / "filament_match.py"
        legacy_file.write_text("legacy\n", encoding="utf-8")

        result = run_install(str(data), env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = plugins / "filament_match"
        self.assertTrue((dest / "filament_match.py").is_file())
        self.assertEqual(
            json.loads((dest / ".install_state.json").read_text(encoding="utf-8")),
            {"enabled": True},
        )
        self.assertFalse(legacy_file.exists())

    def test_missing_python_has_a_clear_prerequisite_error(self):
        data = self._data_dir()
        env = dict(self.env)
        env["PYTHON"] = str(self.root / "missing-python3")
        result = run_install(str(data), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.12", result.stderr)
        self.assertFalse((self._plugins_dir(data) / "filament_match").exists())

    def test_failed_build_leaves_existing_install_untouched(self):
        data = self._data_dir()
        dest = self._plugins_dir(data) / "filament_match"
        dest.mkdir(parents=True)
        marker = dest / "filament_match.py"
        marker.write_text("previous build\n", encoding="utf-8")

        fake = self.root / "fake-python"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ]; then exit 0; fi\n'
            "echo simulated build failure >&2\n"
            "exit 42\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env = dict(self.env)
        env["PYTHON"] = str(fake)

        result = run_install(str(data), env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("simulated build failure", result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "previous build\n")
        self.assertFalse((dest / "stale.py").exists())

    def test_no_argument_uses_os_default_data_dir(self):
        if sys.platform == "darwin":
            relative = Path("Library") / "Application Support" / "OrcaSlicer"
        elif sys.platform.startswith("linux"):
            relative = Path(".config") / "OrcaSlicer"
        else:
            self.skipTest("no documented default for this OS")
        home = self.root / "home"
        data = home / relative
        data.mkdir(parents=True)
        env = dict(self.env)
        env["HOME"] = str(home)
        result = run_install(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (data / "orca_plugins" / "filament_match" / "filament_match.py").is_file())

    def _fake_uname(self, value):
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        uname = fake_bin / "uname"
        uname.write_text("#!/bin/sh\nprintf '%%s\\n' '%s'\n" % value, encoding="utf-8")
        uname.chmod(0o755)
        env = dict(self.env)
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        return env

    def test_fake_linux_uses_linux_default_data_dir(self):
        home = self.root / "linux-home"
        data = home / ".config" / "OrcaSlicer"
        data.mkdir(parents=True)
        env = self._fake_uname("Linux")
        env["HOME"] = str(home)
        result = run_install(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (data / "orca_plugins" / "filament_match" / "filament_match.py").is_file())

    def test_fake_macos_uses_macos_default_data_dir(self):
        home = self.root / "mac-home"
        data = home / "Library" / "Application Support" / "OrcaSlicer"
        data.mkdir(parents=True)
        env = self._fake_uname("Darwin")
        env["HOME"] = str(home)
        result = run_install(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (data / "orca_plugins" / "filament_match" / "filament_match.py").is_file())


if __name__ == "__main__":
    unittest.main()
