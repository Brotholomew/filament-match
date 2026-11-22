"""Tests for filament_match_sources.

The tests are standard-library only and never touch the network.  If the core
module has not been written yet (it may be produced by another task), a tiny
test-only Observation shim is installed so these adapters can be exercised
against the interface in PLAN.md.  When the real module exists, the shim is not
used.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

try:  # pragma: no cover - the real module is used when present
    import filament_match_core  # noqa: F401
except ImportError:  # pragma: no cover - only while the core task is in flight
    _core_stub = types.ModuleType("filament_match_core")

    @dataclasses.dataclass
    class Observation:  # type: ignore[no-redef]
        slot: int
        present: bool = True
        vendor: str = ""
        material: str = ""
        subtype: str = ""
        product: str = ""
        color: str = ""
        filament_id: str = ""
        spool_id: int | None = None
        tag_uid: str = ""
        bed_temp: int | None = None
        nozzle_temp: int | None = None
        source: str = ""
        raw: dict = dataclasses.field(default_factory=dict)

    _core_stub.Observation = Observation
    sys.modules["filament_match_core"] = _core_stub

import filament_match_sources as sources  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_json(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


def load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class PrinterHTTP:
    """Fake urllib opener for read_printer tests."""

    def __init__(self, snapmaker=None, moonraker=None):
        self.snapmaker = snapmaker
        self.moonraker = moonraker
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, timeout=None, headers=None):
        self.calls.append((url, dict(headers or {})))
        if "print_task_config" in url:
            if self.snapmaker is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return self.snapmaker
        if "namespace=lane_data" in url:
            if self.moonraker is None:
                raise urllib.error.URLError("connection refused")
            return self.moonraker
        raise AssertionError(f"unexpected URL: {url}")


def mapping_opener(mapping):
    """Return an opener that serves values by URL suffix.

    A value may be bytes/str/dict/list, or an exception instance to raise.
    """

    def opener(url):
        for suffix, value in mapping.items():
            if url.endswith(suffix):
                if isinstance(value, BaseException):
                    raise value
                return value
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    return opener


def raising_opener(exception):
    def opener(url):
        raise exception

    return opener


class ValueHelperTests(unittest.TestCase):
    def test_normalize_base_url(self):
        self.assertEqual(
            sources.normalize_base_url(" 192.168.1.50:7125/ "),
            "http://192.168.1.50:7125",
        )
        self.assertEqual(
            sources.normalize_base_url("http://printer.local/"),
            "http://printer.local",
        )
        self.assertEqual(
            sources.normalize_base_url("https://spool.example/api/"),
            "https://spool.example/api",
        )
        self.assertEqual(sources.normalize_base_url("printer"), "http://printer")
        self.assertEqual(sources.normalize_base_url("http://"), "")
        self.assertEqual(sources.normalize_base_url(None), "")
        self.assertEqual(sources.normalize_base_url(""), "")

    def test_safe_str(self):
        self.assertEqual(sources.safe_str("  x "), "x")
        self.assertEqual(sources.safe_str(42), "42")
        self.assertEqual(sources.safe_str(3.5), "3.5")
        self.assertEqual(sources.safe_str(None), "")
        self.assertEqual(sources.safe_str(True), "")
        self.assertEqual(sources.safe_str(["x"]), "")
        self.assertEqual(sources.safe_str(None, "fallback"), "fallback")

    def test_safe_int(self):
        self.assertEqual(sources.safe_int("210"), 210)
        self.assertEqual(sources.safe_int(" 210.9 "), 210)
        self.assertEqual(sources.safe_int("60"), 60)
        self.assertEqual(sources.safe_int("abc"), None)
        self.assertEqual(sources.safe_int("#None"), None)
        self.assertEqual(sources.safe_int(""), None)
        self.assertEqual(sources.safe_int(None, 7), 7)
        self.assertEqual(sources.safe_int(True), 1)
        self.assertEqual(sources.safe_int(False), 0)
        self.assertEqual(sources.safe_int(3.7), 3)
        self.assertIsNone(sources.safe_int(float("nan")))
        self.assertIsNone(sources.safe_int(float("inf")))
        self.assertIsNone(sources.safe_int([1]))

    def test_normalize_color_hex_forms(self):
        cases = {
            "#ff0000": "#FF0000",
            "FF0000": "#FF0000",
            "f06292ff": "#F06292",
            "#F06292FF": "#F06292",
            "#12345678": "#123456",
            "#F00": "#FF0000",
            "F00": "#FF0000",
            "#F00A": "#FF0000",
            "0x112233": "#112233",
            "0X11223344": "#112233",
            "  #f06292ff  ": "#F06292",
            "#FFFFFFFF": "#FFFFFF",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sources.normalize_color(raw), expected)

    def test_normalize_color_sequences_and_integers(self):
        self.assertEqual(sources.normalize_color([255, 61, 0]), "#FF3D00")
        self.assertEqual(sources.normalize_color([255, 61, 0, 128]), "#FF3D00")
        self.assertEqual(sources.normalize_color(["255", "61", "0"]), "#FF3D00")
        self.assertEqual(sources.normalize_color("255, 61, 0"), "#FF3D00")
        self.assertEqual(sources.normalize_color("rgb(255, 61, 0)"), "#FF3D00")
        self.assertEqual(sources.normalize_color("[255, 61, 0, 128]"), "#FF3D00")
        self.assertEqual(sources.normalize_color(0xF06292), "#F06292")
        self.assertEqual(sources.normalize_color(0xF06292FF), "#F06292")
        self.assertEqual(sources.normalize_color(0xFFFFFFFF), "#FFFFFF")

    def test_normalize_color_garbage(self):
        for raw in (
            None,
            True,
            "",
            "garbage",
            "#None",
            "#12",
            "#12345",
            [300, 0, 0],
            [255, 0],
            -1,
        ):
            with self.subTest(raw=raw):
                self.assertEqual(sources.normalize_color(raw), "")


class SnapmakerTests(unittest.TestCase):
    def setUp(self):
        self.payload = load_json("snapmaker_response.json")

    def test_parses_four_slots(self):
        observations = sources._parse_snapmaker(self.payload)
        self.assertEqual([observation.slot for observation in observations], [1, 2, 3, 4])

        first = observations[0]
        self.assertTrue(first.present)
        self.assertEqual(first.source, "snapmaker")
        self.assertEqual(first.vendor, "Polymaker")
        self.assertEqual(first.material, "PLA")
        self.assertEqual(first.subtype, "SnapSpeed")
        self.assertEqual(first.product, "")
        self.assertEqual(first.color, "#F06292")
        self.assertEqual(first.spool_id, 42)
        self.assertEqual(first.bed_temp, 60)
        self.assertEqual(first.nozzle_temp, 230)

        second = observations[1]
        self.assertTrue(second.present)
        self.assertEqual(second.material, "PETG")
        self.assertEqual(second.subtype, "")
        self.assertEqual(second.vendor, "")
        self.assertEqual(second.color, "#FF3D00")
        self.assertIsNone(second.spool_id)
        self.assertEqual(second.bed_temp, 80)
        self.assertEqual(second.nozzle_temp, 250)

        third = observations[2]
        self.assertTrue(third.present)
        self.assertEqual(third.material, "PLA")
        self.assertEqual(third.subtype, "Matte")
        self.assertEqual(third.vendor, "Snapmaker")
        self.assertEqual(third.color, "")
        self.assertEqual(third.spool_id, 7)
        self.assertIsNone(third.bed_temp)
        self.assertIsNone(third.nozzle_temp)

        fourth = observations[3]
        self.assertFalse(fourth.present)
        self.assertEqual(fourth.material, "")
        self.assertEqual(fourth.vendor, "")
        self.assertEqual(fourth.color, "")
        self.assertIsNone(fourth.spool_id)
        self.assertIsNone(fourth.bed_temp)
        self.assertIsNone(fourth.nozzle_temp)

    def test_raw_contains_slot_data(self):
        first = sources._parse_snapmaker(self.payload)[0]
        self.assertEqual(first.raw["slot_index"], 0)
        self.assertEqual(first.raw["filament_type"], "PLA")
        self.assertEqual(first.raw["filament_detect"]["BED_TEMP"], 60)

    def test_missing_exist_infers_presence(self):
        payload = {
            "result": {
                "status": {
                    "print_task_config": {
                        "filament_type": ["PLA", "PETG"],
                        "filament_color_rgba": ["#FF0000", "#00FF00"],
                    }
                }
            }
        }
        observations = sources._parse_snapmaker(payload)
        self.assertEqual([observation.slot for observation in observations], [1, 2])
        self.assertTrue(all(observation.present for observation in observations))
        self.assertEqual(observations[1].material, "PETG")

    def test_mapping_arrays_and_false_slot(self):
        payload = {
            "result": {
                "status": {
                    "print_task_config": {
                        "filament_exist": {"T1": True, "T2": False},
                        "filament_type": {"T1": "PLA", "T2": "PETG"},
                        "filament_vendor": {"T1": "Acme", "T2": "Acme"},
                    }
                }
            }
        }
        observations = sources._parse_snapmaker(payload)
        self.assertEqual(len(observations), 2)
        self.assertTrue(observations[0].present)
        self.assertEqual(observations[0].vendor, "Acme")
        self.assertFalse(observations[1].present)
        self.assertEqual(observations[1].material, "")

    def test_scalar_exist_does_not_hide_other_slots(self):
        payload = {
            "result": {
                "status": {
                    "print_task_config": {
                        "filament_exist": True,
                        "filament_type": ["PLA", "PETG", "ABS"],
                    }
                }
            }
        }
        observations = sources._parse_snapmaker(payload)
        self.assertEqual(len(observations), 3)
        self.assertTrue(all(observation.present for observation in observations))

    def test_garbage_values_do_not_raise(self):
        payload = {
            "result": {
                "status": {
                    "print_task_config": {
                        "filament_exist": [1, 0],
                        "filament_type": ["PLA", None],
                        "filament_sub_type": [{"bad": 1}, "NONE"],
                        "filament_color_rgba": ["junk", "#00FF00"],
                        "filament_vendor": [{"bad": 1}, "Acme"],
                    },
                    "filament_detect": {"info": "not-a-list"},
                }
            }
        }
        observations = sources._parse_snapmaker(payload)
        self.assertEqual(len(observations), 2)
        self.assertTrue(observations[0].present)
        self.assertEqual(observations[0].color, "")
        self.assertEqual(observations[0].vendor, "")
        self.assertEqual(observations[0].subtype, "")
        self.assertFalse(observations[1].present)

    def test_empty_and_non_mapping_payloads(self):
        self.assertEqual(sources._parse_snapmaker({}), [])
        self.assertEqual(sources._parse_snapmaker([]), [])
        self.assertEqual(sources._parse_snapmaker(None), [])
        self.assertEqual(
            sources._parse_snapmaker(
                {"result": {"status": {"print_task_config": {"filament_exist": []}}}}
            ),
            [],
        )


class MoonrakerTests(unittest.TestCase):
    def setUp(self):
        self.payload = load_json("moonraker_lane_data.json")

    def test_parses_lanes_and_skips_empty(self):
        observations = sources._parse_moonraker(self.payload)
        self.assertEqual([observation.slot for observation in observations], [1, 2, 4, 5])

        first = observations[0]
        self.assertEqual(first.source, "moonraker")
        self.assertTrue(first.present)
        self.assertEqual(first.material, "PETG")
        self.assertEqual(first.vendor, "")
        self.assertEqual(first.color, "#00FF00")
        self.assertEqual(first.filament_id, "")
        self.assertIsNone(first.spool_id)
        self.assertEqual(first.bed_temp, 80)
        self.assertEqual(first.nozzle_temp, 250)

        second = observations[1]
        self.assertEqual(second.material, "PLA")
        self.assertEqual(second.vendor, "eSUN")
        self.assertEqual(second.color, "#0000FF")
        self.assertEqual(second.filament_id, "OFDSrzZ8")
        self.assertEqual(second.spool_id, 101)
        self.assertEqual(second.bed_temp, 60)
        self.assertEqual(second.nozzle_temp, 210)

        fourth = observations[2]
        self.assertEqual(fourth.slot, 4)
        self.assertEqual(fourth.material, "ABS")
        self.assertEqual(fourth.color, "#112233")
        self.assertEqual(fourth.filament_id, "GFABS")
        self.assertEqual(fourth.spool_id, 5)

        fifth = observations[3]
        self.assertEqual(fifth.slot, 5)
        self.assertEqual(fifth.material, "TPU")
        self.assertEqual(fifth.color, "#112233")

    def test_multiple_payload_shapes(self):
        list_payload = {
            "result": {
                "value": [
                    {"lane": "0", "material": "PLA", "color": "#FF0000"},
                    {"lane": "1", "material": "PETG", "color": "#00FF00"},
                ]
            }
        }
        observations = sources._parse_moonraker(list_payload)
        self.assertEqual([observation.slot for observation in observations], [1, 2])

        single_payload = {
            "result": {"value": {"lane": "0", "material": "PLA", "color": "#FF0000"}}
        }
        observations = sources._parse_moonraker(single_payload)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].slot, 1)

        top_level = {
            "lane1": {"material": "PLA", "color": "#FF0000"},
            "lane2": {"material": "PETG"},
        }
        observations = sources._parse_moonraker(top_level)
        self.assertEqual([observation.slot for observation in observations], [1, 2])
        self.assertEqual(observations[1].material, "PETG")

    def test_t_prefixed_lane_is_zero_based(self):
        payload = {"result": {"value": {"laneT": {"lane": "T1", "material": "PLA"}}}}
        observations = sources._parse_moonraker(payload)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].slot, 2)

    def test_numeric_filament_id_and_zero_spool_are_ignored(self):
        payload = {
            "result": {
                "value": {
                    "lane1": {
                        "lane": "0",
                        "material": "PLA",
                        "filament_id": 12345,
                        "spool_id": "0",
                    }
                }
            }
        }
        observations = sources._parse_moonraker(payload)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].filament_id, "")
        self.assertIsNone(observations[0].spool_id)

    def test_empty_and_non_mapping_payloads(self):
        self.assertEqual(sources._parse_moonraker({}), [])
        self.assertEqual(sources._parse_moonraker(None), [])
        self.assertEqual(sources._parse_moonraker([]), [])
        self.assertEqual(sources._parse_moonraker({"result": {"value": {}}}), [])
        self.assertEqual(
            sources._parse_moonraker(
                {"result": {"value": {"lane1": {"material": "", "color": "#None"}}}}
            ),
            [],
        )
        self.assertEqual(
            sources._parse_moonraker(
                {"result": {"value": {"lane1": {"material": "", "color": "#FF0000"}}}}
            ),
            [],
        )
        spool_only = sources._parse_moonraker(
            {"result": {"value": {"lane1": {"material": "", "spool_id": 12}}}}
        )
        self.assertEqual(len(spool_only), 1)
        self.assertEqual(spool_only[0].spool_id, 12)
        self.assertEqual(spool_only[0].material, "")


class MergeAndReadPrinterTests(unittest.TestCase):
    def test_merge_precedence_and_fill(self):
        primary = [
            sources.Observation(
                slot=1,
                present=True,
                vendor="Polymaker",
                material="PLA",
                color="#F06292",
                source="snapmaker",
            )
        ]
        secondary = [
            sources.Observation(
                slot=1,
                present=True,
                vendor="eSUN",
                material="PETG",
                color="#00FF00",
                filament_id="OFDSrzZ8",
                spool_id=101,
                bed_temp=80,
                source="moonraker",
                raw={"material": "PETG"},
            ),
            sources.Observation(
                slot=2,
                present=True,
                material="ABS",
                source="moonraker",
            ),
        ]
        merged = sources._merge_observations(primary, secondary)
        self.assertEqual([observation.slot for observation in merged], [1, 2])
        first = merged[0]
        self.assertEqual(first.material, "PLA")
        self.assertEqual(first.vendor, "Polymaker")
        self.assertEqual(first.color, "#F06292")
        self.assertEqual(first.filament_id, "OFDSrzZ8")
        self.assertEqual(first.spool_id, 101)
        self.assertEqual(first.bed_temp, 80)
        self.assertEqual(first.source, "snapmaker")
        self.assertEqual(first.raw["other_sources"]["moonraker"]["material"], "PETG")
        self.assertEqual(merged[1].source, "moonraker")

    def test_merge_switches_source_for_empty_primary_slot(self):
        primary = [
            sources.Observation(slot=4, present=False, source="snapmaker"),
        ]
        secondary = [
            sources.Observation(
                slot=4,
                present=True,
                material="ABS",
                color="#112233",
                spool_id=5,
                source="moonraker",
            )
        ]
        merged = sources._merge_observations(primary, secondary)
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0].present)
        self.assertEqual(merged[0].material, "ABS")
        self.assertEqual(merged[0].spool_id, 5)
        self.assertEqual(merged[0].source, "moonraker")

    def test_read_printer_merges_both_sources(self):
        snapmaker = load_bytes("snapmaker_response.json")
        moonraker = load_bytes("moonraker_lane_data.json")
        fake = PrinterHTTP(snapmaker, moonraker)
        with mock.patch.object(sources, "_open_url", fake):
            observations, errors = sources.read_printer(
                "printer.local:7125", api_key="secret"
            )

        self.assertEqual(errors, [])
        self.assertEqual([observation.slot for observation in observations], [1, 2, 3, 4, 5])

        first, second, third, fourth, fifth = observations
        self.assertEqual(first.source, "snapmaker")
        self.assertEqual(first.material, "PLA")
        self.assertEqual(first.vendor, "Polymaker")
        self.assertEqual(first.subtype, "SnapSpeed")
        self.assertEqual(first.color, "#F06292")
        self.assertEqual(first.spool_id, 42)
        self.assertEqual(first.bed_temp, 60)
        self.assertEqual(first.nozzle_temp, 230)

        self.assertEqual(second.source, "snapmaker")
        self.assertEqual(second.material, "PETG")
        self.assertEqual(second.vendor, "eSUN")
        self.assertEqual(second.filament_id, "OFDSrzZ8")
        self.assertEqual(second.spool_id, 101)

        self.assertEqual(third.source, "snapmaker")
        self.assertEqual(third.subtype, "Matte")
        self.assertEqual(third.color, "")
        self.assertEqual(third.spool_id, 7)

        self.assertEqual(fourth.source, "moonraker")
        self.assertTrue(fourth.present)
        self.assertEqual(fourth.material, "ABS")
        self.assertEqual(fourth.color, "#112233")
        self.assertEqual(fourth.spool_id, 5)

        self.assertEqual(fifth.source, "moonraker")
        self.assertEqual(fifth.material, "TPU")
        self.assertEqual(fifth.spool_id, 9)

        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(fake.calls[0][0].startswith("http://printer.local:7125/"))
        self.assertIn("print_task_config", fake.calls[0][0])
        self.assertIn("namespace=lane_data", fake.calls[1][0])
        self.assertEqual(fake.calls[0][1]["X-Api-Key"], "secret")
        self.assertEqual(fake.calls[1][1]["X-Api-Key"], "secret")

    def test_read_printer_without_api_key_sends_no_key(self):
        fake = PrinterHTTP(load_bytes("snapmaker_response.json"), None)
        with mock.patch.object(sources, "_open_url", fake):
            sources.read_printer("http://printer.local")
        self.assertNotIn("X-Api-Key", fake.calls[0][1])

    def test_read_printer_falls_back_to_moonraker(self):
        fake = PrinterHTTP(None, load_bytes("moonraker_lane_data.json"))
        with mock.patch.object(sources, "_open_url", fake):
            observations, errors = sources.read_printer("http://printer.local")
        self.assertEqual(len(observations), 4)
        self.assertEqual([observation.slot for observation in observations], [1, 2, 4, 5])
        self.assertEqual(len(errors), 1)
        self.assertIn("snapmaker", errors[0])

    def test_read_printer_falls_back_to_snapmaker(self):
        fake = PrinterHTTP(load_bytes("snapmaker_response.json"), None)
        with mock.patch.object(sources, "_open_url", fake):
            observations, errors = sources.read_printer("http://printer.local")
        self.assertEqual(len(observations), 4)
        self.assertEqual(len(errors), 1)
        self.assertIn("moonraker", errors[0])

    def test_read_printer_both_fail(self):
        fake = PrinterHTTP(None, None)
        with mock.patch.object(sources, "_open_url", fake):
            observations, errors = sources.read_printer("http://printer.local")
        self.assertEqual(observations, [])
        self.assertEqual(len(errors), 2)
        self.assertIn("snapmaker", errors[0])
        self.assertIn("moonraker", errors[1])

    def test_read_printer_non_json(self):
        fake = PrinterHTTP(b"<html>not json</html>", None)
        with mock.patch.object(sources, "_open_url", fake):
            observations, errors = sources.read_printer("http://printer.local")
        self.assertEqual(observations, [])
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("invalid JSON" in error for error in errors))

    def test_one_bad_source_does_not_discard_the_other(self):
        fake = PrinterHTTP(load_bytes("snapmaker_response.json"), b"<html>not json</html>")
        with mock.patch.object(sources, "_open_url", fake):
            observations, errors = sources.read_printer("http://printer.local")
        self.assertEqual(len(observations), 4)
        self.assertEqual(len(errors), 1)
        self.assertIn("moonraker", errors[0])
        self.assertTrue(any("invalid JSON" in error for error in errors))

        fake = PrinterHTTP(b"<html>not json</html>", load_bytes("moonraker_lane_data.json"))
        with mock.patch.object(sources, "_open_url", fake):
            observations, errors = sources.read_printer("http://printer.local")
        self.assertEqual(len(observations), 4)
        self.assertEqual(len(errors), 1)
        self.assertIn("snapmaker", errors[0])

    def test_read_printer_successful_but_empty(self):
        fake = PrinterHTTP(b"{}", b"{}")
        with mock.patch.object(sources, "_open_url", fake):
            observations, errors = sources.read_printer("http://printer.local")
        self.assertEqual(observations, [])
        self.assertEqual(errors, ["no filament slots reported by Snapmaker or Moonraker"])

    def test_read_printer_without_url(self):
        observations, errors = sources.read_printer("")
        self.assertEqual(observations, [])
        self.assertEqual(errors, ["no printer URL configured"])


class SpoolmanTests(unittest.TestCase):
    def setUp(self):
        self.spool = load_json("spoolman_spool.json")
        self.filament = load_json("spoolman_filament.json")
        self.filaments = load_json("spoolman_filaments.json")

    def make_client(self, mapping=None):
        mapping = mapping or {
            "/api/v1/spool/42": self.spool,
            "/api/v1/filament/7": self.filament,
            "/api/v1/filament": self.filaments,
        }
        return sources.SpoolmanClient(
            "http://spoolman:8000/",
            opener=mapping_opener(mapping),
        )

    def test_get_spool_and_filament(self):
        client = self.make_client()
        spool = client.get_spool(42)
        self.assertEqual(spool["id"], 42)
        self.assertEqual(spool["filament"]["name"], "PolyLite PLA")
        filament = client.get_filament(7)
        self.assertEqual(filament["id"], 7)
        self.assertEqual(filament["vendor"]["name"], "Polymaker")

    def test_extra_fields_are_decoded(self):
        client = self.make_client()
        spool = client.get_spool(42)
        extras = client.extras(spool)
        self.assertEqual(extras["orca_filament_preset"], "Polymaker PolyLite PLA @U1")
        self.assertEqual(extras["orca_preset"], "CC3D PETG @U1")
        self.assertEqual(extras["density"], 1.24)
        self.assertIsNone(extras["empty_spool_weight"])
        self.assertEqual(extras["card_uids"], ["04A7B2C3D4E5F6"])
        self.assertTrue(extras["flag"])
        self.assertEqual(extras["notes"], "not json")
        self.assertEqual(extras["nested"], {"value": 3})
        self.assertEqual(extras["variant"], "Matte")
        self.assertEqual(client.extra_value(spool, "density"), 1.24)
        self.assertEqual(client.extra_value(spool, "missing", "fallback"), "fallback")

    def test_preset_hint(self):
        client = self.make_client()
        self.assertEqual(
            client.preset_hint(client.get_spool(42)),
            "Polymaker PolyLite PLA @U1",
        )
        self.assertEqual(
            client.preset_hint(client.get_filament(7)),
            "Polymaker PolyLite PLA @U1",
        )

    def test_decode_extra_shapes(self):
        self.assertEqual(
            sources.SpoolmanClient.decode_extra({"x": {"value": '"y"'}}),
            {"x": "y"},
        )
        self.assertEqual(
            sources.SpoolmanClient.decode_extra([{"name": "x", "value": '"y"'}]),
            {"x": "y"},
        )
        self.assertEqual(
            sources.SpoolmanClient.decode_extra('{"a": "\\"b\\""}'),
            {"a": "b"},
        )
        self.assertEqual(sources.SpoolmanClient.decode_extra("not json"), {})
        self.assertEqual(sources.SpoolmanClient.decode_extra(None), {})
        self.assertEqual(sources.SpoolmanClient.decode_extra(""), {})

    def test_find_filament_by_extra(self):
        client = self.make_client()
        found = client.find_filament_by_extra("orca_preset", "CC3D PETG @U1")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], 8)
        self.assertIsNone(client.find_filament_by_extra("orca_preset", "missing"))

    def test_find_filament_by_extra_wrapped_list(self):
        client = sources.SpoolmanClient(
            "http://spoolman:8000",
            opener=mapping_opener({"/api/v1/filament": {"filaments": self.filaments}}),
        )
        found = client.find_filament_by_extra("orca_preset", "CC3D PETG @U1")
        self.assertEqual(found["id"], 8)

    def test_invalid_ids(self):
        client = self.make_client()
        with self.assertRaises(sources.SourceError):
            client.get_spool(None)
        with self.assertRaises(sources.SourceError):
            client.get_filament("")
        with self.assertRaises(sources.SourceError):
            client.find_filament_by_extra("", "x")

    def test_no_base_url(self):
        client = sources.SpoolmanClient("", opener=lambda url: self.spool)
        with self.assertRaises(sources.SourceError) as context:
            client.get_spool(42)
        self.assertIn("not configured", str(context.exception))

    def test_http_404_is_source_error(self):
        client = sources.SpoolmanClient(
            "http://spoolman:8000",
            opener=mapping_opener({}),
        )
        with self.assertRaises(sources.SourceError) as context:
            client.get_spool(999)
        self.assertIn("404", str(context.exception))

    def test_network_error_is_source_error(self):
        client = sources.SpoolmanClient(
            "http://spoolman:8000",
            opener=raising_opener(urllib.error.URLError("connection refused")),
        )
        with self.assertRaises(sources.SourceError) as context:
            client.get_spool(42)
        self.assertIn("connection refused", str(context.exception))

    def test_timeout_is_source_error(self):
        client = sources.SpoolmanClient(
            "http://spoolman:8000",
            opener=raising_opener(TimeoutError("timed out")),
        )
        with self.assertRaises(sources.SourceError) as context:
            client.get_filament(7)
        self.assertIn("timed out", str(context.exception))

    def test_non_json_is_source_error(self):
        client = sources.SpoolmanClient(
            "http://spoolman:8000",
            opener=lambda url: b"<html>not json</html>",
        )
        with self.assertRaises(sources.SourceError) as context:
            client.get_spool(42)
        self.assertIn("invalid JSON", str(context.exception))

    def test_non_object_response_is_source_error(self):
        client = sources.SpoolmanClient(
            "http://spoolman:8000",
            opener=lambda url: [],
        )
        with self.assertRaises(sources.SourceError):
            client.get_spool(42)


class OfdTests(unittest.TestCase):
    def setUp(self):
        self.index_bytes = load_bytes("ofd_index.json")
        self.preset_bytes = load_bytes("ofd_preset.json")
        self.index = load_json("ofd_index.json")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache_path = os.path.join(self.temp_dir.name, "nested", "ofd_index.json")
        self.calls: list[str] = []

    def opener(self, url):
        self.calls.append(url)
        if url.endswith("/index.json"):
            return self.index_bytes
        if url.endswith("polylite_pla.json") or url.endswith("polylite_pla_matte.json"):
            return self.preset_bytes
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def make_library(self, cache_path=None, opener=None):
        return sources.OfdLibrary(
            cache_path=self.cache_path if cache_path is None else cache_path,
            opener=self.opener if opener is None else opener,
        )

    def test_find_returns_candidates_and_filters(self):
        library = self.make_library()
        candidates = library.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertEqual(len(candidates), 2)
        first = candidates[0]
        self.assertEqual(first["brand"], "Polymaker")
        self.assertEqual(first["filament"], "PolyLite PLA")
        self.assertEqual(first["profile_name"], "Polymaker PolyLite PLA (OFD)")
        self.assertEqual(
            first["path"],
            "brands/polymaker/materials/PLA/filaments/polylite_pla.json",
        )
        self.assertEqual(
            first["source"],
            "https://api.openfilamentdatabase.org/api/v1/brands/polymaker/materials/PLA/filaments/polylite_pla/index.json",
        )
        for key in ("brand", "filament", "profile_name", "path", "source"):
            self.assertIn(key, first)
        self.assertGreater(first["score"], candidates[1]["score"])
        self.assertLessEqual(first["score"], 100)

        with_subtype = library.find("Polymaker", "PLA", "PolyLite PLA", "Matte")
        self.assertEqual(len(with_subtype), 1)
        self.assertTrue(with_subtype[0]["path"].endswith("polylite_pla_matte.json"))

        unknown_subtype = library.find("Polymaker", "PLA", "PolyLite PLA", "Silk")
        self.assertEqual(len(unknown_subtype), 2)
        self.assertTrue(unknown_subtype[0]["path"].endswith("polylite_pla.json"))

        self.assertEqual(library.find("Polymaker", "PETG", "", ""), [])
        self.assertEqual(library.find("Generic", "PLA", "", ""), [])
        self.assertEqual(library.find("", "", "", ""), [])
        self.assertEqual(len(library.find("Polymaker", "PLA+", "PolyLite PLA")), 2)
        self.assertEqual(library.find("Polymaker", "PLA", "PolyLite PLA", limit=1)[0]["filament"], "PolyLite PLA")
        self.assertEqual(library.find("Polymaker", "PLA", "PolyLite PLA", limit=0), [])

    def test_find_case_insensitive_and_other_brand(self):
        library = self.make_library()
        candidates = library.find("polymaker", "pla", "polylite pla")
        self.assertEqual(candidates[0]["brand"], "Polymaker")
        esun = library.find("eSUN", "PETG", "PETG Basic")
        self.assertEqual(len(esun), 1)
        self.assertEqual(esun[0]["brand"], "eSUN")
        self.assertTrue(esun[0]["path"].endswith("petg_basic.json"))

    def test_fetch_one_preset_only(self):
        library = self.make_library()
        candidates = library.find("Polymaker", "PLA", "PolyLite PLA")
        index_calls = [url for url in self.calls if url.endswith("/index.json")]
        self.assertEqual(len(index_calls), 1)

        self.calls.clear()
        preset = library.fetch(candidates[0])
        self.assertEqual(preset["name"], "Polymaker PolyLite PLA (OFD)")
        self.assertEqual(preset["from"], "User")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0].endswith("polylite_pla.json"))

        # A second fetch is served from the in-memory preset cache.
        library.fetch(candidates[0])
        self.assertEqual(len(self.calls), 1)

        # Repeated finds use the in-memory index and never re-fetch.
        library.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertEqual(len(self.calls), 1)

    def test_fetch_accepts_path_string(self):
        library = self.make_library()
        library.find("Polymaker", "PLA", "PolyLite PLA")
        preset = library.fetch(
            "brands/polymaker/materials/PLA/filaments/polylite_pla.json"
        )
        self.assertEqual(preset["name"], "Polymaker PolyLite PLA (OFD)")

    def test_plain_local_index_path_without_an_opener(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "index.json").write_bytes(self.index_bytes)
            relative_preset = Path(
                "brands/polymaker/materials/PLA/filaments/polylite_pla.json"
            )
            preset_path = root / relative_preset
            preset_path.parent.mkdir(parents=True)
            preset_path.write_bytes(self.preset_bytes)

            library = sources.OfdLibrary(
                index_url=str(root / "index.json"),
                cache_path=None,
            )
            candidates = library.find("Polymaker", "PLA", "PolyLite PLA")
            self.assertGreaterEqual(len(candidates), 1)
            preset = library.fetch(candidates[0])
            self.assertEqual(preset["name"], "Polymaker PolyLite PLA (OFD)")

    def test_fetch_uses_the_index_directory_for_custom_urls(self):
        urls = []

        def opener(url):
            urls.append(url)
            if url.endswith("/my/ofd/index.json"):
                return self.index_bytes
            if url.endswith("polylite_pla.json"):
                return self.preset_bytes
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        library = sources.OfdLibrary(
            index_url="https://example.test/my/ofd/index.json",
            cache_path=None,
            opener=opener,
        )
        candidates = library.find("Polymaker", "PLA", "PolyLite PLA")
        library.fetch(candidates[0])
        self.assertTrue(
            urls[-1].startswith(
                "https://example.test/my/ofd/brands/polymaker/materials/PLA/filaments/"
            )
        )

    def test_fetch_rejects_bad_paths(self):
        library = self.make_library()
        library.find("Polymaker", "PLA", "PolyLite PLA")
        self.calls.clear()
        for path in (
            "",
            "/etc/passwd",
            "../secret.json",
            "a/../../secret.json",
            "a//b.json",
            "https://evil.example/preset.json",
        ):
            with self.subTest(path=path):
                with self.assertRaises(sources.SourceError):
                    library.fetch({"path": path})
        self.assertEqual(self.calls, [])

    def test_fetch_errors(self):
        def bad_preset(url):
            if url.endswith("/index.json"):
                return self.index_bytes
            return b"<html>not json</html>"

        library = sources.OfdLibrary(cache_path=None, opener=bad_preset)
        candidates = library.find("Polymaker", "PLA", "PolyLite PLA")
        with self.assertRaises(sources.SourceError) as context:
            library.fetch(candidates[0])
        self.assertIn("invalid JSON", str(context.exception))

        def not_object(url):
            if url.endswith("/index.json"):
                return self.index_bytes
            return []

        library = sources.OfdLibrary(cache_path=None, opener=not_object)
        candidates = library.find("Polymaker", "PLA", "PolyLite PLA")
        with self.assertRaises(sources.SourceError) as context:
            library.fetch(candidates[0])
        self.assertIn("not a JSON object", str(context.exception))

        def missing_preset(url):
            if url.endswith("/index.json"):
                return self.index_bytes
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        library = sources.OfdLibrary(cache_path=None, opener=missing_preset)
        candidates = library.find("Polymaker", "PLA", "PolyLite PLA")
        with self.assertRaises(sources.SourceError) as context:
            library.fetch(candidates[0])
        self.assertIn("404", str(context.exception))

    def test_cache_is_written_atomically_and_reused(self):
        library = self.make_library()
        candidates = library.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertTrue(os.path.exists(self.cache_path))
        self.assertEqual(
            json.loads(Path(self.cache_path).read_text(encoding="utf-8"))["profiles"],
            self.index["profiles"],
        )
        leftovers = list(Path(self.cache_path).parent.glob(".ofd-index-*.tmp"))
        self.assertEqual(leftovers, [])

        offline_calls = []

        def offline(url):
            offline_calls.append(url)
            raise urllib.error.URLError("offline")

        second = sources.OfdLibrary(cache_path=self.cache_path, opener=offline)
        second_candidates = second.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertEqual(
            [candidate["path"] for candidate in second_candidates],
            [candidate["path"] for candidate in candidates],
        )
        self.assertEqual(offline_calls, [])
        self.assertEqual(second.last_error, "")

    def test_preset_json_is_cached_and_reused_offline(self):
        preset_dir = os.path.join(self.temp_dir.name, "presets")
        library = sources.OfdLibrary(
            cache_path=self.cache_path,
            preset_cache_dir=preset_dir,
            opener=self.opener,
        )
        candidate = library.find("Polymaker", "PLA", "PolyLite PLA")[0]
        self.assertFalse(library.preset_is_cached(candidate))
        fetched = library.fetch(candidate)
        self.assertTrue(library.preset_is_cached(candidate))
        cached_files = list(Path(preset_dir).rglob("*.json"))
        self.assertEqual(len(cached_files), 1)
        self.assertEqual(json.loads(cached_files[0].read_text(encoding="utf-8")),
                         fetched)

        offline_calls = []

        def offline(url):
            offline_calls.append(url)
            raise urllib.error.URLError("offline")

        second = sources.OfdLibrary(
            cache_path=self.cache_path,
            preset_cache_dir=preset_dir,
            opener=offline,
        )
        second.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertTrue(second.preset_is_cached(candidate))
        self.assertEqual(second.fetch(candidate), fetched)
        self.assertEqual(offline_calls, [])

    def test_stale_cache_falls_back_when_offline(self):
        library = self.make_library()
        library.find("Polymaker", "PLA", "PolyLite PLA")
        old = time.time() - (sources.DEFAULT_OFD_CACHE_TTL * 2)
        os.utime(self.cache_path, (old, old))

        def offline(url):
            raise urllib.error.URLError("offline")

        stale = sources.OfdLibrary(cache_path=self.cache_path, opener=offline)
        candidates = stale.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertEqual(len(candidates), 2)
        self.assertIn("offline", stale.last_error)

    def test_no_cache_and_offline_raises(self):
        def offline(url):
            raise urllib.error.URLError("offline")

        library = sources.OfdLibrary(cache_path=None, opener=offline)
        with self.assertRaises(sources.SourceError) as context:
            library.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertIn("offline", str(context.exception))
        self.assertIn("offline", library.last_error)

    def test_refresh_falls_back_to_cache_when_offline(self):
        library = self.make_library()
        library.find("Polymaker", "PLA", "PolyLite PLA")

        def offline(url):
            raise urllib.error.URLError("offline")

        refreshed = sources.OfdLibrary(cache_path=self.cache_path, opener=offline)
        index = refreshed.refresh()
        self.assertEqual(len(index["profiles"]), 4)
        self.assertIn("offline", refreshed.last_error)

    def test_bad_index_does_not_poison_cache(self):
        library = sources.OfdLibrary(
            cache_path=self.cache_path,
            opener=lambda url: b"<html>not json</html>",
        )
        with self.assertRaises(sources.SourceError):
            library.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertFalse(os.path.exists(self.cache_path))

        malformed = sources.OfdLibrary(
            cache_path=self.cache_path,
            opener=lambda url: {"profiles": "not a list"},
        )
        with self.assertRaises(sources.SourceError):
            malformed.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertFalse(os.path.exists(self.cache_path))

    def test_list_index_is_accepted(self):
        library = sources.OfdLibrary(
            cache_path=None,
            opener=lambda url: self.index["profiles"],
        )
        candidates = library.find("Prusament", "PLA", "PLA Galaxy Black")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["brand"], "Prusament")

    def test_validate_preset(self):
        valid = load_json("ofd_preset.json")
        self.assertEqual(sources.OfdLibrary.validate_preset(valid), [])
        self.assertIn(
            "missing or empty name",
            sources.OfdLibrary.validate_preset({**valid, "name": ""}),
        )
        self.assertIn(
            "system presets cannot be installed",
            sources.OfdLibrary.validate_preset({**valid, "from": "System"}),
        )
        self.assertIn(
            "missing inherits",
            sources.OfdLibrary.validate_preset({k: v for k, v in valid.items() if k != "inherits"}),
        )
        self.assertIn(
            "system-only key present: filament_id",
            sources.OfdLibrary.validate_preset({**valid, "filament_id": "OFDSrzZ8"}),
        )
        self.assertTrue(sources.OfdLibrary.validate_preset([]))

    def test_cache_ttl_zero_always_refreshes(self):
        calls = []

        def opener(url):
            calls.append(url)
            return self.index_bytes

        library = sources.OfdLibrary(
            cache_path=self.cache_path,
            cache_ttl=0,
            opener=opener,
        )
        library.find("Polymaker", "PLA", "PolyLite PLA")
        library.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertEqual(len(calls), 1)  # in-memory index still wins in one process

        second = sources.OfdLibrary(
            cache_path=self.cache_path,
            cache_ttl=0,
            opener=opener,
        )
        second.find("Polymaker", "PLA", "PolyLite PLA")
        self.assertEqual(len(calls), 2)  # cache is never considered fresh


if __name__ == "__main__":
    unittest.main()
