"""Tests for the standalone mock printer used during Filament Match development."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from filament_match_sources import read_printer  # noqa: E402

MOCK_PATH = PLUGIN_DIR / "dev" / "mock_printer.py"
_spec = importlib.util.spec_from_file_location("mock_printer", MOCK_PATH)
mock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock)


class MockPrinterTests(unittest.TestCase):
    def setUp(self):
        self.store = mock.Store(None)
        self.server = mock.MockPrinterServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post_state(self, state):
        request = urllib.request.Request(
            self.base + "/__state",
            data=json.dumps(state).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_default_state_is_read_by_both_sources(self):
        observations, errors = read_printer(self.base, timeout=3)
        self.assertEqual(errors, [])
        self.assertEqual(len(observations), 4)
        self.assertFalse(observations[0].present)
        self.assertTrue(observations[2].present)
        self.assertEqual(observations[2].vendor, "3DJake")
        self.assertEqual(observations[2].material, "ABS")
        self.assertEqual(observations[2].spool_id, 7)
        self.assertEqual(observations[3].subtype, "SnapSpeed")

    def test_four_spool_seed_matches_the_spoolman_fixtures(self):
        seed = (PLUGIN_DIR / "dev" / "mock_states" / "four_spools.json")
        saved = self._post_state(json.loads(seed.read_text(encoding="utf-8")))
        self.assertEqual(len(saved["feeders"]), 4)

        observations, errors = read_printer(self.base, timeout=3)
        self.assertEqual(errors, [])
        self.assertEqual(len(observations), 4)
        self.assertEqual(
            [(item.vendor, item.material, item.spool_id)
             for item in observations[:3]],
            [("3DJake", "ABS", 6), ("CC3D", "PETG", 8),
             ("eSUN", "PETG", 9)],
        )
        the_filament = observations[3]
        self.assertEqual(the_filament.vendor, "The Filament")
        self.assertEqual(the_filament.material, "PLA")
        self.assertEqual(the_filament.subtype, "Lite")
        self.assertEqual(the_filament.product, "PLA Lite White")
        self.assertEqual(the_filament.color, "#FFFFFF")
        self.assertIsNone(the_filament.spool_id)
        self.assertEqual(the_filament.bed_temp, 50)
        self.assertEqual(the_filament.nozzle_temp, 210)

    def test_state_can_change_feeder_count_and_filaments(self):
        state = {
            "mode": "both",
            "printer": "Mock",
            "feeders": [
                {"present": True, "vendor": "3DJake", "material": "ABS",
                 "subtype": "", "product": "ABS", "color": "#4DB6AC",
                 "spool_id": 7, "filament_id": "", "bed_temp": 100,
                 "nozzle_temp": 260},
                {"present": True, "vendor": "Prusament", "material": "PETG",
                 "subtype": "", "product": "Prusament PETG", "color": "#FF0000",
                 "spool_id": 12, "filament_id": "", "bed_temp": 80,
                 "nozzle_temp": 250},
                {"present": False, "vendor": "", "material": "", "subtype": "",
                 "product": "", "color": "", "spool_id": 0,
                 "filament_id": "", "bed_temp": 0, "nozzle_temp": 0},
            ],
        }
        saved = self._post_state(state)
        self.assertEqual(len(saved["feeders"]), 3)

        observations, errors = read_printer(self.base, timeout=3)
        self.assertEqual(errors, [])
        self.assertEqual(len(observations), 3)
        self.assertEqual([o.present for o in observations], [True, True, False])
        self.assertEqual(observations[0].product, "ABS")
        self.assertEqual(observations[1].vendor, "Prusament")
        self.assertEqual(observations[1].material, "PETG")
        self.assertEqual(observations[1].spool_id, 12)
        self.assertEqual(observations[1].bed_temp, 80)

    def test_moonraker_only_mode_still_works(self):
        state = mock.Store(None).get()
        state["mode"] = "moonraker"
        self._post_state(state)
        observations, errors = read_printer(self.base, timeout=3)
        self.assertTrue(observations)
        self.assertTrue(any("snapmaker" in error for error in errors))
        by_slot = {observation.slot: observation for observation in observations}
        self.assertEqual(by_slot[3].vendor, "3DJake")

    def test_health_and_state_endpoints(self):
        with urllib.request.urlopen(self.base + "/__health", timeout=3) as response:
            health = json.loads(response.read().decode("utf-8"))
        self.assertTrue(health["ok"])
        self.assertEqual(health["feeders"], 4)
        self.assertEqual(health["present"], 2)
        with urllib.request.urlopen(self.base + "/__state", timeout=3) as response:
            state = json.loads(response.read().decode("utf-8"))
        self.assertEqual(state["feeders"][2]["vendor"], "3DJake")


if __name__ == "__main__":
    unittest.main()
