"""Integration simulation for the entry point with a fake Orca host.

This does not replace the live test. It exercises the real entry code end to
end against a fake preset bundle and a fake slot API, which catches wiring
mistakes (snapshot -> match -> apply -> history) before restarting Orca.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ENTRY = PLUGIN_DIR / "filament_match.py"
DIST = PLUGIN_DIR / "dist" / "filament_match.py"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


class FakePreset:
    def __init__(self, name, *, vendor="", material="", subtype="", color="",
                 filament_id="", system=True, user=False, bundle_id="", file="",
                 visible=True, compatible=True, label=""):
        self.name = name
        self._label = label
        self._values = {
            "filament_vendor": vendor,
            "filament_type": material,
            "filament_sub_type": subtype,
            "filament_colour": color,
            "filament_id": filament_id,
        }
        self.is_system = system
        self.is_user = user
        self.is_default = False
        self.is_visible = visible
        self.is_compatible = compatible
        self.bundle_id = bundle_id
        self.file = file

    def config_value(self, key):
        return self._values.get(key)

    def config_keys(self):
        return list(self._values)

    def label(self):
        return self._label or self.name


class FakeCollection:
    def __init__(self, presets):
        self._presets = presets

    def size(self):
        return len(self._presets)

    def preset(self, index):
        return self._presets[index]


class FakeBundle:
    def __init__(self, presets, slots, printer="Snapmaker U1", print_host="10.0.0.176",
                 data_dir=""):
        self.filaments = FakeCollection(presets)
        self.current_filament_preset_names = list(slots)
        self._printer_preset = FakePreset(printer, system=True)
        self._printer_preset._values["print_host"] = print_host
        self._values = {"print_host": print_host}
        self.data_dir = data_dir

    @property
    def current_printer_preset(self):
        return self._printer_preset

    def full_config_value(self, key):
        return self._values.get(key)


class FakeWindow:
    def __init__(self):
        self.posts = []
        self.open = True

    def post(self, message):
        self.posts.append(message)

    def is_open(self):
        return self.open

    def close(self):
        self.open = False


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

    module.script = types.SimpleNamespace(ScriptPluginCapabilityBase=ScriptPluginCapabilityBase)
    module.base = PluginBase
    module.plugin = lambda cls: cls
    module.register_capability = lambda cls: registered.append(cls)
    module.PluginResult = PluginResult
    module.ExecutionResult = ExecutionResult
    module.PythonPluginBase = PythonPluginBase
    module._registered = registered
    module.host = types.SimpleNamespace()
    return module


class IntegrationSimulationTests(unittest.TestCase):
    entry = ENTRY

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = self.temp.name
        self.bundle = FakeBundle([
            FakePreset("Prusament PLA @Test", vendor="Prusament", material="PLA",
                       color="#000000"),
            FakePreset("Prusament PETG @Test", vendor="Prusament", material="PETG",
                       color="#FFFFFF"),
            FakePreset("Generic PLA @Test", vendor="Generic", material="PLA",
                       color="#000000"),
            FakePreset("Generic PETG @Test", vendor="Generic", material="PETG",
                       color="#FFFFFF"),
            FakePreset("_local/filament_match/Custom PLA", vendor="Custom",
                       material="PLA", color="#112233", system=False, user=False,
                       bundle_id="filament_match", label="Custom PLA"),
        ], ["Prusament PLA @Test", "Prusament PETG @Test"])

        self.selects = []

        def begin_update(slot, name):
            self.selects.append((slot, name))

            class Result:
                slot_assignment_changed = False

                def commit(self):
                    return self

            return Result()

        self.window = FakeWindow()
        self.messages = []
        fake = _fake_orca()
        fake.host = types.SimpleNamespace(
            preset_bundle=lambda: self.bundle,
            plugin=types.SimpleNamespace(storage=lambda: self.storage),
            selected_printer=lambda: {
                "agent": "snapmaker", "model_id": "", "connection": "lan",
                "online": True, "connected": True, "firmware": "",
            },
            ui=types.SimpleNamespace(
                create_window=lambda *a, **k: self.window,
                message=lambda text, **k: self.messages.append(text)),
            preset=types.SimpleNamespace(
                filament=types.SimpleNamespace(begin_update=begin_update),
                bundles=types.SimpleNamespace(reload_local=lambda bundle_id: None)),
        )
        sys.modules["orca"] = fake
        self.fake = fake
        spec = importlib.util.spec_from_file_location("filament_match_integration", self.entry)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.capability = self.module.FilamentMatch()
        self.capability.window = self.window
        self.capability._storage = self.storage

        self.observation_type = self.module._DEPS["Observation"]
        self.original_reader = self.module._DEPS["read_printer"]
        self.original_preflight = self.module._preflight_host
        self.module._preflight_host = lambda *args, **kwargs: None

    def tearDown(self):
        self.module._DEPS["read_printer"] = self.original_reader
        self.module._preflight_host = self.original_preflight
        sys.modules.pop("orca", None)
        self.temp.cleanup()

    # -- helpers ---------------------------------------------------------

    def _settings(self, **overrides):
        settings = {
            "spoolman_url": "",
            "ofd_enabled": False,
        }
        settings.update(overrides)
        return settings

    def _snapshot(self):
        return self.module._preset_snapshot(self.bundle)

    def _observations(self):
        return [
            self.observation_type(
                slot=1, present=True, vendor="Prusament", material="PLA",
                color="#000000", spool_id=7, source="snapmaker"),
            self.observation_type(
                slot=2, present=True, vendor="Prusament", material="PETG",
                color="#FFFFFF", spool_id=8, source="snapmaker"),
        ]

    def _scan(self):
        self.module._DEPS["read_printer"] = (
            lambda *args, **kwargs: (self._observations(), []))
        return self.capability._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })

    def _history(self):
        with open(Path(self.storage) / "history.json", encoding="utf-8") as stream:
            return json.load(stream)

    def _prepared_snapshot(self, presets, slots=None, printer="Snapmaker U1 (0.4 nozzle)"):
        self.bundle = FakeBundle(presets, slots or [], printer=printer)
        snapshot = self._snapshot()
        snapshot.update({
            "data_dir": self.storage,
            "folder": "profile",
            "folder_inferred": True,
        })
        (Path(self.storage) / "user" / "profile").mkdir(parents=True, exist_ok=True)
        return snapshot

    def _preset_info(self, snapshot, name):
        return next(item for item in snapshot["presets"] if item.name == name)

    def _local_filament_files(self):
        directory = (Path(self.storage) / "user" / "profile" / "_local" /
                     "filament_match" / "filament")
        return sorted(directory.glob("*.json")) if directory.is_dir() else []

    # -- tests -----------------------------------------------------------

    def test_init_message_contains_presets_and_slots(self):
        self.capability._on_message({"command": "init", "rid": 1})
        init = self.window.posts[-1]["init"]
        self.assertEqual(init["printer"], "Snapmaker U1")
        self.assertEqual(init["slots"], ["Prusament PLA @Test", "Prusament PETG @Test"])
        names = {item["name"] for item in init["presets"]}
        self.assertIn("Prusament PLA @Test", names)
        self.assertEqual(init["settings"]["spoolman_url"], "")
        self.assertEqual(init["connection"]["host"], "10.0.0.176")
        self.assertTrue(init["connection"]["connected"])
        self.assertNotIn("api_key", init["connection"])
        self.assertNotIn('id="moonraker"', self.module.PAGE)

    def test_user_path_ignores_a_home_directory_named_user(self):
        data, folder = self.module._user_path(
            "/home/user/.config/OrcaSlicer/user/ABC-123/filament/PLA.json")
        self.assertEqual(data, "/home/user/.config/OrcaSlicer")
        self.assertEqual(folder, "ABC-123")

    def test_user_path_finds_local_bundle_folder(self):
        data, folder = self.module._user_path(
            "/tmp/data/user/ABC-123/_local/filament_match/filament/X.json")
        self.assertEqual(data, "/tmp/data")
        self.assertEqual(folder, "ABC-123")

    def test_snapshot_infers_user_folder_from_printer_preset(self):
        self.bundle._printer_preset.file = (
            "/tmp/data/user/ABC-123/machine/Snapmaker U1.json")
        snapshot = self._snapshot()
        self.assertEqual(snapshot["folder"], "ABC-123")
        self.assertTrue(snapshot["folder_inferred"])
        self.assertEqual(snapshot["data_dir"], "/tmp/data")

    def test_snapshot_hides_other_nozzle_variants(self):
        presets = [
            FakePreset("Snapmaker PLA SnapSpeed @U1", vendor="Snapmaker",
                       material="PLA", color="#E2DEDB"),
            FakePreset("Snapmaker PLA SnapSpeed @U1 0.2 nozzle", vendor="Snapmaker",
                       material="PLA", color="#E2DEDB"),
            FakePreset("Snapmaker PLA SnapSpeed @U1 0.6 nozzle", vendor="Snapmaker",
                       material="PLA", color="#E2DEDB"),
        ]
        bundle = FakeBundle(
            presets, ["Snapmaker PLA SnapSpeed @U1"],
            printer="Snapmaker U1 (0.4 nozzle)", print_host="10.0.0.176")
        snapshot = self.module._preset_snapshot(bundle)
        names = {item["name"] for item in snapshot["ui_presets"]}
        self.assertEqual(names, {"Snapmaker PLA SnapSpeed @U1"})
        self.assertEqual(snapshot["printer_nozzle"], "0.4")

    def test_open_ui_falls_back_to_window(self):
        def unavailable(*args, **kwargs):
            raise RuntimeError("dock panels unavailable")

        self.fake.host.ui.create_dock_panel = unavailable
        ui = self.module.FilamentMatch()._open_ui()
        self.assertIs(ui, self.window)

    def test_execute_opens_dock_panel_and_resolves_storage_once(self):
        calls = []
        panel = FakeWindow()

        def storage():
            calls.append("storage")
            return self.storage

        def create_dock_panel(html, **kwargs):
            on_message = kwargs["on_message"]
            on_close = kwargs["on_close"]

            def feed():
                time.sleep(0.05)
                on_message({"command": "init", "rid": 1})
                on_close()

            threading.Thread(target=feed, daemon=True).start()
            return panel

        self.fake.host.plugin = types.SimpleNamespace(storage=storage)
        self.fake.host.ui.create_dock_panel = create_dock_panel
        result = self.module.FilamentMatch().execute()
        self.assertEqual(calls, ["storage"])
        self.assertTrue(any("init" in post for post in panel.posts))
        self.assertEqual(result.status, "success")

    def test_execute_does_not_clear_a_newer_window(self):
        panel = FakeWindow()
        newer = FakeWindow()
        capability = self.module.FilamentMatch()

        def create_dock_panel(html, **kwargs):
            on_close = kwargs["on_close"]

            def feed():
                time.sleep(0.05)
                capability.window = newer
                on_close()

            threading.Thread(target=feed, daemon=True).start()
            return panel

        self.fake.host.ui.create_dock_panel = create_dock_panel
        result = capability.execute()
        self.assertEqual(result.status, "success")
        self.assertIs(capability.window, newer)

    def test_page_layout_is_feeds_first_and_settings_collapsed(self):
        page = self.module.PAGE
        self.assertLess(page.index('id="rows"'), page.index('id="settingsTile"'))
        self.assertIn('<details class="settings" id="settingsTile">', page)
        self.assertNotIn('<details class="settings" id="settingsTile" open', page)
        self.assertNotIn('id="moonraker"', page)
        for removed in ('Apply &amp; remember', 'Apply only', 'Use generic',
                        'Don&#39;t ask again', 'Current slot', 'Show reported fields'):
            self.assertNotIn(removed, page)
        self.assertIn('id="rescan"', page)
        self.assertIn('id="apply"', page)
        self.assertIn('id="mismatch"', page)
        self.assertIn('id="clearCache"', page)
        self.assertIn("post('clear_cache')", page)
        self.assertIn('data-price="new"', page)
        self.assertIn('data-action="fetch-price"', page)
        self.assertIn('data-action="save-price"', page)
        self.assertIn("post('fetch_price'", page)
        self.assertIn('price: row.price_pending', page)
        self.assertIn('slot_exists !== false', page)
        self.assertLess(page.index('id="apply"'), page.index('id="rescan"'))
        self.assertIn('data-action="edit"', page)
        self.assertIn('data-action="expand"', page)
        self.assertIn('data-action="match"', page)
        self.assertIn("post('scan');", page)
        self.assertIn("post('apply_all'", page)
        self.assertNotIn('apply: true', page)

    def test_scan_without_printer_shows_popup_and_does_not_enqueue(self):
        self.bundle = FakeBundle(
            [], ["Prusament PLA @Test"], printer="Snapmaker U1", print_host="")
        self.fake.host.selected_printer = lambda: None
        self.capability._on_message({"command": "scan", "rid": 7})
        self.assertTrue(self.messages, "expected a native connect-printer popup")
        self.assertIn("connect a printer", self.messages[-1].casefold())
        self.assertIn("error", self.window.posts[-1])

    def test_storage_is_not_resolved_from_message_handler(self):
        def forbidden():
            raise RuntimeError(
                "plugin.storage() must be called from a plugin callback")

        self.fake.host.plugin = types.SimpleNamespace(storage=forbidden)
        self.capability._on_message({"command": "init", "rid": 2})
        self.assertIn("init", self.window.posts[-1])

    def test_connected_printer_without_host_gets_specific_popup(self):
        problem = self.module._printer_problem({
            "host": "", "selected": {"connected": True, "online": True}})
        self.assertIn("print host", problem.casefold())

    def test_printer_base_url_returns_one_configured_or_agent_url(self):
        self.assertEqual(
            self.module._printer_base_urls(
                {"host": "10.0.0.176", "agent": "snapmaker"}),
            ["http://10.0.0.176"])
        self.assertEqual(
            self.module._printer_base_urls(
                {"host": "moon.local", "agent": "moonraker"}),
            ["http://moon.local:7125"])
        self.assertEqual(
            self.module._printer_base_urls(
                {"host": "10.0.0.176", "port": "7912",
                 "agent": "snapmaker"}),
            ["http://10.0.0.176:7912"])
        self.assertEqual(
            self.module._printer_base_urls(
                {"host": "http://10.0.0.176:7912", "port": "9999"}),
            ["http://10.0.0.176:7912"])
        self.assertEqual(
            self.module._printer_base_urls(
                {"host": "fd00::1", "agent": "moonraker"}),
            ["http://[fd00::1]:7125"])
        self.assertEqual(
            self.module._printer_base_urls(
                {"host": "fd00::1", "port": "7912"}),
            ["http://[fd00::1]:7912"])

    def test_read_printer_passes_agent_and_configured_port(self):
        calls = []

        def reader(url, api_key="", *, timeout=0, agent="", port=None):
            calls.append((url, api_key, timeout, agent, port))
            return ([self.observation_type(
                slot=1, present=True, vendor="Prusament", material="PLA",
                source="moonraker")], [])

        original = self.module._DEPS["read_printer"]
        self.module._DEPS["read_printer"] = reader
        try:
            observations = self.module._read_printer({
                "printer_info": {
                    "host": "10.0.0.50", "port": "7912", "agent": "moonraker",
                },
            }, [])
        finally:
            self.module._DEPS["read_printer"] = original
        self.assertEqual(len(observations), 1)
        self.assertEqual(calls, [("http://10.0.0.50:7912", "", 4.0,
                                  "moonraker", "7912")])

    def test_read_printer_falls_back_for_legacy_reader_signature(self):
        calls = []

        def reader(url, api_key="", timeout=0):
            calls.append((url, api_key, timeout))
            return ([self.observation_type(
                slot=1, present=True, vendor="Prusament", material="PLA",
                source="snapmaker")], [])

        original = self.module._DEPS["read_printer"]
        self.module._DEPS["read_printer"] = reader
        try:
            observations = self.module._read_printer({
                "printer_info": {"host": "10.0.0.176", "agent": "snapmaker"},
            }, [])
        finally:
            self.module._DEPS["read_printer"] = original
        self.assertEqual(len(observations), 1)
        self.assertEqual(calls, [("http://10.0.0.176", "", 4.0)])

    def test_scan_matches_each_slot_and_keeps_label(self):
        reply = self._scan()
        self.assertEqual(reply["rid"], None)
        rows = reply["scan"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["preset_name"], "Prusament PLA @Test")
        self.assertEqual(rows[0]["slot"], 1)
        self.assertEqual(rows[1]["preset_name"], "Prusament PETG @Test")
        self.assertTrue(all(row["slot_exists"] for row in rows))

    def test_slot_mismatch_reports_missing_and_extra_slots(self):
        missing = self.module._slot_mismatch(9, 4)
        self.assertEqual(missing["missing_slots"], 5)
        self.assertEqual(missing["extra_slots"], 0)
        self.assertIn("Add 5 slot(s)", missing["message"])
        extra = self.module._slot_mismatch(2, 4)
        self.assertEqual(extra["missing_slots"], 0)
        self.assertEqual(extra["extra_slots"], 2)
        self.assertIn("remove", extra["message"].casefold())
        equal = self.module._slot_mismatch(4, 4)
        self.assertEqual(equal["message"], "")

    def test_scan_marks_feeds_without_project_slots(self):
        observations = self._observations() + [
            self.observation_type(
                slot=3, present=True, vendor="Prusament", material="ABS",
                color="#808080", spool_id=9, source="snapmaker"),
        ]
        self.module._DEPS["read_printer"] = lambda *a, **k: (observations, [])
        reply = self.capability._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertEqual(reply["slot_mismatch"]["feeders"], 3)
        self.assertEqual(reply["slot_mismatch"]["slots"], 2)
        self.assertEqual(reply["slot_mismatch"]["missing_slots"], 1)
        self.assertFalse(reply["scan"][2]["slot_exists"])
        self.assertIn("Add 1 slot(s)", reply["message"])

    def test_scan_reports_extra_project_slots(self):
        observations = self._observations()[:1]
        self.module._DEPS["read_printer"] = lambda *a, **k: (observations, [])
        reply = self.capability._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertEqual(reply["slot_mismatch"]["feeders"], 1)
        self.assertEqual(reply["slot_mismatch"]["slots"], 2)
        self.assertEqual(reply["slot_mismatch"]["extra_slots"], 1)
        self.assertIn("remove", reply["message"].casefold())

    def test_scan_does_not_apply_confident_matches(self):
        self.bundle.current_filament_preset_names = ["Generic PLA @Test", "Generic PETG @Test"]
        reply = self._scan()
        rows = reply["scan"]
        self.assertFalse(rows[0]["applied"])
        self.assertFalse(rows[1]["applied"])
        self.assertEqual(rows[0]["preset_name"], "Prusament PLA @Test")
        self.assertEqual(rows[1]["preset_name"], "Prusament PETG @Test")
        self.assertEqual(self.selects, [])
        self.assertFalse((Path(self.storage) / "history.json").exists())

    def test_match_button_only_selects(self):
        reply = self.capability._run({
            "command": "match",
            "slot": 1,
            "observation": {"vendor": "Prusament", "material": "PLA", "spool_id": 7},
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertIsNone(reply["applied"])
        self.assertFalse(reply["match"]["applied"])
        self.assertFalse(reply["match"]["user_applied"])
        self.assertEqual(reply["match"]["preset_name"], "Prusament PLA @Test")
        self.assertIn("Press Apply", reply["message"])
        self.assertEqual(self.selects, [])
        self.assertFalse((Path(self.storage) / "history.json").exists())

    def test_apply_all_applies_each_selection_and_remembers(self):
        reply = self.capability._run({
            "command": "apply_all",
            "entries": [
                {"slot": 1, "preset_name": "Prusament PLA @Test",
                 "source": "installed", "remember": True,
                 "observation": {"vendor": "Prusament", "material": "PLA",
                                 "spool_id": 7}},
                {"slot": 2, "preset_name": "Prusament PETG @Test",
                 "source": "installed", "remember": True,
                 "observation": {"vendor": "Prusament", "material": "PETG",
                                 "spool_id": 8}},
            ],
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertEqual(
            self.selects,
            [(1, "Prusament PLA @Test"), (2, "Prusament PETG @Test")],
        )
        self.assertEqual(len(reply["applied_all"]), 2)
        self.assertEqual(reply["apply_errors"], [])
        self.assertEqual(reply["message"], "Applied 2 feed(s).")
        saved = self._history()
        self.assertEqual(saved["spools"]["7"]["default"], "Prusament PLA @Test")
        self.assertEqual(saved["spools"]["8"]["default"], "Prusament PETG @Test")

    def test_apply_all_skips_entries_beyond_project_slots(self):
        reply = self.capability._run({
            "command": "apply_all",
            "entries": [
                {"slot": 99, "preset_name": "Prusament PLA @Test",
                 "source": "installed", "remember": True,
                 "observation": {"vendor": "Prusament", "material": "PLA",
                                 "spool_id": 7}},
                {"slot": 1, "preset_name": "Prusament PLA @Test",
                 "source": "installed", "remember": True,
                 "observation": {"vendor": "Prusament", "material": "PLA",
                                 "spool_id": 7}},
            ],
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertEqual(self.selects, [(1, "Prusament PLA @Test")])
        self.assertEqual(len(reply["applied_all"]), 1)
        self.assertEqual(reply["apply_errors"], [])
        self.assertEqual(len(reply["apply_skipped"]), 1)
        self.assertEqual(reply["apply_skipped"][0]["slot"], 99)
        self.assertIn("add 1 filament slot", reply["message"])
        self.assertEqual(reply["slot_mismatch"]["missing_slots"], 1)

    def test_apply_all_reports_one_preset_failure_and_keeps_going(self):
        reply = self.capability._run({
            "command": "apply_all",
            "entries": [
                {"slot": 1, "preset_name": "Missing Preset",
                 "source": "installed", "remember": True,
                 "observation": {"vendor": "Prusament", "material": "PLA",
                                 "spool_id": 7}},
                {"slot": 2, "preset_name": "Prusament PETG @Test",
                 "source": "installed", "remember": True,
                 "observation": {"vendor": "Prusament", "material": "PETG",
                                 "spool_id": 8}},
            ],
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertEqual(self.selects, [(2, "Prusament PETG @Test")])
        self.assertEqual(len(reply["applied_all"]), 1)
        self.assertEqual(len(reply["apply_errors"]), 1)
        self.assertEqual(reply["apply_errors"][0]["slot"], 1)

    def test_library_install_writes_the_remapped_parent(self):
        self.bundle = FakeBundle([
            FakePreset("Generic PETG @System", vendor="Generic",
                       material="PETG", compatible=False),
            FakePreset("Generic PETG @U1 0.4 nozzle", vendor="Generic",
                       material="PETG", compatible=True),
        ], ["Generic PETG @U1 0.4 nozzle"],
            printer="Snapmaker U1 (0.4 nozzle)")
        snapshot = self._snapshot()
        snapshot.update({
            "data_dir": self.storage,
            "folder": "profile",
            "folder_inferred": True,
        })
        (Path(self.storage) / "user" / "profile").mkdir(parents=True)

        class Library:
            def preset_is_cached(self, candidate):
                return True

            def fetch(self, candidate):
                return {
                    "name": "CC3D PETG (OFD)",
                    "inherits": "Generic PETG @System",
                    "from": "User",
                    "filament_type": ["PETG"],
                    "filament_vendor": ["CC3D"],
                }

        original = self.module._ofd_library
        self.module._ofd_library = lambda *args, **kwargs: Library()
        try:
            reply = self.module._apply_selection({
                "slot": 1,
                "preset_name": "CC3D PETG (OFD)",
                "source": "library",
                "candidate": {
                    "preset_name": "CC3D PETG (OFD)",
                    "library": {"path": "brands/cc3d/test.json"},
                },
                "remember": True,
                "observation": {"vendor": "CC3D", "material": "PETG",
                                "spool_id": 8},
            }, snapshot, self._settings(ofd_enabled=True), self.storage, {}, [])
        finally:
            self.module._ofd_library = original

        self.assertEqual(self.selects, [(1, "CC3D PETG (OFD)")])
        self.assertTrue(reply["applied"]["remembered"])
        written = list((Path(self.storage) / "user" / "profile" / "_local" /
                        "filament_match" / "filament").glob("*.json"))
        self.assertEqual(len(written), 1)
        installed = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertEqual(installed["inherits"],
                         "Generic PETG @U1 0.4 nozzle")

    def test_library_parent_remaps_shadowed_system_generic(self):
        self.bundle = FakeBundle([
            FakePreset("Generic PETG @System", vendor="Generic",
                       material="PETG", compatible=False),
            FakePreset("Generic PETG @U1 0.4 nozzle", vendor="Generic",
                       material="PETG", compatible=True),
        ], [], printer="Snapmaker U1 (0.4 nozzle)")
        warnings = []
        result = self.module._validated_library_preset({
            "name": "CC3D PETG (OFD)",
            "inherits": "Generic PETG @System",
            "from": "User",
            "filament_type": ["PETG"],
            "filament_vendor": ["CC3D"],
        }, self._snapshot(), warnings)
        self.assertEqual(result["inherits"],
                         "Generic PETG @U1 0.4 nozzle")
        self.assertEqual(result["type"], "filament")
        self.assertTrue(any("Using compatible parent" in text
                            for text in warnings))

    def test_library_parent_uses_the_active_nozzle_variant(self):
        self.bundle = FakeBundle([
            FakePreset("Generic PLA @System", vendor="Generic",
                       material="PLA", compatible=False),
            FakePreset("Generic PLA @U1 0.4 nozzle", vendor="Generic",
                       material="PLA", compatible=False),
            FakePreset("Generic PLA @U1 0.6 nozzle", vendor="Generic",
                       material="PLA", compatible=True),
        ], [], printer="Snapmaker U1 (0.6 nozzle)")
        result = self.module._validated_library_preset({
            "name": "The Filament PLA Lite (OFD)",
            "inherits": "Generic PLA @System",
            "from": "User",
            "filament_type": ["PLA"],
            "filament_vendor": ["The Filament"],
        }, self._snapshot(), [])
        self.assertEqual(result["inherits"],
                         "Generic PLA @U1 0.6 nozzle")

    def test_library_parent_does_not_remap_non_generic_parent(self):
        self.bundle = FakeBundle([
            FakePreset("Vendor PETG @U1", vendor="Vendor", material="PETG",
                       compatible=False),
            FakePreset("Vendor PETG Base @U1", vendor="Vendor",
                       material="PETG", compatible=True),
        ], [], printer="Snapmaker U1 (0.4 nozzle)")
        with self.assertRaises(ValueError) as caught:
            self.module._validated_library_preset({
                "name": "Vendor PETG (OFD)",
                "inherits": "Vendor PETG @U1",
                "from": "User",
                "filament_type": ["PETG"],
                "filament_vendor": ["Vendor"],
            }, self._snapshot(), [])
        self.assertIn("not compatible", str(caught.exception))

    def test_spoolman_price_is_converted_to_per_kg(self):
        self.assertEqual(
            self.module._spoolman_cost_per_kg(
                {"price": 19.99, "initial_weight": 1000}, {"weight": 1000}),
            "19.99")
        self.assertEqual(
            self.module._spoolman_cost_per_kg(
                {"price": 6.25, "initial_weight": 250}, {"weight": 1000}),
            "25.00")
        self.assertEqual(
            self.module._spoolman_cost_per_kg(
                {"price": None, "initial_weight": 500}, {"price": 20}),
            "40.00")
        self.assertEqual(
            self.module._spoolman_cost_per_kg(
                {"price": 10, "initial_weight": 0}, {"weight": 0}),
            "")

    def test_snapshot_resolves_inherited_preset_cost(self):
        path = Path(self.storage) / "CC3D PETG (OFD).json"
        path.write_text(json.dumps({
            "name": "_local/filament_match/CC3D PETG (OFD)",
            "inherits": "Generic PETG @System",
            "filament_type": ["PETG"],
            "filament_vendor": ["CC3D"],
        }), encoding="utf-8")
        parent = FakePreset("Generic PETG @System", vendor="Generic",
                            material="PETG")
        parent._values["filament_cost"] = "30"
        child = FakePreset("_local/filament_match/CC3D PETG (OFD)",
                           vendor="CC3D", material="PETG", system=False,
                           bundle_id="filament_match", file=str(path),
                           label="CC3D PETG (OFD)")
        self.bundle = FakeBundle([parent, child],
                                 ["_local/filament_match/CC3D PETG (OFD)"])
        snapshot = self._snapshot()
        ui = next(item for item in snapshot["ui_presets"]
                  if item["name"] == "_local/filament_match/CC3D PETG (OFD)")
        self.assertEqual(ui["cost"], "30.00")
        info = next(item for item in snapshot["presets"]
                    if item.name == "_local/filament_match/CC3D PETG (OFD)")
        self.assertEqual(info.config["filament_cost"], "30.00")

    def test_snapshot_and_row_expose_current_and_spool_prices(self):
        filament = FakePreset(
            "eSUN PETG @Test", vendor="eSUN", material="PETG",
            color="#03720A")
        filament._values["filament_cost"] = ["21.50"]
        self.bundle = FakeBundle([filament], ["eSUN PETG @Test"])
        snapshot = self._snapshot()
        ui = next(item for item in snapshot["ui_presets"]
                  if item["name"] == "eSUN PETG @Test")
        self.assertEqual(ui["cost"], "21.50")
        observation = self.observation_type(
            slot=1, present=True, vendor="eSUN", material="PETG",
            product="PETG Basic Transparent Green", spool_id=7)
        row = self.module._build_row(
            observation, snapshot, {}, self._settings(), self.storage,
            {"spool_prices": {"7": "22.75"}}, [])
        self.assertEqual(row["current_price"], "21.50")
        self.assertEqual(row["spool_price"], "22.75")

    def test_enrich_spoolman_sets_status_and_prices(self):
        class Client:
            base_url = "https://spoolman.test"

            def get_spool(self, spool_id):
                return {"price": 19.99, "initial_weight": 1000,
                        "filament": {"name": "ABS", "weight": 1000}}

        observation = self.observation_type(
            slot=1, present=True, vendor="3DJake", material="ABS",
            spool_id=6)
        session = {
            "spoolman_client": Client(),
            "spoolman_url": "https://spoolman.test",
            "spoolman_cache": {},
        }
        self.module._enrich_spoolman(
            [observation],
            {"spoolman_url": "https://spoolman.test"}, session, [])
        self.assertTrue(session["spoolman_status"]["available"])
        self.assertEqual(session["spool_prices"]["6"], "19.99")

        empty = {}
        self.module._enrich_spoolman([], {"spoolman_url": ""}, empty, [])
        self.assertFalse(empty["spoolman_status"]["available"])
        self.assertIn("Add a Spoolman URL", empty["spoolman_status"]["message"])

    def test_fetch_price_command_uses_spoolman_and_reports_errors(self):
        class Client:
            base_url = "https://spoolman.test"

            def get_spool(self, spool_id):
                return {"price": 10.39, "initial_weight": 1000,
                        "filament": {"name": "PETG", "weight": 1000}}

        session = {
            "spoolman_client": Client(),
            "spoolman_url": "https://spoolman.test",
            "spoolman_cache": {"9": ({}, {})},
        }
        self.capability._session = session
        reply = self.capability._run({
            "command": "fetch_price",
            "slot": 1,
            "observation": {"spool_id": 9},
            "storage": self.storage,
            "settings": self._settings(spoolman_url="https://spoolman.test"),
            "snapshot": {},
        })
        self.assertEqual(reply["price_fetched"]["price"], "10.39")
        self.assertEqual(session["spool_prices"]["9"], "10.39")
        self.assertIn("9", session["spoolman_cache"])

        missing = self.capability._run({
            "command": "fetch_price",
            "slot": 1,
            "observation": {"spool_id": ""},
            "storage": self.storage,
            "settings": self._settings(spoolman_url="https://spoolman.test"),
            "snapshot": {},
        })
        self.assertIn("no Spoolman spool", missing["price_error"]["error"])

        no_url = self.capability._run({
            "command": "fetch_price",
            "slot": 1,
            "observation": {"spool_id": 9},
            "storage": self.storage,
            "settings": self._settings(spoolman_url=""),
            "snapshot": {},
        })
        self.assertIn("Add a Spoolman URL", no_url["price_error"]["error"])

    def test_spoolman_client_accepts_bare_host_and_api_suffix(self):
        original = self.module._DEPS["SpoolmanClient"]
        seen = []

        class Client:
            def __init__(self, base_url):
                seen.append(base_url)
                self.base_url = base_url.rstrip("/")

        self.module._DEPS["SpoolmanClient"] = Client
        warnings = []
        try:
            client = self.module._spoolman_client(
                {"spoolman_url": "spoolman.local:7912/api/v1"}, {}, warnings)
        finally:
            self.module._DEPS["SpoolmanClient"] = original
        self.assertIsNotNone(client)
        self.assertEqual(seen, ["spoolman.local:7912/api/v1"])
        self.assertEqual(warnings, [])

    def test_spoolman_client_reports_an_unusable_client(self):
        original = self.module._DEPS["SpoolmanClient"]

        class Client:
            def __init__(self, base_url):
                self.base_url = ""

        self.module._DEPS["SpoolmanClient"] = Client
        warnings = []
        try:
            client = self.module._spoolman_client(
                {"spoolman_url": "spoolman.local"}, {}, warnings)
        finally:
            self.module._DEPS["SpoolmanClient"] = original
        self.assertIsNone(client)
        self.assertTrue(any("not usable" in warning for warning in warnings))

    def test_enrich_spoolman_accepts_bare_host(self):
        original = self.module._DEPS["SpoolmanClient"]

        class Client:
            base_url = "http://spoolman.local:7912"

            def __init__(self, base_url):
                self.base_url = "http://spoolman.local:7912"

            def get_spool(self, spool_id):
                return {"price": 20.00, "initial_weight": 1000,
                        "filament": {"name": "PETG", "weight": 1000}}

        self.module._DEPS["SpoolmanClient"] = Client
        session = {}
        warnings = []
        observation = self.observation_type(
            slot=1, present=True, vendor="eSUN", material="PETG", spool_id=9)
        try:
            self.module._enrich_spoolman(
                [observation], {"spoolman_url": "spoolman.local:7912"},
                session, warnings)
        finally:
            self.module._DEPS["SpoolmanClient"] = original
        self.assertTrue(session["spoolman_status"]["available"])
        self.assertEqual(session["spool_prices"]["9"], "20.00")
        self.assertEqual(warnings, [])

    def test_price_copy_of_user_preset_flattens_its_parent(self):
        user_path = (Path(self.storage) / "user" / "profile" / "filament" /
                     "CC3D PETG (custom).json")
        user_path.parent.mkdir(parents=True)
        user_path.write_text(json.dumps({
            "name": "CC3D PETG (custom)",
            "from": "User",
            "inherits": "Generic PETG @System",
            "filament_vendor": ["CC3D"],
            "nozzle_temperature": ["240"],
            "nozzle_temperature_initial_layer": ["245"],
            "hot_plate_temp": ["55"],
            "filament_notes": ["custom note"],
        }), encoding="utf-8")
        parent = FakePreset("Generic PETG @System", vendor="Generic",
                            material="PETG")
        parent._values["filament_cost"] = "30"
        custom = FakePreset("CC3D PETG (custom)", vendor="CC3D",
                            material="PETG", system=False, user=True,
                            file=str(user_path),
                            label="CC3D PETG (custom)")
        self.bundle = FakeBundle([parent, custom],
                                 ["CC3D PETG (custom)"],
                                 printer="Snapmaker U1 (0.4 nozzle)")
        snapshot = self._snapshot()
        snapshot.update({
            "data_dir": self.storage,
            "folder": "profile",
            "folder_inferred": True,
        })
        custom_info = next(
            item for item in snapshot["presets"]
            if item.name == "CC3D PETG (custom)")
        copy_name = self.module._create_price_copy(
            custom_info, "10.07", snapshot, [])
        self.assertEqual(copy_name, "CC3D PETG (custom) (local)")
        # Pressing Save price again must update the same copy, not create a
        # second one just because a flattened user copy inherits the root.
        self.assertEqual(
            self.module._create_price_copy(custom_info, "11.07", snapshot, []),
            copy_name)
        self.assertEqual(len(self._local_filament_files()), 1)
        target = (Path(self.storage) / "user" / "profile" / "_local" /
                  "filament_match" / "filament" /
                  "CC3D PETG (custom) (local).json")
        self.assertTrue(target.exists())
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["inherits"], "Generic PETG @System")
        self.assertEqual(data["filament_cost"], ["11.07"])
        self.assertEqual(data["filament_vendor"], ["CC3D"])
        self.assertEqual(data["filament_type"], ["PETG"])
        self.assertEqual(data["nozzle_temperature"], ["240"])
        self.assertEqual(data["hot_plate_temp"], ["55"])
        self.assertNotIn("CC3D PETG (custom)", data["inherits"])

    def test_apply_price_on_system_preset_creates_local_copy(self):
        self.bundle = FakeBundle([
            FakePreset("eSUN PETG @System", vendor="eSUN", material="PETG",
                       color="#03720A"),
        ], ["eSUN PETG @System"],
            printer="Snapmaker U1 (0.4 nozzle)")
        snapshot = self._snapshot()
        snapshot.update({
            "data_dir": self.storage,
            "folder": "profile",
            "folder_inferred": True,
        })
        (Path(self.storage) / "user" / "profile").mkdir(parents=True)
        reply = self.module._apply_selection({
            "slot": 1,
            "preset_name": "eSUN PETG @System",
            "source": "installed",
            "remember": True,
            "price": "10.39",
            "observation": {"vendor": "eSUN", "material": "PETG",
                            "spool_id": 9},
        }, snapshot, self._settings(), self.storage, {}, [])
        self.assertEqual(reply["applied"]["price"], "10.39")
        self.assertTrue(reply["applied"]["price_persisted"])
        self.assertTrue(reply["applied"]["price_copy"])
        self.assertEqual(reply["applied"]["select_name"], "eSUN PETG (local)")
        self.assertEqual(self.selects, [(1, "eSUN PETG (local)")])
        written = (Path(self.storage) / "user" / "profile" / "_local" /
                   "filament_match" / "filament" / "eSUN PETG (local).json")
        self.assertTrue(written.exists())
        installed = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(installed["inherits"], "eSUN PETG @System")
        self.assertEqual(installed["filament_cost"], ["10.39"])
        self.assertEqual(installed["filament_vendor"], ["eSUN"])
        self.assertEqual(installed["filament_type"], ["PETG"])

    def test_apply_price_on_plugin_local_preset_updates_its_json(self):
        path = (Path(self.storage) / "user" / "profile" / "_local" /
                "filament_match" / "filament" / "CC3D PETG (OFD).json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "name": "CC3D PETG (OFD)",
            "inherits": "Generic PETG @System",
            "filament_cost": ["9.00"],
        }), encoding="utf-8")
        self.bundle = FakeBundle([
            FakePreset("_local/filament_match/CC3D PETG (OFD)",
                       vendor="CC3D", material="PETG", system=False,
                       bundle_id="filament_match", file=str(path),
                       label="CC3D PETG (OFD)"),
        ], ["_local/filament_match/CC3D PETG (OFD)"],
            printer="Snapmaker U1 (0.4 nozzle)")
        snapshot = self._snapshot()
        snapshot.update({
            "data_dir": self.storage,
            "folder": "profile",
            "folder_inferred": True,
        })
        reply = self.module._apply_selection({
            "slot": 1,
            "preset_name": "_local/filament_match/CC3D PETG (OFD)",
            "source": "installed",
            "remember": True,
            "price": "11.50",
            "observation": {"vendor": "CC3D", "material": "PETG",
                            "spool_id": 8},
        }, snapshot, self._settings(), self.storage, {}, [])
        self.assertEqual(reply["applied"]["select_name"], "CC3D PETG (OFD)")
        self.assertFalse(reply["applied"]["price_copy"])
        updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["filament_cost"], ["11.50"])
        self.assertEqual(self.selects, [(1, "CC3D PETG (OFD)")])

    def test_clear_cache_removes_only_ofd_files(self):
        storage = Path(self.storage)
        (storage / "ofd_index.json").write_text("{}", encoding="utf-8")
        preset_dir = storage / "ofd_presets" / "brands" / "cc3d"
        preset_dir.mkdir(parents=True)
        (preset_dir / "petg.json").write_text("{}", encoding="utf-8")
        (storage / "history.json").write_text("{}", encoding="utf-8")
        (storage / "settings.json").write_text("{}", encoding="utf-8")
        self.capability._session = {
            "ofd_library": object(), "ofd_key": ("x", "y")}

        self.capability._on_message({"command": "clear_cache", "rid": 9})
        reply = self.window.posts[-1]
        self.assertEqual(reply["rid"], 9)
        self.assertTrue(reply["cache_cleared"]["index_removed"])
        self.assertEqual(reply["cache_cleared"]["preset_files_removed"], 1)
        self.assertIn("cleared", reply["message"])
        self.assertFalse((storage / "ofd_index.json").exists())
        self.assertFalse((storage / "ofd_presets").exists())
        self.assertTrue((storage / "history.json").exists())
        self.assertTrue((storage / "settings.json").exists())
        self.assertIsNone(self.capability._session["ofd_library"])
        self.assertIsNone(self.capability._session["ofd_key"])

        self.capability._on_message({"command": "clear_cache", "rid": 10})
        empty = self.window.posts[-1]
        self.assertFalse(empty["cache_cleared"]["index_removed"])
        self.assertEqual(empty["cache_cleared"]["preset_files_removed"], 0)
        self.assertIn("already empty", empty["message"])

    def test_clear_cache_removes_scoped_ofd_variants(self):
        storage = Path(self.storage)
        (storage / "ofd_index.json").write_text("{}", encoding="utf-8")
        (storage / "ofd_index-abc123.json").write_text("{}", encoding="utf-8")
        (storage / "ofd_index-def456.json").write_text("{}", encoding="utf-8")
        index_dir = storage / "ofd_index-ghi789"
        index_dir.mkdir(parents=True)
        (index_dir / "ofd_index.json").write_text("{}", encoding="utf-8")
        legacy = storage / "ofd_presets" / "brands" / "legacy"
        legacy.mkdir(parents=True)
        (legacy / "one.json").write_text("{}", encoding="utf-8")
        scoped = storage / "ofd_presets-abc123" / "brands" / "new"
        scoped.mkdir(parents=True)
        (scoped / "two.json").write_text("{}", encoding="utf-8")
        (scoped / "three.json").write_text("{}", encoding="utf-8")
        (storage / "settings.json").write_text("{}", encoding="utf-8")

        removed = self.module._clear_ofd_cache(self.storage)
        self.assertTrue(removed["index_removed"])
        self.assertEqual(removed["index_files_removed"], 4)
        self.assertEqual(removed["preset_files_removed"], 3)
        self.assertFalse((storage / "ofd_index.json").exists())
        self.assertFalse((storage / "ofd_index-abc123.json").exists())
        self.assertFalse((storage / "ofd_index-def456.json").exists())
        self.assertFalse((storage / "ofd_index-ghi789").exists())
        self.assertFalse((storage / "ofd_presets").exists())
        self.assertFalse((storage / "ofd_presets-abc123").exists())
        self.assertTrue((storage / "settings.json").exists())

    def test_library_install_uses_cached_preset_without_network(self):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        storage = Path(self.storage)
        (storage / "ofd_index.json").write_bytes(
            (fixtures / "ofd_index.json").read_bytes())
        preset_path = (storage / "ofd_presets" / "brands" / "polymaker" /
                       "materials" / "PLA" / "filaments" / "polylite_pla.json")
        preset_path.parent.mkdir(parents=True)
        preset = json.loads((fixtures / "ofd_preset.json").read_text(encoding="utf-8"))
        preset["inherits"] = "Generic PLA @Test"
        preset_path.write_text(json.dumps(preset), encoding="utf-8")
        (storage / "user" / "profile").mkdir(parents=True)

        snapshot = self._snapshot()
        snapshot.update({
            "data_dir": self.storage,
            "folder": "profile",
            "folder_inferred": True,
        })
        calls = []
        self.module._preflight_host = lambda *args, **kwargs: calls.append(args)
        reply = self.module._apply_selection({
            "slot": 1,
            "preset_name": "Polymaker PolyLite PLA (OFD)",
            "source": "library",
            "candidate": {
                "preset_name": "Polymaker PolyLite PLA (OFD)",
                "library": {
                    "path": ("brands/polymaker/materials/PLA/filaments/"
                             "polylite_pla.json"),
                },
            },
            "remember": True,
            "observation": {"vendor": "Polymaker", "material": "PLA",
                            "spool_id": 9},
        }, snapshot, self._settings(
            ofd_enabled=True,
            ofd_index_url="https://api.openfilamentdatabase.org/orcaslicer/index.json",
        ), self.storage, {}, [])

        self.assertEqual(calls, [])
        self.assertEqual(self.selects, [(1, "Polymaker PolyLite PLA (OFD)")])
        self.assertTrue(reply["applied"]["remembered"])
        installed = (storage / "user" / "profile" / "_local" / "filament_match" /
                     "filament")
        self.assertTrue(list(installed.glob("*.json")))
        self.assertTrue((storage / "history.json").exists())

    def test_match_uses_the_real_library_stage(self):
        self.bundle = FakeBundle([
            FakePreset("Generic ABS @Test", vendor="Generic", material="ABS",
                       color="#000000"),
        ], ["Generic ABS @Test"])

        class Library:
            def find(self, *args, **kwargs):
                return [{
                    "profile_name": "3DJAKE ABS (OFD)",
                    "brand": "3DJAKE",
                    "material": "ABS",
                    "filament": "ABS",
                    "path": "brands/3djake/materials/ABS/filaments/abs.json",
                    "score": 85,
                }]

            def preset_is_cached(self, candidate):
                return True

        original = self.module._ofd_library
        self.module._ofd_library = lambda *args, **kwargs: Library()
        try:
            reply = self.capability._run({
                "command": "match",
                "slot": 1,
                "observation": {"vendor": "3DJake", "material": "ABS",
                                "product": "ABS Pastel Pink", "spool_id": 6},
                "storage": self.storage,
                "settings": self._settings(ofd_enabled=True),
                "snapshot": self._snapshot(),
            })
        finally:
            self.module._ofd_library = original
        self.assertEqual(reply["match"]["source"], "library")
        self.assertEqual(
            [candidate["preset_name"] for candidate in reply["match"]["candidates"]],
            ["3DJAKE ABS (OFD)"],
        )
        self.assertIn("library match", reply["message"])
        self.assertEqual(self.selects, [])

    def test_library_lookup_uses_cache_without_network_preflight(self):
        calls = []

        def deny(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("network denied")

        class Library:
            def find(self, *args, **kwargs):
                return [{
                    "profile_name": "3DJAKE ABS (OFD)",
                    "brand": "3DJAKE",
                    "material": "ABS",
                    "filament": "ABS",
                    "path": "brands/3djake/materials/ABS/filaments/abs.json",
                    "score": 85,
                }]

        original_preflight = self.module._preflight_host
        original_library = self.module._ofd_library
        self.module._preflight_host = deny
        self.module._ofd_library = lambda *args, **kwargs: Library()
        try:
            found = self.module._library_candidates(
                self._observations()[0], {"ofd_enabled": True},
                self.storage, {}, [])
        finally:
            self.module._preflight_host = original_preflight
            self.module._ofd_library = original_library
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].preset_name, "3DJAKE ABS (OFD)")
        self.assertEqual(calls, [])

    def test_library_preset_preflight_only_when_not_cached(self):
        calls = []

        class Library:
            def __init__(self, cached):
                self.cached = cached

            def preset_is_cached(self, candidate):
                return self.cached

            def fetch(self, candidate):
                return {"name": "3DJAKE ABS (OFD)"}

        original_preflight = self.module._preflight_host
        self.module._preflight_host = lambda *args, **kwargs: calls.append(args)
        try:
            cached_library = Library(True)
            self.module._fetch_library_preset(
                cached_library, {"library": {"path": "x.json"}},
                {"ofd_index_url": "https://api.openfilamentdatabase.org/index.json"})
            self.assertEqual(calls, [])
            cold_library = Library(False)
            self.module._fetch_library_preset(
                cold_library, {"library": {"path": "x.json"}},
                {"ofd_index_url": "https://api.openfilamentdatabase.org/index.json"})
            self.assertEqual(len(calls), 1)
        finally:
            self.module._preflight_host = original_preflight

    def test_match_single_library_candidate_explains_apply(self):
        self.bundle = FakeBundle([
            FakePreset("Generic ABS @Test", vendor="Generic", material="ABS",
                       color="#000000"),
        ], ["Generic ABS @Test"])
        candidate_type = self.module._DEPS["Candidate"]
        original = self.module._library_candidates
        self.module._library_candidates = lambda *args, **kwargs: [
            candidate_type(
                preset_name="3DJAKE ABS (OFD)", score=85,
                reasons=["Open Filament Database"], source="library",
                preset=None, library_url="https://example.test/abs.json",
                library_meta={"brand": "3DJAKE", "filament": "ABS"},
            )
        ]
        try:
            reply = self.capability._run({
                "command": "match",
                "slot": 1,
                "observation": {"vendor": "3DJake", "material": "ABS",
                                "product": "ABS Pastel Pink", "spool_id": 6},
                "storage": self.storage,
                "settings": self._settings(ofd_enabled=True),
                "snapshot": self._snapshot(),
            })
        finally:
            self.module._library_candidates = original
        self.assertEqual(reply["match"]["source"], "library")
        self.assertEqual(reply["match"]["candidates"][0]["preset_name"],
                         "3DJAKE ABS (OFD)")
        self.assertIn("library match", reply["message"])
        self.assertIn("Press Apply", reply["message"])
        self.assertEqual(self.selects, [])

    def test_apply_all_installs_a_library_candidate(self):
        captured = {}

        def fake_install(candidate, snapshot, settings, storage, session,
                         warnings, price=""):
            captured.update(candidate)
            captured["price"] = price
            return "3DJAKE ABS (OFD)"

        original = self.module._install_library_preset
        self.module._install_library_preset = fake_install
        try:
            reply = self.capability._run({
                "command": "apply_all",
                "entries": [{
                    "slot": 1,
                    "preset_name": "3DJAKE ABS (OFD)",
                    "source": "library",
                    "candidate": {"preset_name": "3DJAKE ABS (OFD)"},
                    "remember": True,
                    "observation": {"vendor": "3DJake", "material": "ABS",
                                    "spool_id": 6},
                }],
                "storage": self.storage,
                "settings": self._settings(),
                "snapshot": self._snapshot(),
            })
        finally:
            self.module._install_library_preset = original
        self.assertEqual(captured["preset_name"], "3DJAKE ABS (OFD)")
        self.assertEqual(self.selects, [(1, "3DJAKE ABS (OFD)")])
        self.assertTrue(reply["applied_all"][0]["remembered"])
        self.assertTrue(reply["refresh_presets"])

    def test_scan_does_not_apply_an_empty_slot(self):
        observation = self.observation_type(
            slot=1, present=False, source="snapmaker")
        self.module._DEPS["read_printer"] = lambda *a, **k: ([observation], [])
        reply = self.capability._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertFalse(reply["scan"][0]["applied"])
        self.assertNotIn(1, [slot for slot, _name in self.selects])
        self.assertTrue(reply["message"].startswith("No filament feeds detected."))

    def test_scan_selects_snapmaker_snapspeed_from_name(self):
        self.bundle = FakeBundle([
            FakePreset("Snapmaker PLA Basic @U1", vendor="Snapmaker", material="PLA"),
            FakePreset("Snapmaker PLA Silk @U1", vendor="Snapmaker", material="PLA"),
            FakePreset("Snapmaker PLA SnapSpeed @U1", vendor="Snapmaker",
                       material="PLA", color="#E2DEDB"),
            FakePreset("Snapmaker PLA SnapSpeed @U1 0.2 nozzle", vendor="Snapmaker",
                       material="PLA", color="#E2DEDB"),
        ], ["Snapmaker PLA Basic @U1"],
            printer="Snapmaker U1 (0.4 nozzle)", print_host="10.0.0.176")
        observation = self.observation_type(
            slot=1, present=True, vendor="Snapmaker", material="PLA",
            subtype="SnapSpeed", color="#E2DEDB", source="snapmaker")
        self.module._DEPS["read_printer"] = lambda *a, **k: ([observation], [])
        reply = self.capability._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        row = reply["scan"][0]
        self.assertEqual(row["preset_name"], "Snapmaker PLA SnapSpeed @U1")
        self.assertEqual(row["source"], "installed")
        self.assertEqual(row["confidence"], 95)
        self.assertFalse(row["applied"])
        self.assertEqual(self.selects, [])

    def test_scan_uses_detected_printer_host(self):
        calls = []

        def reader(url, api_key="", timeout=0):
            calls.append(url)
            return self._observations(), []

        self.module._DEPS["read_printer"] = reader
        self.capability._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertEqual(calls, ["http://10.0.0.176"])

    def test_apply_uses_selector_label_for_local_bundle(self):
        snapshot = self._snapshot()
        reply = self.capability._run({
            "command": "apply",
            "slot": 1,
            "preset_name": "_local/filament_match/Custom PLA",
            "source": "installed",
            "remember": True,
            "observation": {"vendor": "Custom", "material": "PLA", "color": "#112233",
                            "spool_id": 9},
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": snapshot,
        })
        self.assertEqual(self.selects, [(1, "Custom PLA")])
        self.assertEqual(reply["applied"]["preset_name"],
                         "_local/filament_match/Custom PLA")
        self.assertEqual(reply["applied"]["select_name"], "Custom PLA")
        self.assertTrue(reply["applied"]["remembered"])

    def test_apply_then_restart_uses_history(self):
        self.capability._run({
            "command": "apply",
            "slot": 1,
            "preset_name": "Prusament PLA @Test",
            "source": "installed",
            "remember": True,
            "observation": {"vendor": "Prusament", "material": "PLA", "spool_id": 7},
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertEqual(self.selects, [(1, "Prusament PLA @Test")])
        saved = self._history()
        self.assertEqual(saved["spools"]["7"]["default"], "Prusament PLA @Test")
        self.assertEqual(saved["spools"]["7"]["printers"]["Snapmaker U1"],
                         "Prusament PLA @Test")

        # A fresh capability instance (as after an Orca restart) recovers the
        # confirmed preset on a scan. Explicit Match deliberately ignores
        # history so edited fields can produce a different candidate.
        history_obs = self.observation_type(
            slot=1, present=True, spool_id=7, source="snapmaker")
        self.module._DEPS["read_printer"] = lambda *a, **k: ([history_obs], [])
        restarted = self.module.FilamentMatch()
        restarted.window = FakeWindow()
        restarted._storage = self.storage
        reply = restarted._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        row = reply["scan"][0]
        self.assertEqual(row["preset_name"], "Prusament PLA @Test")
        self.assertEqual(row["source"], "history")
        self.assertEqual(row["confidence"], 100)

        reply = restarted._run({
            "command": "match",
            "slot": 1,
            "observation": {"vendor": "Prusament", "material": "PETG", "spool_id": 7},
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertEqual(reply["match"]["preset_name"], "Prusament PETG @Test")
        self.assertEqual(reply["match"]["source"], "installed")

    def test_spoolman_variant_extra_fills_missing_subtype(self):
        class Client:
            base_url = "https://spoolman.test"

            def get_spool(self, spool_id):
                return {"filament": {
                    "name": "Yellow",
                    "vendor": {"name": "Snapmaker"},
                    "material": "PLA",
                    "extra": {"variant": '"SnapSpeed"'},
                }}

        observation = self.observation_type(
            slot=1, present=True, vendor="Snapmaker", material="PLA",
            product="Yellow", spool_id=7)
        session = {
            "spoolman_client": Client(),
            "spoolman_url": "https://spoolman.test",
            "spoolman_cache": {},
        }
        warnings = []
        self.module._enrich_spoolman(
            [observation], {"spoolman_url": "https://spoolman.test"}, session, warnings)
        self.assertEqual(observation.subtype, "SnapSpeed")
        self.assertEqual(warnings, [])

    def test_printer_reported_subtype_wins_over_spoolman_variant(self):
        class Client:
            base_url = "https://spoolman.test"

            def get_spool(self, spool_id):
                return {"filament": {
                    "name": "Yellow", "material": "PLA",
                    "extra": {"variant": '"SnapSpeed"'},
                }}

        observation = self.observation_type(
            slot=1, present=True, vendor="Snapmaker", material="PLA",
            subtype="Silk", spool_id=7)
        session = {
            "spoolman_client": Client(),
            "spoolman_url": "https://spoolman.test",
            "spoolman_cache": {},
        }
        self.module._enrich_spoolman(
            [observation], {"spoolman_url": "https://spoolman.test"}, session, [])
        self.assertEqual(observation.subtype, "Silk")

    def test_preflight_permission_error_explains_retry(self):
        original_urlopen = self.module.urllib.request.urlopen

        def denied(*args, **kwargs):
            raise PermissionError("without permission: audit permission required")

        self.module.urllib.request.urlopen = denied
        self.module._PREFLIGHTED_HOSTS.discard("http://example.test:7125")
        try:
            with self.assertRaises(RuntimeError) as caught:
                self.original_preflight("http://example.test:7125")
            self.assertIn("http://example.test:7125",
                          self.module._PREFLIGHTED_HOSTS)
        finally:
            self.module.urllib.request.urlopen = original_urlopen
            self.module._PREFLIGHTED_HOSTS.discard("http://example.test:7125")
        self.assertIn("approve", str(caught.exception).casefold())

    def test_permission_denied_explains_retry(self):
        def denied(*args, **kwargs):
            raise PermissionError("[Errno 13] Permission denied")

        self.module._DEPS["read_printer"] = denied
        with self.assertRaises(RuntimeError) as caught:
            self.capability._run({
                "command": "scan",
                "storage": self.storage,
                "settings": self._settings(),
                "snapshot": self._snapshot(),
            })
        self.assertIn("approve", str(caught.exception).casefold())

    def test_permission_error_reported_in_warnings_explains_retry(self):
        self.module._DEPS["read_printer"] = (
            lambda *args, **kwargs: ([], ["moonraker: Permission denied by audit hook"]))
        with self.assertRaises(RuntimeError) as caught:
            self.capability._run({
                "command": "scan",
                "storage": self.storage,
                "settings": self._settings(),
                "snapshot": self._snapshot(),
            })
        self.assertIn("approve", str(caught.exception).casefold())

    # -- audit regressions: preset filesystem (PF-01..PF-10) -------------

    def test_library_install_uses_canonical_sanitized_name(self):
        snapshot = self._prepared_snapshot([
            FakePreset("Generic TPU @Test", vendor="Generic", material="TPU"),
        ], ["Generic TPU @Test"], printer="Snapmaker U1 (0.4 nozzle)")

        class Library:
            def preset_is_cached(self, candidate):
                return True

            def fetch(self, candidate):
                return {
                    "name": "AzureFilm TPU/TPE (OFD)",
                    "inherits": "Generic TPU @Test",
                    "from": "User",
                    "filament_type": ["TPU"],
                    "filament_vendor": ["AzureFilm"],
                }

        original = self.module._ofd_library
        self.module._ofd_library = lambda *args, **kwargs: Library()
        try:
            reply = self.module._apply_selection({
                "slot": 1,
                "preset_name": "AzureFilm TPU/TPE (OFD)",
                "source": "library",
                "candidate": {"preset_name": "AzureFilm TPU/TPE (OFD)"},
                "remember": True,
                "observation": {"vendor": "AzureFilm", "material": "TPU",
                                "spool_id": 21},
            }, snapshot, self._settings(ofd_enabled=True), self.storage, {}, [])
        finally:
            self.module._ofd_library = original

        canonical = "AzureFilm TPU_TPE (OFD)"
        self.assertEqual(self.selects, [(1, canonical)])
        self.assertEqual(reply["applied"]["select_name"], canonical)
        self.assertEqual(reply["applied"]["preset_name"],
                         "_local/filament_match/" + canonical)
        files = self._local_filament_files()
        self.assertEqual([path.stem for path in files], [canonical])
        installed = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(installed["name"], canonical)
        self.assertEqual(installed["filament_settings_id"], [canonical])
        saved = self._history()
        self.assertEqual(saved["spools"]["21"]["default"],
                         "_local/filament_match/" + canonical)

    def test_library_install_sanitizes_quotes(self):
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PETG @Test", vendor="Generic", material="PETG"),
        ], [], printer="Snapmaker U1 (0.4 nozzle)")
        preset = self.module._validated_library_preset({
            "name": 'CC3D "Matte" PETG (OFD)',
            "inherits": "Generic PETG @Test",
            "from": "User",
            "filament_type": ["PETG"],
            "filament_vendor": ["CC3D"],
        }, snapshot, [])
        written = self.module._write_local_preset(preset, snapshot)
        canonical = "CC3D _Matte_ PETG (OFD)"
        self.assertEqual(written, canonical)
        files = self._local_filament_files()
        self.assertEqual([path.stem for path in files], [canonical])
        installed = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(installed["name"], canonical)
        self.assertEqual(installed["filament_settings_id"], [canonical])

    def test_old_local_history_name_resolves_canonical_preset(self):
        canonical = "AzureFilm TPU_TPE (OFD)"
        preset = FakePreset(
            "_local/filament_match/" + canonical, vendor="AzureFilm",
            material="TPU", system=False, bundle_id="filament_match",
            label=canonical)
        self.bundle = FakeBundle([preset], ["_local/filament_match/" + canonical])
        snapshot = self._snapshot()
        history_cls = self.module._DEPS["HistoryStore"]
        observation = self.observation_type(
            slot=1, present=True, vendor="AzureFilm", material="TPU", spool_id=42)
        history = history_cls(str(Path(self.storage) / "history.json"))
        history.remember(observation, "Snapmaker U1",
                         "_local/filament_match/AzureFilm TPU/TPE (OFD)")
        self.module._DEPS["read_printer"] = lambda *a, **k: ([observation], [])
        reply = self.capability._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": snapshot,
        })
        row = reply["scan"][0]
        self.assertEqual(row["preset_name"], "_local/filament_match/" + canonical)
        self.assertEqual(row["source"], "history")

    # -- audit regressions: folder discovery (PF-02) ----------------------

    def test_discover_user_folder_prefers_active_bundle_folder(self):
        data = Path(self.storage) / "data"
        (data / "user" / "default" / "machine").mkdir(parents=True)
        active = data / "user" / "1a2b3c4d-1111-2222-3333-444455556666"
        (active / "machine").mkdir(parents=True)
        (active / "filament").mkdir(parents=True)
        (active / "_local" / "filament_match").mkdir(parents=True)
        folder, _why = self.module._discover_user_folder(str(data))
        self.assertEqual(folder, active.name)
        self.assertEqual(
            self.module._discover_user_folder(
                str(data), preferred="default")[0], "default")

    def test_discover_user_folder_allows_existing_default(self):
        data = Path(self.storage) / "data"
        (data / "user" / "default" / "filament").mkdir(parents=True)
        self.assertEqual(
            self.module._discover_user_folder(str(data))[0], "default")

    def test_system_only_profile_installs_into_existing_default_folder(self):
        data = Path(self.storage) / "default-data"
        (data / "user" / "default" / "machine").mkdir(parents=True)
        self.bundle = FakeBundle([
            FakePreset("Generic PLA @Test", vendor="Generic", material="PLA"),
        ], [], printer="Snapmaker U1 (0.4 nozzle)")
        original = self.module._data_dir_from_plugin
        self.module._data_dir_from_plugin = lambda: str(data)
        try:
            snapshot = self._snapshot()
            self.assertEqual(snapshot["folder"], "default")
            self.assertTrue(snapshot["folder_inferred"])
            written = self.module._write_local_preset({
                "name": "CC3D PLA (OFD)",
                "from": "User",
                "type": "filament",
                "instantiation": "true",
                "inherits": "Generic PLA @Test",
                "filament_type": ["PLA"],
                "filament_vendor": ["CC3D"],
            }, snapshot)
        finally:
            self.module._data_dir_from_plugin = original
        self.assertEqual(written, "CC3D PLA (OFD)")
        self.assertTrue(
            (data / "user" / "default" / "_local" / "filament_match" /
             "filament" / "CC3D PLA (OFD).json").exists())

    def test_snapshot_discovers_user_folder_for_system_only_profile(self):
        data = Path(self.storage) / "data"
        active = data / "user" / "1a2b3c4d-1111-2222-3333-444455556666"
        (active / "machine").mkdir(parents=True)
        self.bundle = FakeBundle([
            FakePreset("Generic PLA @Test", vendor="Generic", material="PLA"),
        ], [], printer="Snapmaker U1 (0.4 nozzle)", data_dir=str(data))
        snapshot = self._snapshot()
        self.assertEqual(snapshot["folder"], active.name)
        self.assertTrue(snapshot["folder_inferred"])

        written = self.module._write_local_preset({
            "name": "CC3D PLA (OFD)",
            "from": "User",
            "type": "filament",
            "instantiation": "true",
            "inherits": "Generic PLA @Test",
            "filament_type": ["PLA"],
            "filament_vendor": ["CC3D"],
        }, snapshot)
        self.assertEqual(written, "CC3D PLA (OFD)")
        target = (active / "_local" / "filament_match" / "filament" /
                  "CC3D PLA (OFD).json")
        self.assertTrue(target.exists())

    # -- audit regressions: flattening and price copies (PF-03/PF-04) ----

    def test_unreadable_user_preset_flattening_fails_loudly(self):
        preset_cls = self.module._DEPS["PresetInfo"]
        missing = Path(self.storage) / "missing.json"
        item = preset_cls(
            name="CC3D PETG (custom)", base="CC3D PETG", vendor="CC3D",
            material="PETG", subtype="", color="", filament_id="",
            compatible=True, system=False, user=True, bundle_id="",
            file=str(missing), rank=0, config={})
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PETG @Test", vendor="Generic", material="PETG"),
        ], [])
        with self.assertRaises(RuntimeError) as caught:
            self.module._flatten_user_preset(item, snapshot)
        self.assertIn("Could not read", str(caught.exception))
        self.assertIn("CC3D PETG (custom)", str(caught.exception))

    def test_cloud_user_preset_uses_live_config_values(self):
        preset_cls = self.module._DEPS["PresetInfo"]
        item = preset_cls(
            name="Cloud PETG", base="Cloud PETG", vendor="Cloud",
            material="PETG", subtype="", color="", filament_id="",
            compatible=True, system=False, user=True, bundle_id="",
            file="", rank=0,
            config={"values": {
                "inherits": "Generic PETG @Test",
                "filament_type": ["PETG"],
                "nozzle_temperature": ["245"],
            }})
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PETG @Test", vendor="Generic", material="PETG"),
        ], [])
        flat, root = self.module._flatten_user_preset(item, snapshot)
        self.assertEqual(root, "Generic PETG @Test")
        self.assertEqual(flat["nozzle_temperature"], ["245"])

    def test_price_copies_for_same_base_do_not_overwrite(self):
        first = FakePreset("eSUN PETG @U1 0.4 nozzle", vendor="eSUN",
                           material="PETG", color="#111111")
        second = FakePreset("eSUN PETG @A1 0.4 nozzle", vendor="eSUN",
                            material="PETG", color="#222222")
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PETG @Test", vendor="Generic", material="PETG"),
            first, second,
        ], ["eSUN PETG @U1 0.4 nozzle", "eSUN PETG @A1 0.4 nozzle"])
        name_a = self.module._create_price_copy(
            self._preset_info(snapshot, first.name), "10.00", snapshot, [])
        name_b = self.module._create_price_copy(
            self._preset_info(snapshot, second.name), "11.00", snapshot, [])
        self.assertNotEqual(name_a, name_b)
        files = {path.stem: json.loads(path.read_text(encoding="utf-8"))
                 for path in self._local_filament_files()}
        self.assertEqual(set(files), {name_a, name_b})
        self.assertEqual(files[name_a]["inherits"], first.name)
        self.assertEqual(files[name_b]["inherits"], second.name)

    def test_price_copies_in_one_apply_batch_get_distinct_names(self):
        first = FakePreset("eSUN PETG @U1 0.4 nozzle", vendor="eSUN",
                           material="PETG")
        second = FakePreset("eSUN PETG @A1 0.4 nozzle", vendor="eSUN",
                            material="PETG")
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PETG @Test", vendor="Generic", material="PETG"),
            first, second,
        ], ["eSUN PETG @U1 0.4 nozzle", "eSUN PETG @A1 0.4 nozzle"])
        reply = self.capability._run({
            "command": "apply_all",
            "entries": [
                {"slot": 1, "preset_name": first.name, "source": "installed",
                 "remember": False, "price": "10.00",
                 "observation": {"vendor": "eSUN", "material": "PETG"}},
                {"slot": 2, "preset_name": second.name, "source": "installed",
                 "remember": False, "price": "11.00",
                 "observation": {"vendor": "eSUN", "material": "PETG"}},
            ],
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": snapshot,
        })
        self.assertEqual(reply["apply_errors"], [])
        self.assertEqual(len(reply["applied_all"]), 2)
        names = {item["select_name"] for item in reply["applied_all"]}
        self.assertEqual(len(names), 2)
        self.assertEqual(len(self._local_filament_files()), 2)

    # -- audit regressions: local parent rules (PF-05) --------------------

    def test_library_parent_rejects_user_parent(self):
        user_parent = FakePreset("Generic PETG @User", vendor="Generic",
                                 material="PETG", system=False, user=True,
                                 compatible=True)
        system_parent = FakePreset("Generic PETG @System", vendor="Generic",
                                   material="PETG", compatible=True)
        snapshot = self._prepared_snapshot(
            [user_parent, system_parent], [], printer="Snapmaker U1 (0.4 nozzle)")
        parent = self.module._compatible_library_parent(
            "Generic PETG @User", snapshot)
        self.assertIsNotNone(parent)
        self.assertEqual(parent.name, "Generic PETG @System")

    def test_library_parent_compares_nozzles_numerically(self):
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PLA @System", vendor="Generic", material="PLA",
                       compatible=False),
            FakePreset("Generic PLA @U1 0.40 nozzle", vendor="Generic",
                       material="PLA", compatible=True),
        ], [], printer="Snapmaker U1 (0.4 nozzle)")
        parent = self.module._compatible_library_parent(
            "Generic PLA @System", snapshot)
        self.assertIsNotNone(parent)
        self.assertEqual(parent.name, "Generic PLA @U1 0.40 nozzle")

    # -- audit regressions: write/reload rollback (PF-06) -----------------

    def test_write_local_preset_rolls_back_on_reload_failure(self):
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PLA @Test", vendor="Generic", material="PLA"),
        ], [])
        calls = []

        def reload_local(bundle_id):
            calls.append(bundle_id)
            if len(calls) == 1:
                raise RuntimeError("reload failed")

        self.fake.host.preset.bundles.reload_local = reload_local
        with self.assertRaises(RuntimeError) as caught:
            self.module._write_local_preset({
                "name": "CC3D PLA (OFD)",
                "from": "User",
                "type": "filament",
                "instantiation": "true",
                "inherits": "Generic PLA @Test",
                "filament_type": ["PLA"],
            }, snapshot)
        self.assertIn("restored", str(caught.exception).casefold())
        self.assertEqual(self._local_filament_files(), [])
        self.assertEqual(len(calls), 2)

    def test_price_update_restores_file_on_reload_failure(self):
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PLA @Test", vendor="Generic", material="PLA"),
        ], [])
        path = (Path(self.storage) / "user" / "profile" / "_local" /
                "filament_match" / "filament" / "CC3D PLA (OFD).json")
        path.parent.mkdir(parents=True)
        original = {
            "name": "CC3D PLA (OFD)",
            "inherits": "Generic PLA @Test",
            "filament_cost": ["9.00"],
        }
        path.write_text(json.dumps(original), encoding="utf-8")
        info = FakePreset("_local/filament_match/CC3D PLA (OFD)",
                          vendor="CC3D", material="PLA", system=False,
                          bundle_id="filament_match", file=str(path),
                          label="CC3D PLA (OFD)")
        calls = []

        def reload_local(bundle_id):
            calls.append(bundle_id)
            if len(calls) == 1:
                raise RuntimeError("reload failed")

        self.fake.host.preset.bundles.reload_local = reload_local
        with self.assertRaises(RuntimeError) as caught:
            self.module._update_local_preset_price(info, "11.00", [])
        self.assertIn("restored", str(caught.exception).casefold())
        restored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(restored["filament_cost"], ["9.00"])
        self.assertEqual(len(calls), 2)

    def test_unknown_config_key_is_refused_before_write(self):
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PLA @Test", vendor="Generic", material="PLA"),
        ], [])
        snapshot["config_keys"] = ["filament_type", "filament_vendor"]
        snapshot["config_keys_complete"] = True
        with self.assertRaises(ValueError) as caught:
            self.module._validated_library_preset({
                "name": "CC3D PLA (OFD)",
                "from": "User",
                "inherits": "Generic PLA @Test",
                "filament_type": ["PLA"],
                "filament_vendor": ["CC3D"],
                "unknown_widget_option": ["1"],
            }, snapshot, [])
        self.assertIn("unknown_widget_option", str(caught.exception))

    def test_settings_save_preserves_unknown_keys_and_version(self):
        settings_path = Path(self.storage) / "settings.json"
        settings_path.write_text(json.dumps({
            "version": 7,
            "ofd_enabled": False,
            "spoolman_url": "http://spoolman.test",
            "future_option": {"keep": True},
        }), encoding="utf-8")
        loaded = self.module._load_settings(self.storage)
        self.assertFalse(loaded["ofd_enabled"])
        self.assertEqual(loaded["version"], 7)
        self.assertEqual(loaded["future_option"], {"keep": True})
        self.module._save_settings(self.storage, loaded)
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 7)
        self.assertEqual(saved["future_option"], {"keep": True})
        self.assertFalse(saved["ofd_enabled"])

        settings_path.write_text("{not json", encoding="utf-8")
        self.assertTrue(self.module._load_settings(self.storage)["ofd_enabled"])
        with self.assertRaises(RuntimeError):
            self.module._save_settings(
                self.storage, self.module._load_settings(self.storage))
        self.assertEqual(settings_path.read_text(encoding="utf-8"), "{not json")

    # -- audit regressions: filenames and JSON reads (PF-07/PF-08) --------

    def test_safe_filename_is_utf8_byte_safe(self):
        long_name = "é" * 100
        safe = self.module._safe_filename(long_name)
        self.assertLessEqual(len(safe.encode("utf-8")), 120)
        self.assertEqual(safe.encode("utf-8").decode("utf-8"), safe)
        first = self.module._safe_filename("A" * 130 + "B")
        second = self.module._safe_filename("A" * 130 + "C")
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first.encode("utf-8")), 120)

    def test_safe_filename_reserved_names_and_trailing_dots(self):
        self.assertEqual(self.module._safe_filename("Name. . "), "Name")
        for raw in ("CON", "PRN.json", "LPT1"):
            safe = self.module._safe_filename(raw)
            self.assertNotIn(
                safe.split(".", 1)[0].upper(),
                {"CON", "PRN", "LPT1"})

    def test_preset_file_json_accepts_bom(self):
        path = Path(self.storage) / "bom.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"name": "BOM preset"}).encode("utf-8"))
        self.assertEqual(
            self.module._preset_file_json(str(path))["name"], "BOM preset")

    def test_write_local_preset_refuses_to_overwrite_corrupt_json(self):
        snapshot = self._prepared_snapshot([
            FakePreset("Generic PLA @Test", vendor="Generic", material="PLA"),
        ], [])
        target = (Path(self.storage) / "user" / "profile" / "_local" /
                  "filament_match" / "filament" / "Corrupt.json")
        target.parent.mkdir(parents=True)
        target.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError) as caught:
            self.module._write_local_preset({
                "name": "Corrupt",
                "from": "User",
                "type": "filament",
                "instantiation": "true",
                "inherits": "Generic PLA @Test",
                "filament_type": ["PLA"],
            }, snapshot)
        self.assertIn("not valid JSON", str(caught.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "{not json")

    # -- audit regressions: permissions and host API (PF-10) --------------

    def test_permission_hints_cover_windows_and_macos(self):
        for text in ("[WinError 5] Access is denied",
                     "OSError: [Errno 1] Operation not permitted"):
            hint = self.module._permission_hint(text)
            self.assertIn("approve", hint.casefold())

    def test_snapshot_handles_preset_without_config_value(self):
        class BarePreset:
            name = "Bare PLA @Test"
            alias = ""
            file = ""
            bundle_id = ""
            is_default = False
            is_system = True
            is_user = False
            is_visible = True
            is_compatible = True

            def config_keys(self):
                return []

        self.bundle = FakeBundle([BarePreset()], ["Bare PLA @Test"])
        snapshot = self._snapshot()
        info = self._preset_info(snapshot, "Bare PLA @Test")
        self.assertEqual(info.config["inherits"], "")

    # -- audit regressions: UI backend (UI-08..UI-12) --------------------

    def test_scan_reports_feed_count_from_all_observations(self):
        observations = self._observations() + [
            self.observation_type(slot=3, present=False, source="snapmaker"),
            self.observation_type(slot=4, present=False, source="snapmaker"),
        ]
        self.module._DEPS["read_printer"] = lambda *a, **k: (observations, [])
        reply = self.capability._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertEqual(reply["feeders"], 4)
        self.assertTrue(reply["feeders_known"])
        self.assertEqual(reply["slot_mismatch"]["missing_slots"], 2)

    def test_unknown_feed_total_suppresses_mismatch(self):
        observations = [
            self.observation_type(slot=1, present=True, vendor="Prusament",
                                  material="PLA", source="moonraker"),
            self.observation_type(slot=2, present=True, vendor="Prusament",
                                  material="PETG", source="moonraker"),
        ]
        self.module._DEPS["read_printer"] = lambda *a, **k: (observations, [])
        self.bundle = FakeBundle([], ["Prusament PLA @Test"])
        reply = self.capability._run({
            "command": "scan",
            "storage": self.storage,
            "settings": self._settings(),
            "snapshot": self._snapshot(),
        })
        self.assertFalse(reply["feeders_known"])
        self.assertEqual(reply["slot_mismatch"]["message"], "")

    def test_connected_flag_is_only_a_hint(self):
        self.assertEqual(self.module._printer_problem({
            "host": "10.0.0.176",
            "selected": {"connected": False, "online": False},
        }), "")

    def test_enrich_spoolman_resets_cache_per_scan(self):
        class Client:
            base_url = "https://spoolman.test"
            calls = 0

            def get_spool(self, spool_id):
                self.calls += 1
                return {"price": 12.00, "initial_weight": 1000,
                        "filament": {"name": "PETG", "weight": 1000}}

        client = Client()
        session = {
            "spoolman_client": client,
            "spoolman_url": "https://spoolman.test",
            "spoolman_cache": {"9": ({"price": 1.00}, {})},
        }
        observation = self.observation_type(
            slot=1, present=True, vendor="eSUN", material="PETG", spool_id=9)
        self.module._enrich_spoolman(
            [observation], {"spoolman_url": "https://spoolman.test"}, session, [])
        self.assertEqual(client.calls, 1)
        self.assertEqual(session["spool_prices"]["9"], "12.00")

    def test_ofd_preflight_runs_once_per_scan(self):
        class Library:
            cache_path = str(Path(self.storage) / "missing-ofd-index.json")

            def __init__(self):
                self.calls = 0

            def find(self, *args, **kwargs):
                self.calls += 1
                return []

        library = Library()
        calls = []
        original = self.module._ofd_library
        self.module._ofd_library = lambda *a, **k: library
        self.module._preflight_host = lambda url: calls.append(url)
        session = {}
        try:
            observation = self._observations()[0]
            self.module._library_candidates(
                observation, self._settings(ofd_enabled=True), self.storage,
                session, [])
            self.module._library_candidates(
                observation, self._settings(ofd_enabled=True), self.storage,
                session, [])
        finally:
            self.module._ofd_library = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(library.calls, 1)

    def test_ofd_preflight_failure_cached_per_scan(self):
        class Library:
            cache_path = str(Path(self.storage) / "missing-ofd-index.json")

            def __init__(self):
                self.calls = 0

            def find(self, *args, **kwargs):
                self.calls += 1
                return []

        library = Library()
        calls = []
        original = self.module._ofd_library
        self.module._ofd_library = lambda *a, **k: library

        def deny(url):
            calls.append(url)
            raise RuntimeError("permission denied by audit")

        self.module._preflight_host = deny
        session = {}
        warnings = []
        try:
            observation = self._observations()[0]
            first = self.module._library_candidates(
                observation, self._settings(ofd_enabled=True), self.storage,
                session, warnings)
            second = self.module._library_candidates(
                observation, self._settings(ofd_enabled=True), self.storage,
                session, warnings)
        finally:
            self.module._ofd_library = original
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(library.calls, 0)
        self.assertEqual(len(warnings), 1)

    def test_log_error_survives_missing_stderr(self):
        original = sys.stderr
        try:
            sys.stderr = None
            self.module._log_error("no stream")
        finally:
            sys.stderr = original

    def test_apply_rejects_slot_beyond_project(self):
        with self.assertRaises(ValueError):
            self.capability._run({
                "command": "apply",
                "slot": 3,
                "preset_name": "Prusament PLA @Test",
                "source": "installed",
                "remember": False,
                "observation": {},
                "storage": self.storage,
                "settings": self._settings(),
                "snapshot": self._snapshot(),
            })
        self.assertEqual(self.selects, [])


@unittest.skipUnless(DIST.exists(), "single-file build not present")
class DistIntegrationSimulationTests(IntegrationSimulationTests):
    """The flattened build must behave the same as the development modules."""

    entry = DIST


if __name__ == "__main__":
    unittest.main()
