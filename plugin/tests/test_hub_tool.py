"""Tests for the Orca Cloud metadata helper.

No network calls: the API is replaced with a small fake for dry-run checks.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
HUB_TOOL = PLUGIN_DIR / "dev" / "orca_hub.py"


def _load_hub():
    spec = importlib.util.spec_from_file_location("fm_orca_hub_test", HUB_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = _load_hub()
        self.session = Path(self.temp.name) / ".secrets" / "orca_cloud.json"
        self.hub.SESSION_PATH = self.session

    def tearDown(self):
        self.temp.cleanup()

    def test_session_is_written_owner_only(self):
        self.hub._save_session({"refresh_token": "secret", "user_id": "u1"})
        self.assertEqual(
            json.loads(self.session.read_text(encoding="utf-8"))["refresh_token"],
            "secret",
        )
        if os.name == "posix":
            mode = stat.S_IMODE(self.session.stat().st_mode)
            self.assertEqual(mode, 0o600)
            directory_mode = stat.S_IMODE(self.session.parent.stat().st_mode)
            self.assertEqual(directory_mode, 0o700)

    def test_session_replacement_leaves_no_temp_file(self):
        self.hub._save_session({"refresh_token": "first"})
        self.hub._save_session({"refresh_token": "second"})
        self.assertEqual(
            json.loads(self.session.read_text(encoding="utf-8"))["refresh_token"],
            "second",
        )
        leftovers = list(self.session.parent.glob(".orca_cloud.*.tmp"))
        self.assertEqual(leftovers, [])


class DescriptionLimitTests(unittest.TestCase):
    def setUp(self):
        self.hub = _load_hub()

    def test_ascii_at_limit_is_accepted(self):
        text = "a" * 10_000
        self.assertEqual(self.hub._check_description(text), (10_000, 10_000))

    def test_character_limit_is_enforced(self):
        with self.assertRaises(self.hub.HubError):
            self.hub._check_description("a" * 10_001)

    def test_byte_limit_is_enforced_for_multibyte_text(self):
        # 5,001 characters but 10,002 UTF-8 bytes: under the character limit,
        # over the byte limit.
        with self.assertRaises(self.hub.HubError):
            self.hub._check_description("é" * 5_001)

    def test_dry_run_uses_the_same_limit(self):
        original = self.hub._api
        self.hub._api = lambda method, path, json_body=None: (
            200, {"data": {"description": "current", "name": "Filament Match"}})
        try:
            with contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                code = self.hub.main([
                    "update", "--description", "a" * 10_001, "--dry-run"])
            self.assertEqual(code, 1)
            self.assertIn("10,001 characters", stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")
        finally:
            self.hub._api = original

    def test_dry_run_accepts_a_valid_description(self):
        original = self.hub._api
        self.hub._api = lambda method, path, json_body=None: (
            200, {"data": {"description": "current", "name": "Filament Match"}})
        try:
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = self.hub.main([
                    "update", "--description", "short description", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("characters", stdout.getvalue())
            self.assertIn("short description", stdout.getvalue())
        finally:
            self.hub._api = original


if __name__ == "__main__":
    unittest.main()
