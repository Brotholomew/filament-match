"""Unit tests for filament_match_core (stdlib unittest only)."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import filament_match_core as core  # noqa: E402


def make_preset(name: str, **kwargs) -> core.PresetInfo:
    return core.PresetInfo(
        name=name,
        base=kwargs.pop("base", ""),
        vendor=kwargs.pop("vendor", ""),
        material=kwargs.pop("material", ""),
        subtype=kwargs.pop("subtype", ""),
        color=kwargs.pop("color", ""),
        filament_id=kwargs.pop("filament_id", ""),
        compatible=kwargs.pop("compatible", True),
        system=kwargs.pop("system", True),
        user=kwargs.pop("user", False),
        bundle_id=kwargs.pop("bundle_id", ""),
        file=kwargs.pop("file", ""),
        rank=kwargs.pop("rank", 2),
        config=kwargs.pop("config", {}),
    )


def make_obs(**kwargs) -> core.Observation:
    return core.Observation(slot=kwargs.pop("slot", 1), **kwargs)


class IdentityAndNormalizationTests(unittest.TestCase):
    def test_identity_precedence_spool_then_tag_then_filament(self):
        obs = make_obs(
            vendor="CC3D",
            material="PETG",
            product="PETG",
            color="#F06292",
            spool_id=42,
            tag_uid="aa:bb:cc",
        )
        self.assertEqual(core.identity_key(obs), "spool:42")

        obs.spool_id = None
        self.assertEqual(core.identity_key(obs), "tag:AABBCC")

        obs.tag_uid = ""
        key = core.identity_key(obs)
        self.assertTrue(key.startswith("filament:"))
        payload = "cc3d|petg||petg|f06292"
        expected = "filament:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()
        self.assertEqual(key, expected)

    def test_spool_zero_is_still_an_identity(self):
        obs = make_obs(spool_id=0, tag_uid="AABB")
        self.assertEqual(core.identity_key(obs), "spool:0")

    def test_tag_normalization_makes_uid_formats_equal(self):
        a = make_obs(tag_uid="04:A7:1B")
        b = make_obs(tag_uid="04-a7-1b")
        self.assertEqual(core.identity_key(a), "tag:04A71B")
        self.assertEqual(core.identity_key(a), core.identity_key(b))

    def test_filament_identity_normalizes_case_punctuation_and_colour(self):
        a = make_obs(
            vendor="CC3D",
            material="PETG",
            subtype="Basic",
            product="PETG-Basic",
            color="#F06292",
        )
        b = make_obs(
            vendor="cc3d",
            material="petg",
            subtype="basic",
            product="PETG Basic",
            color="#f06292ff",
        )
        self.assertEqual(core.identity_key(a), core.identity_key(b))

        different_colour = make_obs(
            vendor="CC3D",
            material="PETG",
            subtype="Basic",
            product="PETG-Basic",
            color="#111111",
        )
        self.assertNotEqual(core.identity_key(a), core.identity_key(different_colour))

    def test_normalization_helpers(self):
        self.assertEqual(core.normalize_text("PLA+ & PETG"), "pla plus and petg")
        self.assertEqual(core.normalize_material("PET-G"), "petg")
        self.assertEqual(
            core.strip_printer_suffix("CC3D PETG @U1 0.4"), "CC3D PETG"
        )
        self.assertEqual(core.strip_bundle_prefix("[vendor] PLA"), "PLA")
        self.assertEqual(core.strip_color_words("Galaxy Black PLA"), "galaxy pla")
        self.assertEqual(core.normalize_color("#F06292FF"), "#f06292")
        self.assertEqual(core.normalize_color("bad"), "")
        self.assertEqual(core.color_distance("#000000", "#000000"), 0)
        self.assertGreater(core.color_distance("#000000", "#ffffff"), 400)


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "storage" / "history.json"

    def _obs(self, **kwargs):
        base = dict(slot=1, vendor="CC3D", material="PETG", product="PETG-Basic")
        base.update(kwargs)
        return make_obs(**base)

    def test_missing_file_is_empty_and_not_created_by_read(self):
        store = core.HistoryStore(self.path)
        self.assertEqual(store.get(self._obs(), "U1"), "")
        self.assertFalse(self.path.exists())
        self.assertEqual(store.load_error, "")

    def test_remember_get_default_printer_and_reload(self):
        store = core.HistoryStore(self.path)
        obs = self._obs(spool_id=42)
        key = store.remember(obs, "Snapmaker U1 (0.4 nozzle)", "CC3D PETG @U1 0.4")

        self.assertEqual(key, "spool:42")
        self.assertEqual(
            store.get(obs, "Snapmaker U1 (0.4 nozzle)"), "CC3D PETG @U1 0.4"
        )
        self.assertEqual(store.get(obs, "Other printer"), "CC3D PETG @U1 0.4")
        self.assertTrue(self.path.exists())

        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["spools"]["42"]["default"], "CC3D PETG @U1 0.4")
        self.assertEqual(
            document["spools"]["42"]["printers"]["Snapmaker U1 (0.4 nozzle)"],
            "CC3D PETG @U1 0.4",
        )
        self.assertIsInstance(document["spools"]["42"]["updated"], int)

        reloaded = core.HistoryStore(self.path)
        self.assertEqual(reloaded.get(obs, "Snapmaker U1 (0.4 nozzle)"), "CC3D PETG @U1 0.4")

        leftovers = list(self.path.parent.glob(".*.tmp"))
        self.assertEqual(leftovers, [])

    def test_printer_specific_mapping_wins_over_default(self):
        store = core.HistoryStore(self.path)
        obs = self._obs(spool_id=7)
        store.remember(obs, "Printer A", "Preset A")
        store.remember(obs, "Printer B", "Preset B")

        self.assertEqual(store.get(obs, "Printer A"), "Preset A")
        self.assertEqual(store.get(obs, "Printer B"), "Preset B")
        self.assertEqual(store.get(obs, "Printer C"), "Preset B")  # latest default

    def test_remember_without_printer_sets_default_only(self):
        store = core.HistoryStore(self.path)
        obs = self._obs(spool_id=9)
        store.remember(obs, "", "Global Preset")
        self.assertEqual(store.get(obs, "Any printer"), "Global Preset")

    def test_spool_tag_and_filament_histories_are_distinct(self):
        store = core.HistoryStore(self.path)
        spool_obs = self._obs(spool_id=42, tag_uid="AA:BB")
        tag_obs = self._obs(tag_uid="AA:BB")
        filament_obs = self._obs(color="#123456")

        store.remember(spool_obs, "U1", "Spool Preset")
        store.remember(tag_obs, "U1", "Tag Preset")
        store.remember(filament_obs, "U1", "Filament Preset")

        self.assertEqual(store.get(spool_obs, "U1"), "Spool Preset")
        self.assertEqual(store.get(tag_obs, "U1"), "Tag Preset")
        self.assertEqual(store.get(filament_obs, "U1"), "Filament Preset")

        self.assertTrue(store.forget(spool_obs))
        self.assertEqual(store.get(spool_obs, "U1"), "")
        self.assertEqual(store.get(tag_obs, "U1"), "Tag Preset")
        self.assertEqual(store.get(filament_obs, "U1"), "Filament Preset")

        self.assertTrue(store.forget(tag_obs))
        self.assertEqual(store.get(filament_obs, "U1"), "Filament Preset")

        store.clear()
        self.assertEqual(store.get(filament_obs, "U1"), "")
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document, {"version": 1, "spools": {}, "tags": {}, "filaments": {}})

    def test_forget_unknown_identity_is_false_and_does_not_write(self):
        store = core.HistoryStore(self.path)
        self.assertFalse(store.forget(self._obs(spool_id=123)))
        self.assertFalse(self.path.exists())

    def test_corrupt_file_is_reported_then_replaced_by_next_write(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        store = core.HistoryStore(self.path)
        self.assertTrue(store.load_error)
        self.assertEqual(store.get(self._obs(spool_id=1), "U1"), "")

        store.remember(self._obs(spool_id=1), "U1", "Good Preset")
        reloaded = core.HistoryStore(self.path)
        self.assertEqual(reloaded.load_error, "")
        self.assertEqual(reloaded.get(self._obs(spool_id=1), "U1"), "Good Preset")

    def test_failed_atomic_replace_keeps_old_file_and_memory(self):
        store = core.HistoryStore(self.path)
        obs = self._obs(spool_id=5)
        store.remember(obs, "U1", "Original Preset")

        with mock.patch.object(core.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.remember(obs, "U1", "New Preset")

        self.assertEqual(store.get(obs, "U1"), "Original Preset")
        self.assertEqual(core.HistoryStore(self.path).get(obs, "U1"), "Original Preset")
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_remember_requires_a_preset_name(self):
        store = core.HistoryStore(self.path)
        with self.assertRaises(ValueError):
            store.remember(self._obs(spool_id=1), "U1", "")

    def test_remember_accepts_preset_info_and_candidate(self):
        store = core.HistoryStore(self.path)
        obs = self._obs(spool_id=3)
        preset = make_preset("Candidate Preset")
        store.remember(obs, "U1", preset)
        self.assertEqual(store.get(obs, "U1"), "Candidate Preset")

        candidate = core.Candidate(
            preset_name="From Candidate", score=90, reasons=[], source="installed"
        )
        store.remember(obs, "U1", candidate)
        self.assertEqual(store.get(obs, "U1"), "From Candidate")


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.history_path = Path(self._tmp.name) / "history.json"

    def test_history_precedes_exact_filament_id(self):
        history_preset = make_preset(
            "History PETG @U1", vendor="CC3D", material="PETG", rank=0, user=True
        )
        exact_preset = make_preset(
            "ID PETG @U1",
            vendor="Other",
            material="PETG",
            filament_id="OFDSrzZ8",
        )
        obs = make_obs(
            spool_id=42,
            vendor="CC3D",
            material="PETG",
            filament_id="OFDSrzZ8",
        )
        history = core.HistoryStore(self.history_path)
        history.remember(obs, "U1", history_preset)

        result = core.match_observation(
            obs, [exact_preset, history_preset], history, "U1"
        )
        self.assertEqual(result.source, "history")
        self.assertEqual(result.preset_name, "History PETG @U1")
        self.assertEqual(result.confidence, 100)
        self.assertFalse(result.error)

    def test_missing_history_preset_falls_through(self):
        exact = make_preset("CC3D PETG @U1", vendor="CC3D", material="PETG")
        obs = make_obs(spool_id=42, vendor="CC3D", material="PETG")
        history = core.HistoryStore(self.history_path)
        history.remember(obs, "U1", "Deleted Preset @U1")

        result = core.match_observation(obs, [exact], history, "U1")
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "CC3D PETG @U1")
        # Stale history is retained for possible troubleshooting, not selected.
        self.assertEqual(history.get(obs, "U1"), "Deleted Preset @U1")

    def test_incompatible_history_preset_falls_through(self):
        incompatible = make_preset(
            "Old PETG @U1",
            vendor="CC3D",
            material="PETG",
            compatible=False,
        )
        compatible = make_preset("CC3D PETG @U1", vendor="CC3D", material="PETG")
        obs = make_obs(spool_id=43, vendor="CC3D", material="PETG")
        history = core.HistoryStore(self.history_path)
        history.remember(obs, "U1", incompatible)

        result = core.match_observation(obs, [incompatible, compatible], history, "U1")
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "CC3D PETG @U1")

    def test_history_uses_printer_then_default(self):
        presets = [
            make_preset("Printer A Preset", vendor="CC3D", material="PETG"),
            make_preset("Printer B Preset", vendor="CC3D", material="PETG"),
        ]
        obs = make_obs(spool_id=44, vendor="CC3D", material="PETG")
        history = core.HistoryStore(self.history_path)
        history.remember(obs, "Printer A", "Printer A Preset")
        history.remember(obs, "Printer B", "Printer B Preset")

        on_a = core.match_observation(obs, presets, history, "Printer A")
        on_c = core.match_observation(obs, presets, history, "Printer C")
        self.assertEqual(on_a.preset_name, "Printer A Preset")
        self.assertEqual(on_a.source, "history")
        self.assertEqual(on_c.preset_name, "Printer B Preset")
        self.assertEqual(on_c.source, "history")

    def test_exact_filament_id_selects_one(self):
        preset = make_preset(
            "CC3D PETG @U1",
            vendor="CC3D",
            material="PETG",
            filament_id="OFDSrzZ8",
        )
        obs = make_obs(vendor="CC3D", material="PETG", filament_id="OFDSrzZ8")
        result = core.match_observation(obs, [preset])
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "CC3D PETG @U1")
        self.assertEqual(result.confidence, 100)
        self.assertEqual(len(result.candidates), 1)
        self.assertIn("filament_id", result.candidates[0].reasons[0])

    def test_exact_filament_id_with_multiple_matches_returns_candidates(self):
        presets = [
            make_preset("CC3D PETG 0.4 @U1", filament_id="FID", vendor="CC3D", material="PETG"),
            make_preset("CC3D PETG 0.6 @U1", filament_id="FID", vendor="CC3D", material="PETG"),
        ]
        obs = make_obs(vendor="CC3D", material="PETG", filament_id="FID")
        result = core.match_observation(obs, presets)
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "")
        self.assertEqual(len(result.candidates), 2)
        self.assertTrue(all(candidate.score == 100 for candidate in result.candidates))

    def test_incompatible_exact_id_is_ignored(self):
        incompatible = make_preset(
            "Wrong PETG @U1",
            vendor="Other",
            material="PETG",
            filament_id="FID",
            compatible=False,
        )
        fallback = make_preset("CC3D PETG @U1", vendor="CC3D", material="PETG")
        obs = make_obs(vendor="CC3D", material="PETG", filament_id="FID")
        result = core.match_observation(obs, [incompatible, fallback])
        self.assertEqual(result.preset_name, "CC3D PETG @U1")

    def test_exact_metadata_single_match_and_product_normalization(self):
        preset = make_preset(
            "CC3D PETG-Basic @U1",
            vendor="CC3D",
            material="PETG",
            subtype="Basic",
        )
        obs = make_obs(
            vendor="cc3d",
            material="petg",
            subtype="basic",
            product="CC3D PETG Basic",
        )
        result = core.match_observation(obs, [preset])
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "CC3D PETG-Basic @U1")
        self.assertEqual(result.confidence, 95)

    def test_snapmaker_subtype_only_in_preset_name_is_exact(self):
        presets = [
            make_preset("Snapmaker PLA @U1", vendor="Snapmaker", material="PLA"),
            make_preset("Snapmaker PLA Basic @U1", vendor="Snapmaker", material="PLA"),
            make_preset("Snapmaker PLA SnapSpeed @U1", vendor="Snapmaker", material="PLA",
                        color="#E2DEDB"),
            make_preset("Snapmaker PLA Silk @U1", vendor="Snapmaker", material="PLA"),
        ]
        obs = make_obs(vendor="Snapmaker", material="PLA", subtype="SnapSpeed",
                       color="#E2DEDB")
        result = core.match_observation(obs, presets)
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "Snapmaker PLA SnapSpeed @U1")
        self.assertEqual(result.confidence, 95)
        self.assertIn("subtype", " ".join(result.candidates[0].reasons).casefold())

        score, reasons = core.score_preset(obs, presets[2])
        self.assertGreaterEqual(score, 70)
        self.assertIn("name", " ".join(reasons).casefold())

    def test_snapmaker_material_variant_in_name_is_exact(self):
        preset = make_preset("Snapmaker PLA-CF @U1", vendor="Snapmaker", material="PLA")
        obs = make_obs(vendor="Snapmaker", material="PLA", subtype="PLA-CF")
        result = core.match_observation(obs, [preset])
        self.assertEqual(result.preset_name, "Snapmaker PLA-CF @U1")
        self.assertEqual(result.confidence, 95)

    def test_blank_observed_subtype_still_returns_all_metadata_matches(self):
        presets = [
            make_preset("Snapmaker PLA Basic @U1", vendor="Snapmaker", material="PLA"),
            make_preset("Snapmaker PLA SnapSpeed @U1", vendor="Snapmaker", material="PLA"),
        ]
        obs = make_obs(vendor="Snapmaker", material="PLA")
        result = core.match_observation(obs, presets)
        self.assertEqual(result.preset_name, "")
        self.assertEqual(len(result.candidates), 2)

    def test_exact_metadata_with_multiple_matches_returns_candidates(self):
        presets = [
            make_preset("CC3D PETG 0.4 @U1", vendor="CC3D", material="PETG", rank=0, user=True),
            make_preset("CC3D PETG 0.6 @U1", vendor="CC3D", material="PETG", rank=2),
        ]
        obs = make_obs(vendor="CC3D", material="PETG")
        result = core.match_observation(obs, presets)
        self.assertEqual(result.preset_name, "")
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.confidence, 95)
        self.assertEqual(
            {candidate.preset_name for candidate in result.candidates},
            {"CC3D PETG 0.4 @U1", "CC3D PETG 0.6 @U1"},
        )

    def test_fuzzy_threshold_with_ten_point_margin_selects(self):
        # Product is not an exact product match, so the fuzzy stage decides.
        best = make_preset(
            "CC3D PETG @U1 0.4",
            vendor="CC3D",
            material="PETG",
            color="#F06292",
            rank=0,
            user=True,
        )
        runner_up = make_preset(
            "CC3D Whatever @U1 0.4",
            vendor="CC3D",
            material="PETG",
            color="#C86080",
        )
        obs = make_obs(
            vendor="CC3D",
            material="PETG",
            product="CC3D PETG Pro",
            color="#F06292",
        )
        best_score, _ = core.score_preset(obs, best)
        runner_score, _ = core.score_preset(obs, runner_up)
        self.assertEqual(best_score, 80)
        self.assertEqual(runner_score, 70)

        result = core.match_observation(obs, [runner_up, best])
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "CC3D PETG @U1 0.4")
        self.assertEqual(result.confidence, 80)

    def test_fuzzy_close_scores_return_candidates(self):
        best = make_preset(
            "CC3D PETG @U1 0.4",
            vendor="CC3D",
            material="PETG",
            color="#F06292",
            rank=0,
            user=True,
        )
        runner_up = make_preset(
            "CC3D Whatever @U1 0.4",
            vendor="CC3D",
            material="PETG",
            color="#F06292",
        )
        obs = make_obs(
            vendor="CC3D",
            material="PETG",
            product="CC3D PETG Pro",
            color="#F06292",
        )
        self.assertEqual(core.score_preset(obs, best)[0], 80)
        self.assertEqual(core.score_preset(obs, runner_up)[0], 72)

        result = core.match_observation(obs, [best, runner_up])
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "")
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.confidence, 80)
        self.assertEqual(
            {candidate.preset_name for candidate in result.candidates},
            {"CC3D PETG @U1 0.4", "CC3D Whatever @U1 0.4"},
        )

    def test_fuzzy_below_threshold_uses_generic(self):
        vendor_preset = make_preset(
            "CC3D PETG @U1 0.4",
            vendor="CC3D",
            material="PETG",
            color="#F06292",
        )
        generic = make_preset(
            "Generic PETG @U1 0.4",
            base="Generic PETG",
            vendor="Generic",
            material="PETG",
            color="#F06292",
            rank=2,
        )
        obs = make_obs(
            vendor="ACME",
            material="PETG",
            product="ACME PETG",
            color="#111111",
        )
        self.assertLess(core.score_preset(obs, vendor_preset)[0], core.FUZZY_THRESHOLD)

        result = core.match_observation(obs, [vendor_preset, generic])
        self.assertEqual(result.source, "generic")
        self.assertEqual(result.preset_name, "Generic PETG @U1 0.4")
        self.assertEqual(result.confidence, 30)
        self.assertEqual(len(result.candidates), 1)

    def test_exact_metadata_prefers_base_preset_when_subtype_unknown(self):
        base = make_preset("Snapmaker PLA @U1", vendor="Snapmaker", material="PLA")
        basic = make_preset("Snapmaker PLA Basic @U1", vendor="Snapmaker",
                            material="PLA")
        obs = make_obs(vendor="Snapmaker", material="PLA", product="Yellow")
        result = core.match_observation(obs, [basic, base])
        self.assertEqual(result.preset_name, "")
        self.assertEqual(result.candidates[0].preset_name, "Snapmaker PLA @U1")

    def test_color_only_product_does_not_block_exact_metadata(self):
        preset = make_preset("Snapmaker PLA @U1", vendor="Snapmaker", material="PLA")
        obs = make_obs(vendor="Snapmaker", material="PLA", product="Yellow",
                       color="#F4C032")
        result = core.match_observation(obs, [preset])
        self.assertEqual(result.preset_name, "Snapmaker PLA @U1")
        self.assertEqual(result.confidence, 95)

    def test_custom_labels_are_ignored_and_local_copy_wins(self):
        custom = make_preset(
            "CC3D PETG (custom)", vendor="CC3D", material="PETG",
            system=False, user=True, rank=0)
        local = make_preset(
            "_local/filament_match/CC3D PETG (custom) (local)",
            base="CC3D PETG (custom) (local)", vendor="CC3D",
            material="PETG", bundle_id="filament_match", system=False,
            rank=1)
        ofd = make_preset(
            "_local/filament_match/CC3D PETG (OFD)",
            base="CC3D PETG (OFD)", vendor="CC3D", material="PETG",
            bundle_id="filament_match", system=False, rank=1)
        obs = make_obs(vendor="CC3D", material="PETG",
                       product="PETG Pink", color="#FAB9E1", spool_id=8)
        for preset in (custom, local, ofd):
            self.assertTrue(core._exact_metadata(obs, preset), preset.name)
        result = core.match_observation(obs, [custom, local, ofd])
        self.assertEqual(result.preset_name, "")
        self.assertEqual(result.candidates[0].preset_name, local.name)
        self.assertEqual(
            {candidate.preset_name for candidate in result.candidates},
            {custom.name, local.name, ofd.name},
        )

        history = {"spools": {"8": {"default": local.name}}}
        result = core.match_observation(
            obs, [custom, local, ofd], history=history, printer="MyKlipper")
        self.assertEqual(result.source, "history")
        self.assertEqual(result.preset_name, local.name)
        self.assertEqual(result.confidence, 100)

    def test_local_bundle_internal_name_is_normalized_for_matching(self):
        local = make_preset(
            "_local/filament_match/eSUN PETG (local)",
            base="eSUN PETG (local)", vendor="eSUN", material="PETG",
            bundle_id="filament_match", system=False, rank=1,
        )
        system = make_preset(
            "eSUN PETG @System", vendor="eSUN", material="PETG")
        obs = make_obs(vendor="eSUN", material="PETG",
                       product="PETG Basic Transparent Green", color="#03720A",
                       spool_id=9)
        self.assertTrue(core._exact_metadata(obs, local))
        result = core.match_observation(obs, [system, local])
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "")
        self.assertEqual(
            [candidate.preset_name for candidate in result.candidates],
            [local.name, system.name],
        )

        history = {"spools": {"9": {"default": local.name}}}
        result = core.match_observation(
            obs, [system, local], history=history, printer="MyKlipper")
        self.assertEqual(result.source, "history")
        self.assertEqual(result.preset_name, local.name)
        self.assertEqual(result.confidence, 100)

    def test_filament_cost_does_not_affect_matching(self):
        cheap = make_preset("CC3D PETG @U1", vendor="CC3D", material="PETG",
                            config={"filament_cost": "1.00"})
        expensive = make_preset("CC3D PETG @U1", vendor="CC3D",
                                material="PETG",
                                config={"filament_cost": "99.00"})
        obs = make_obs(vendor="CC3D", material="PETG", product="PETG Pink",
                       color="#FAB9E1")
        first = core.match_observation(obs, [cheap])
        second = core.match_observation(obs, [expensive])
        self.assertEqual(first.preset_name, second.preset_name)
        self.assertEqual(first.source, second.source)
        self.assertEqual(first.confidence, second.confidence)
        self.assertEqual(core.score_preset(obs, cheap)[0],
                         core.score_preset(obs, expensive)[0])

    def test_local_price_copy_suffix_does_not_block_matching(self):
        preset = make_preset("eSUN PETG (local)", vendor="eSUN", material="PETG")
        obs = make_obs(vendor="eSUN", material="PETG",
                       product="PETG Basic Transparent Green", color="#03720A",
                       spool_id=7)
        result = core.match_observation(obs, [preset])
        self.assertEqual(result.preset_name, "eSUN PETG (local)")
        self.assertEqual(result.confidence, 95)

        history = {"spools": {"7": {"default": preset.name}}}
        result = core.match_observation(
            obs, [preset], history=history, printer="Mock U1")
        self.assertEqual(result.preset_name, "eSUN PETG (local)")
        self.assertEqual(result.source, "history")
        self.assertEqual(result.confidence, 100)

    def test_product_grade_word_does_not_block_exact_metadata(self):
        preset = make_preset("eSUN PETG @System", vendor="eSUN", material="PETG")
        obs = make_obs(vendor="eSUN", material="PETG",
                       product="PETG Basic Transparent Green", color="#03720A")
        result = core.match_observation(obs, [preset])
        self.assertEqual(result.preset_name, "eSUN PETG @System")
        self.assertEqual(result.confidence, 95)

    def test_meaningful_product_word_still_constrains_matching(self):
        base = make_preset("eSUN PETG @System", vendor="eSUN", material="PETG")
        matte = make_preset("eSUN PETG Matte @System", vendor="eSUN",
                            material="PETG")
        obs = make_obs(vendor="eSUN", material="PETG",
                       product="PETG Matte Transparent Green", color="#03720A")
        result = core.match_observation(obs, [base, matte])
        self.assertEqual(result.preset_name, "eSUN PETG Matte @System")

    def test_generic_fallback_prefers_exact_material_over_variant(self):
        exact = make_preset("Generic PLA @System", base="Generic PLA",
                            vendor="Generic", material="PLA", color="#000000")
        variant = make_preset("Generic PLA-CF @System", base="Generic PLA-CF",
                              vendor="Generic", material="PLA", color="#F4C032")
        obs = make_obs(material="PLA", color="#F4C032")
        result = core.match_observation(obs, [variant, exact])
        self.assertEqual(result.preset_name, "Generic PLA @System")

    def test_stale_history_variant_is_ignored_for_color_only_product(self):
        snap = make_preset("Snapmaker PLA SnapSpeed @U1", vendor="Snapmaker",
                           material="PLA")
        base = make_preset("Snapmaker PLA @U1", vendor="Snapmaker", material="PLA")
        obs = make_obs(vendor="Snapmaker", material="PLA", product="Black",
                       spool_id=3)
        history = {"spools": {"3": {"default": snap.name}}}
        result = core.match_observation(
            obs, [snap, base], history=history, printer="Snapmaker U1")
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.preset_name, "")
        self.assertEqual(
            {candidate.preset_name for candidate in result.candidates},
            {"Snapmaker PLA SnapSpeed @U1", "Snapmaker PLA @U1"},
        )

    def test_stale_history_is_ignored_after_material_change(self):
        old = make_preset("Snapmaker PLA SnapSpeed @U1", vendor="Snapmaker",
                          material="PLA")
        new = make_preset("Snapmaker PETG @U1", vendor="Snapmaker", material="PETG")
        obs = make_obs(slot=1, vendor="Snapmaker", material="PETG", spool_id=3)
        history = {"spools": {"3": {"default": old.name}}}
        result = core.match_observation(
            obs, [old, new], history=history, printer="Snapmaker U1")
        self.assertEqual(result.preset_name, "Snapmaker PETG @U1")
        self.assertEqual(result.source, "installed")
        self.assertEqual(result.confidence, 95)

    def test_generic_fallback_is_not_written_to_history(self):
        generic = make_preset(
            "Generic PLA @U1 0.4",
            base="Generic PLA",
            vendor="Generic",
            material="PLA",
        )
        obs = make_obs(vendor="ACME", material="PLA", color="#123456")
        history = core.HistoryStore(self.history_path)

        result = core.match_observation(obs, [generic], history, "U1")
        self.assertEqual(result.source, "generic")
        self.assertFalse(self.history_path.exists())
        self.assertEqual(history.get(obs, "U1"), "")

    def test_generic_tie_returns_candidates(self):
        first = make_preset(
            "Generic PETG @U1",
            base="Generic PETG",
            vendor="Generic",
            material="PETG",
            color="#808080",
        )
        second = make_preset(
            "Generic PETG @U1 0.4",
            base="Generic PETG",
            vendor="Generic",
            material="PETG",
            color="#808080",
        )
        obs = make_obs(vendor="ACME", material="PETG", color="#808080")
        result = core.match_observation(obs, [first, second])
        self.assertEqual(result.source, "generic")
        self.assertEqual(result.preset_name, "")
        self.assertEqual(len(result.candidates), 2)
        self.assertTrue(all(candidate.score == 30 for candidate in result.candidates))

    def test_incompatible_generic_is_not_used(self):
        generic = make_preset(
            "Generic PETG @U1 0.4",
            base="Generic PETG",
            vendor="Generic",
            material="PETG",
            compatible=False,
        )
        obs = make_obs(vendor="ACME", material="PETG")
        result = core.match_observation(obs, [generic])
        self.assertTrue(result.error)
        self.assertEqual(result.preset_name, "")

    def test_library_candidate_is_returned_but_not_selected(self):
        generic = make_preset(
            "Generic PETG @U1 0.4",
            base="Generic PETG",
            vendor="Generic",
            material="PETG",
        )
        obs = make_obs(
            vendor="ACME",
            material="PETG",
            product="ACME PETG",
            color="#336699",
        )
        calls = []

        def lookup(observation):
            calls.append(observation)
            return core.Candidate(
                preset_name="ACME PETG @U1 0.4",
                score=55,
                reasons=["OFD index hit"],
                source="ignored",
                library_url="https://example.test/acme.json",
                library_meta={"brand": "ACME", "profile_name": "ACME PETG @U1 0.4"},
            )

        result = core.match_observation(obs, [generic], library_lookup=lookup)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.source, "library")
        self.assertEqual(result.preset_name, "")
        self.assertFalse(result.error)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.source, "library")
        self.assertEqual(candidate.preset_name, "ACME PETG @U1 0.4")
        self.assertEqual(candidate.library_url, "https://example.test/acme.json")
        self.assertEqual(candidate.library_meta["brand"], "ACME")

    def test_library_multiple_candidates_are_all_returned(self):
        obs = make_obs(vendor="ACME", material="PETG", product="ACME PETG")

        result = core.match_observation(
            obs,
            [],
            library_lookup=lambda _obs: [
                {"profile_name": "ACME PETG A", "url": "https://example.test/a.json", "score": 40},
                {"profile_name": "ACME PETG B", "url": "https://example.test/b.json", "score": 60},
            ],
        )
        self.assertEqual(result.source, "library")
        self.assertEqual(result.preset_name, "")
        self.assertEqual(result.confidence, 60)
        self.assertEqual(
            [candidate.preset_name for candidate in result.candidates],
            ["ACME PETG B", "ACME PETG A"],
        )

    def test_library_dict_shape_and_exception_fall_back_to_generic(self):
        generic = make_preset(
            "Generic PETG @U1 0.4",
            base="Generic PETG",
            vendor="Generic",
            material="PETG",
        )
        obs = make_obs(vendor="ACME", material="PETG", product="ACME PETG")

        from_dict = core.match_observation(
            obs,
            [generic],
            library_lookup=lambda _obs: {
                "profile_name": "ACME PETG @U1 0.4",
                "url": "https://example.test/acme.json",
                "brand": "ACME",
            },
        )
        self.assertEqual(from_dict.source, "library")
        self.assertEqual(from_dict.candidates[0].library_url, "https://example.test/acme.json")
        self.assertEqual(from_dict.candidates[0].library_meta["brand"], "ACME")

        def broken(_obs):
            raise RuntimeError("library offline")

        fallback = core.match_observation(obs, [generic], library_lookup=broken)
        self.assertEqual(fallback.source, "generic")
        self.assertEqual(fallback.preset_name, "Generic PETG @U1 0.4")

    def test_no_match_returns_error_without_candidates(self):
        preset = make_preset("CC3D PETG @U1", vendor="CC3D", material="PETG")
        obs = make_obs(vendor="ACME", material="ABS", color="#000000")
        result = core.match_observation(obs, [preset])
        self.assertEqual(result.preset_name, "")
        self.assertEqual(result.source, "")
        self.assertEqual(result.candidates, [])
        self.assertIn(core.NO_MATCH_ERROR, result.error)
        self.assertIn("ABS", result.error)

    def test_blank_material_cannot_match_or_fall_back(self):
        preset = make_preset("CC3D PETG @U1", vendor="CC3D", material="PETG")
        obs = make_obs(vendor="CC3D")
        result = core.match_observation(obs, [preset])
        self.assertTrue(result.error)
        self.assertEqual(result.preset_name, "")

    def test_incompatible_presets_are_never_selected(self):
        presets = [
            make_preset(
                "CC3D PETG @Other",
                vendor="CC3D",
                material="PETG",
                filament_id="FID",
                compatible=False,
            ),
            make_preset(
                "Generic PETG @Other",
                base="Generic PETG",
                vendor="Generic",
                material="PETG",
                compatible=False,
            ),
        ]
        obs = make_obs(vendor="CC3D", material="PETG", filament_id="FID")
        result = core.match_observation(obs, presets)
        self.assertTrue(result.error)
        self.assertEqual(result.candidates, [])


if __name__ == "__main__":
    unittest.main()
