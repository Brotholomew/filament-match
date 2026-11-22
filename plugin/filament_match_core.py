"""Pure-Python core for the Filament Match OrcaSlicer plugin.

No Orca import, no network, no import-time side effects. The entry/UI worker
imports the public names below and supplies installed presets plus a
``HistoryStore`` backed by plugin storage.

Matching order (see PLAN.md):

1. user-confirmed history, but only while that preset still exists and is
   compatible with the printer;
2. exact ``filament_id``;
3. exact vendor + material + subtype + product;
4. fuzzy score, threshold 70, margin 10 over the next qualifying candidate;
5. ``library_lookup`` candidates (presented, never auto-installed);
6. compatible ``Generic <material>`` fallback;
7. an error string when nothing matches.

Identity keys are ``spool:<id>``, ``tag:<normalized uid>`` or
``filament:<sha1(vendor|material|subtype|product|color)>``. History stores
those under the ``spools`` / ``tags`` / ``filaments`` sections with a
``default`` and per-printer names, and writes atomically.

Assumptions/extensions worth knowing at the call site:

* ``PresetInfo`` has no product field in the plan; product values are derived
  from an optional duck-typed ``product`` attribute, then ``base``, then the
  name without ``@Printer`` and without a leading vendor prefix.
* A blank observation subtype/product means "unknown", not "must be blank",
  in exact-metadata matching. Vendor and material are required for that step.
* ``score_preset`` weights: material 30, vendor 20, subtype 15, product token
  overlap 20, colour 10, name prefix 5.
* ``library_lookup`` failures are swallowed so a dead library cannot block the
  generic fallback; it also accepts one Candidate, an iterable of Candidates,
  or mappings with ``profile_name``/``name`` and ``url``/``path``.
* ``MatchResult.candidates`` contains the selected candidate too, so the UI can
  always show provenance and reasons. For ambiguous matches ``preset_name`` is
  blank and the candidates are the choice.
* ``HistoryStore.get`` returns ``""`` for "no mapping"; ``.data`` is a read-only
  copy and ``.load_error`` records an unreadable/corrupt file instead of raising
  at construction. The next successful write replaces a corrupt file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "Observation",
    "PresetInfo",
    "Candidate",
    "MatchResult",
    "identity_key",
    "normalize_text",
    "normalize_vendor",
    "normalize_material",
    "normalize_color",
    "normalize_tag_uid",
    "strip_printer_suffix",
    "strip_bundle_prefix",
    "strip_color_words",
    "color_distance",
    "score_preset",
    "match_observation",
    "HistoryStore",
    "FUZZY_THRESHOLD",
    "FUZZY_MARGIN",
    "NO_MATCH_ERROR",
]

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    slot: int  # 1-based Orca slot
    present: bool = True
    vendor: str = ""
    material: str = ""  # PLA, PETG, ABS...
    subtype: str = ""  # SnapSpeed, Silk, Matte...
    product: str = ""  # "PolyLite PLA", "PETG-Basic"...
    color: str = ""  # "#RRGGBB"
    filament_id: str = ""  # Orca filament_id, when the printer reports it
    spool_id: int | None = None
    tag_uid: str = ""
    bed_temp: int | None = None
    nozzle_temp: int | None = None
    source: str = ""  # snapmaker / moonraker
    raw: dict = field(default_factory=dict)


@dataclass
class PresetInfo:
    name: str
    base: str
    vendor: str
    material: str
    subtype: str
    color: str
    filament_id: str
    compatible: bool
    system: bool
    user: bool
    bundle_id: str
    file: str
    rank: int  # user=0, bundle=1, system=2
    config: dict = field(default_factory=dict)


@dataclass
class Candidate:
    preset_name: str
    score: int
    reasons: list[str]
    source: str  # history / installed / library / generic
    preset: PresetInfo | None = None
    library_url: str = ""
    library_meta: dict = field(default_factory=dict)


@dataclass
class MatchResult:
    observation: Observation
    preset_name: str = ""
    source: str = ""
    confidence: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    error: str = ""


FUZZY_THRESHOLD = 70
FUZZY_MARGIN = 10
NO_MATCH_ERROR = "No compatible filament preset matched"

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_COLOR_WORDS = frozenset(
    {
        "black",
        "white",
        "red",
        "green",
        "blue",
        "yellow",
        "orange",
        "purple",
        "pink",
        "brown",
        "grey",
        "gray",
        "clear",
        "transparent",
        "natural",
        "silver",
        "gold",
        "cyan",
        "magenta",
        "beige",
        "ivory",
        "charcoal",
        "turquoise",
        "violet",
        "indigo",
        "teal",
        "lime",
        "navy",
        "maroon",
        "olive",
        "aqua",
        "coral",
        "peach",
        "mint",
        "lavender",
        "rose",
        "wine",
        "bronze",
        "copper",
        "chrome",
        "pearl",
    }
)

_HEX_RE = re.compile(r"^[0-9a-f]+$")
_LOCAL_SUFFIX_RE = re.compile(
    r"\s*\((?:local|custom|ofd|copy)\)\s*$", re.I)
_HISTORY_SECTIONS = ("spools", "tags", "filaments")


def _core_text(value: Any) -> str:
    return "" if value is None else str(value)


def strip_local_suffix(value: Any) -> str:
    """Remove trailing provenance labels from a preset name for matching.

    ``(local)``, ``(custom)``, ``(OFD)`` and ``(copy)`` are labels, not
    product-line tokens. Remove repeated suffixes so
    ``CC3D PETG (custom) (local)`` matches as ``CC3D PETG``.
    """
    text = _core_text(value).strip()
    while True:
        stripped = _LOCAL_SUFFIX_RE.sub("", text).strip()
        if stripped == text:
            return text
        text = stripped


def normalize_text(value: Any) -> str:
    """Casefold, expand ``+``/``&``, strip punctuation and collapse spaces."""
    text = unicodedata.normalize("NFKC", _core_text(value))
    text = text.replace("+", " plus ").replace("&", " and ")
    text = text.casefold()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return " ".join(text.split())


def strip_printer_suffix(value: Any) -> str:
    """Return the part of a preset name before ``@Printer``."""
    return re.split(r"\s*@\s*", _core_text(value).strip(), maxsplit=1)[0].strip()


def strip_bundle_prefix(value: Any) -> str:
    """Remove bundle markers and Orca's internal ``_local/<id>/`` prefix.

    Orca exposes local/subscribed bundle presets by their internal name
    ``_local/<bundle_id>/<base>`` even though the selector uses ``<base>``.
    Matching must use the visible base name, otherwise the prefix becomes a
    fake product/variant token and hides the preset from exact matching.
    """
    text = _core_text(value).strip()
    text = re.sub(r"^_(?:local|subscribed)/[^/]+/", "", text, flags=re.I)
    text = re.sub(r"^\s*\[[^\]]*\]\s*", "", text)
    text = re.sub(r"^\s*\([^)]*\)\s*", "", text)
    text = re.sub(r"^(?:bundle|local|user|system)\s*[:\-]\s*", "", text, flags=re.I)
    return text.strip()


def normalize_vendor(value: Any) -> str:
    return normalize_text(strip_bundle_prefix(value))


def normalize_material(value: Any) -> str:
    text = normalize_text(value)
    # Common spelling variant in printer payloads and preset names.
    return re.sub(r"\bpet\s+g\b", "petg", text)


def _parse_hex_color(value: Any) -> tuple[int, int, int] | None:
    text = _core_text(value).strip().lower()
    prefixed = False
    if text.startswith("#"):
        text = text[1:]
        prefixed = True
    elif text.startswith("0x"):
        text = text[2:]
        prefixed = True
    if not text or not _HEX_RE.match(text):
        return None
    if len(text) in (3, 4):  # #RGB / #RGBA
        if not prefixed:  # bare "bad" is a name, not shorthand hex
            return None
        text = "".join(ch * 2 for ch in text[:3])
    elif len(text) == 8:  # #RRGGBBAA: alpha is ignored
        text = text[:6]
    if len(text) != 6:
        return None
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def normalize_color(value: Any) -> str:
    """Return canonical ``#rrggbb`` or ``""`` when the colour is not hex."""
    rgb = _parse_hex_color(value)
    if rgb is None:
        return ""
    return "#" + "".join(f"{channel:02x}" for channel in rgb)


def normalize_tag_uid(value: Any) -> str:
    """Normalize RFID UIDs: uppercase, separators removed."""
    return re.sub(r"[^0-9A-Za-z]", "", _core_text(value)).upper()


def strip_color_words(value: Any) -> str:
    tokens = [token for token in normalize_text(value).split() if token not in _COLOR_WORDS]
    return " ".join(tokens)


def color_distance(a: Any, b: Any) -> float | None:
    """Euclidean RGB distance (0..441.67), or ``None`` if either is invalid."""
    first = _parse_hex_color(a)
    second = _parse_hex_color(b)
    if first is None or second is None:
        return None
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(first, second)))


# ---------------------------------------------------------------------------
# Preset/product helpers
# ---------------------------------------------------------------------------


def _preset_name(preset: Any) -> str:
    if isinstance(preset, str):
        return preset.strip()
    name = getattr(preset, "preset_name", "")
    if isinstance(name, str) and name.strip():
        return name.strip()
    inner = getattr(preset, "preset", None)
    if inner is not None and inner is not preset:
        name = getattr(inner, "name", "")
        if isinstance(name, str) and name.strip():
            return name.strip()
    name = getattr(preset, "name", "")
    if isinstance(name, str):
        return name.strip()
    return _core_text(preset).strip()


def _preset_rank(preset: Any) -> int:
    try:
        return int(getattr(preset, "rank", 2))
    except (TypeError, ValueError):
        return 2


def _name_without_vendor(preset: Any) -> str:
    name = normalize_text(strip_local_suffix(
        strip_printer_suffix(strip_bundle_prefix(
            getattr(preset, "name", "")))))
    vendor = normalize_vendor(getattr(preset, "vendor", ""))
    if not name or not vendor:
        return name
    name_tokens = name.split()
    vendor_tokens = vendor.split()
    if name_tokens[: len(vendor_tokens)] == vendor_tokens:
        name_tokens = name_tokens[len(vendor_tokens) :]
    return " ".join(name_tokens)


def _preset_product_texts(preset: Any) -> list[str]:
    texts: list[str] = []
    extra = getattr(preset, "product", "")
    if isinstance(extra, str) and extra.strip():
        texts.append(extra)
    base = getattr(preset, "base", "")
    if isinstance(base, str) and base.strip():
        texts.append(strip_local_suffix(strip_bundle_prefix(base)))
    name = strip_local_suffix(strip_printer_suffix(
        strip_bundle_prefix(getattr(preset, "name", ""))))
    if name:
        texts.append(name)
    without_vendor = _name_without_vendor(preset)
    if without_vendor:
        texts.append(without_vendor)
    seen: set[str] = set()
    result: list[str] = []
    for text in texts:
        normalized = normalize_text(text)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return result


def _normalized_product(value: Any) -> str:
    raw = normalize_text(value)
    stripped = strip_color_words(value)
    # A product that is only a colour word ("Black") must still be comparable.
    return stripped or raw


_PRODUCT_QUALIFIERS = frozenset(
    {
        # Marketing grade words that do not identify a different filament
        # line. "eSUN PETG Basic" is still the eSUN PETG preset; "Premium
        # PCTG" is still the base Spectrum PCTG line.
        "basic",
        "standard",
        "premium",
        "value",
        "regular",
        "performance",
    }
)


def _observed_product_text(obs: Observation) -> str:
    """The product text that may constrain a match.

    Spoolman's filament name is often just a colour ("Yellow"), a colour plus
    the material ("PETG Pink"), or the material plus a grade word ("PETG
    Basic"). None of those identify a product line, so they must not prevent
    an exact vendor/material preset match. A name with an actual extra token
    ("ABS Pastel", "PETG Matte") still constrains it.
    """
    raw = getattr(obs, "product", "")
    text = strip_color_words(raw)
    if not text:
        return ""
    subtype_tokens = set(normalize_text(getattr(obs, "subtype", "")).split())
    tokens = [
        token for token in text.split()
        if token not in _PRODUCT_QUALIFIERS or token in subtype_tokens
    ]
    if not tokens:
        return ""
    return " ".join(tokens)


def _products_match(observed: Any, preset: Any) -> bool:
    wanted = _normalized_product(observed)
    if not wanted:
        return True
    return any(wanted == _normalized_product(text) for text in _preset_product_texts(preset))


def _token_similarity(first: str, second: str) -> float:
    a = set(first.split())
    b = set(second.split())
    if not a or not b:
        return 0.0
    if first == second:
        return 1.0
    common = len(a & b)
    if not common:
        return 0.0
    dice = (2.0 * common) / (len(a) + len(b))
    coverage = common / len(a)
    return min(1.0, 0.6 * dice + 0.4 * coverage)


def _product_similarity(obs: Observation, preset: Any) -> float:
    wanted = _observed_product_text(obs)
    if not wanted:
        return 0.0
    best = 0.0
    for text in _preset_product_texts(preset):
        candidate = _normalized_product(text)
        if not candidate:
            continue
        if wanted == candidate:
            return 1.0
        best = max(best, _token_similarity(wanted, candidate))
    return best


def _is_generic(preset: Any) -> bool:
    name = normalize_text(strip_printer_suffix(strip_bundle_prefix(getattr(preset, "name", ""))))
    if name == "generic" or name.startswith("generic "):
        return True
    return normalize_vendor(getattr(preset, "vendor", "")) == "generic"


def _name_prefix_score(obs: Observation, preset: Any) -> int:
    name = normalize_text(
        strip_printer_suffix(strip_bundle_prefix(getattr(preset, "name", "")))
    )
    if not name:
        return 0
    candidates = (
        normalize_vendor(getattr(obs, "vendor", "")),
        _normalized_product(getattr(obs, "product", "")),
        normalize_material(getattr(obs, "material", "")),
    )
    for value in candidates:
        if value and (name == value or name.startswith(value + " ")):
            return 5
    return 0


def _name_without_vendor_material(preset: Any) -> str:
    """The preset name with vendor and material tokens removed.

    Snapmaker presets carry the filament variant (SnapSpeed, Silk, Matte) only
    in the preset name, not in a config key, so the tail is the usable subtype.
    """
    tokens = normalize_text(strip_local_suffix(
        strip_printer_suffix(strip_bundle_prefix(
            getattr(preset, "name", "")))
    )).split()
    remove = set(normalize_vendor(getattr(preset, "vendor", "")).split())
    remove.update(normalize_material(getattr(preset, "material", "")).split())
    return " ".join(token for token in tokens if token not in remove)


def _subtype_match(obs: Observation, preset: Any) -> tuple[int, str]:
    """Compare the observed subtype with the preset's config *or its name*.

    Returns (2 exact, 1 partial, 0 none) and a short reason. The name fallback
    is required for Snapmaker-style presets: their filament_sub_type is only in
    the printer payload and the preset name ("Snapmaker PLA SnapSpeed"), not in
    a config option the plugin can read.
    """
    observed = normalize_text(getattr(obs, "subtype", ""))
    if not observed:
        return 0, ""
    configured = normalize_text(getattr(preset, "subtype", ""))
    if configured and observed == configured:
        return 2, f"subtype {_core_text(getattr(obs, 'subtype', '')).strip()}"

    name = normalize_text(
        strip_printer_suffix(strip_bundle_prefix(getattr(preset, "name", "")))
    )
    texts = [_name_without_vendor_material(preset)]
    # A material variant such as "PLA-CF" normalizes to two tokens and is only
    # intact in the full name; a single token such as "PLA" would be noise.
    if " " in observed:
        texts.append(name)
    padded = " %s " % observed
    for text in texts:
        if text and padded in (" %s " % text):
            return 2, f"subtype {_core_text(getattr(obs, 'subtype', '')).strip()} (name)"

    if configured and (observed in configured or configured in observed):
        return 1, "subtype partial"
    return 0, ""



def score_preset(obs: Observation, preset: PresetInfo) -> tuple[int, list[str]]:
    """Score one preset against one observation (0..100) with short reasons."""
    score = 0
    reasons: list[str] = []

    observed_material = normalize_material(getattr(obs, "material", ""))
    preset_material = normalize_material(getattr(preset, "material", ""))
    if observed_material and observed_material == preset_material:
        score += 30
        reasons.append(f"material {_core_text(getattr(obs, 'material', '')).strip()}")

    observed_vendor = normalize_vendor(getattr(obs, "vendor", ""))
    preset_vendor = normalize_vendor(getattr(preset, "vendor", ""))
    if observed_vendor and preset_vendor:
        if observed_vendor == preset_vendor:
            score += 20
            reasons.append(f"vendor {_core_text(getattr(obs, 'vendor', '')).strip()}")
        elif observed_vendor in preset_vendor or preset_vendor in observed_vendor:
            score += 10
            reasons.append("vendor partial")

    observed_subtype = normalize_text(getattr(obs, "subtype", ""))
    if observed_subtype:
        subtype_level, subtype_reason = _subtype_match(obs, preset)
        if subtype_level == 2:
            score += 15
            reasons.append(subtype_reason)
        elif subtype_level == 1:
            score += 8
            reasons.append(subtype_reason)

    similarity = _product_similarity(obs, preset)
    if similarity:
        points = int(round(20 * similarity))
        if points:
            score += points
            reasons.append(f"product {int(round(similarity * 100))}%")

    distance = color_distance(getattr(obs, "color", ""), getattr(preset, "color", ""))
    if distance is not None:
        closeness = max(0.0, 1.0 - (distance / 255.0))
        points = int(round(10 * closeness))
        if points:
            score += points
            reasons.append(f"colour Δ{distance:.0f}")

    prefix = _name_prefix_score(obs, preset)
    if prefix:
        score += prefix
        reasons.append("name prefix")

    return max(0, min(100, score)), reasons


# ---------------------------------------------------------------------------
# Identity and history
# ---------------------------------------------------------------------------


def identity_key(obs: Observation) -> str:
    """Return the stable identity used by history, in the documented order."""
    spool_id = getattr(obs, "spool_id", None)
    if spool_id is not None and _core_text(spool_id).strip():
        return f"spool:{_core_text(spool_id).strip()}"

    tag = normalize_tag_uid(getattr(obs, "tag_uid", ""))
    if tag:
        return f"tag:{tag}"

    color = normalize_color(getattr(obs, "color", ""))
    if not color:
        color = normalize_text(getattr(obs, "color", "")).replace(" ", "")
    payload = "|".join(
        (
            normalize_vendor(getattr(obs, "vendor", "")),
            normalize_material(getattr(obs, "material", "")),
            normalize_text(getattr(obs, "subtype", "")),
            normalize_text(getattr(obs, "product", "")),
            color.lstrip("#"),
        )
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"filament:{digest}"


def _split_identity(key: str) -> tuple[str, str]:
    if key.startswith("spool:"):
        return "spools", key[len("spool:") :]
    if key.startswith("tag:"):
        return "tags", normalize_tag_uid(key[len("tag:") :])
    if key.startswith("filament:"):
        return "filaments", key[len("filament:") :]
    return "filaments", key


def _entry_name(entry: Any, printer: str) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, Mapping):
        return ""
    if printer:
        printers = entry.get("printers")
        if isinstance(printers, Mapping):
            value = printers.get(printer)
            if isinstance(value, str) and value.strip():
                return value.strip()
    default = entry.get("default")
    if isinstance(default, str):
        return default.strip()
    return ""


class HistoryStore:
    """Versioned, atomically written spool/tag/filament → preset mappings.

    Only explicit :meth:`remember` calls write. ``match_observation`` never
    records its generic fallback (or any other automatic choice).
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.load_error = ""
        self._lock = threading.RLock()
        self._data: dict[str, Any] = _empty_history()
        self._load()

    @property
    def data(self) -> dict[str, Any]:
        """A copy of the in-memory document (handy for diagnostics/UI)."""
        with self._lock:
            return copy.deepcopy(self._data)

    def _load(self) -> None:
        try:
            with open(os.fspath(self.path), "r", encoding="utf-8") as stream:
                text = stream.read()
        except FileNotFoundError:
            return
        except OSError as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            return
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("history root is not an object")
        except (ValueError, TypeError) as exc:
            self.load_error = f"invalid history JSON: {exc}"
            return

        data = dict(raw)
        for section in _HISTORY_SECTIONS:
            if not isinstance(data.get(section), dict):
                data[section] = {}
        data.setdefault("version", 1)
        self._data = data

    def get(self, obs: Observation | str, printer: str = "") -> str:
        """Return the printer-specific mapping, else the default (or ``""``)."""
        key = obs if isinstance(obs, str) else identity_key(obs)
        section, part = _split_identity(key)
        printer_name = _core_text(printer).strip()
        with self._lock:
            section_map = self._data.get(section)
            if not isinstance(section_map, Mapping):
                return ""
            return _entry_name(section_map.get(part), printer_name)

    def remember(
        self,
        obs: Observation,
        printer: str,
        preset: str | PresetInfo | Candidate,
    ) -> str:
        """Persist a user-confirmed mapping and return the identity key."""
        name = _preset_name(preset)
        if not name:
            raise ValueError("preset name is required")
        key = identity_key(obs)
        section, part = _split_identity(key)
        printer_name = _core_text(printer).strip()

        with self._lock:
            new_data = copy.deepcopy(self._data)
            section_map = new_data.setdefault(section, {})
            if not isinstance(section_map, dict):
                section_map = {}
                new_data[section] = section_map
            entry = section_map.get(part)
            entry = copy.deepcopy(entry) if isinstance(entry, dict) else {}
            printers = entry.get("printers")
            printers = dict(printers) if isinstance(printers, Mapping) else {}
            if printer_name:
                printers[printer_name] = name
            entry["printers"] = printers
            entry["default"] = name
            entry["updated"] = int(time.time())
            section_map[part] = entry

            self._write(new_data)
            self._data = new_data
        return key

    def forget(self, obs: Observation | str) -> bool:
        """Remove one identity entirely. Returns whether anything was deleted."""
        key = obs if isinstance(obs, str) else identity_key(obs)
        section, part = _split_identity(key)
        with self._lock:
            new_data = copy.deepcopy(self._data)
            section_map = new_data.get(section)
            if not isinstance(section_map, dict) or part not in section_map:
                return False
            del section_map[part]
            self._write(new_data)
            self._data = new_data
        return True

    def clear(self) -> None:
        """Remove every mapping, leaving a valid empty history file."""
        with self._lock:
            new_data = _empty_history()
            self._write(new_data)
            self._data = new_data

    def _write(self, data: Mapping[str, Any]) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(parent)
        )
        os.close(fd)
        try:
            # Re-open by path. os.fdopen() triggers an audit "open" event with
            # no target, which Orca denies instead of applying the plugin's
            # allowed-root shortcut for the storage folder.
            with open(temp_name, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        # Best-effort directory fsync so the rename is durable on POSIX.
        try:
            dir_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


def _empty_history() -> dict[str, Any]:
    return {"version": 1, "spools": {}, "tags": {}, "filaments": {}}


def _lookup_history(history: Any, obs: Observation, printer: str) -> str:
    if history is None:
        return ""
    if isinstance(history, HistoryStore):
        return history.get(obs, printer)
    if isinstance(history, Mapping):
        key = identity_key(obs)
        section, part = _split_identity(key)
        section_map = history.get(section)
        if isinstance(section_map, Mapping):
            name = _entry_name(section_map.get(part), printer)
            if name:
                return name
        return _entry_name(history.get(key), printer)
    getter = getattr(history, "get", None)
    if callable(getter):
        try:
            value = getter(obs, printer)
        except TypeError:
            value = getter(obs)
        return value.strip() if isinstance(value, str) else ""
    return ""


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _compatible(presets: Iterable[PresetInfo]) -> list[PresetInfo]:
    return [preset for preset in presets if bool(getattr(preset, "compatible", False))]


def _is_price_copy(preset: Any) -> bool:
    """True for the plugin's ``<name> (local)`` price-copy presets."""
    name = strip_bundle_prefix(getattr(preset, "name", ""))
    return bool(re.search(r"\(local\)\s*$", name, re.I))


def _exact_match_sort_key(preset: PresetInfo) -> tuple[int, int, int, str, str]:
    """Prefer the base preset when the reading has no subtype.

    An exact vendor/material match with an unknown subtype can hit every
    variant of the same filament. "Snapmaker PLA" is a better default than
    "Snapmaker PLA Basic" or "Snapmaker PLA SnapSpeed" until the user chooses.
    """
    variant = normalize_text(getattr(preset, "subtype", "")) or normalize_text(
        _name_without_vendor_material(preset)
    )
    return (
        0 if _is_price_copy(preset) else 1,
        0 if not variant else 1,
        _preset_rank(preset),
        normalize_text(getattr(preset, "name", "")),
        _core_text(preset.name),
    )


def _preset_sort_key(preset: PresetInfo) -> tuple[int, int, str, str]:
    return (
        0 if _is_price_copy(preset) else 1,
        _preset_rank(preset),
        normalize_text(getattr(preset, "name", "")),
        _core_text(preset.name),
    )


def _candidates_for(
    matches: list[PresetInfo], source: str, score: int, reasons: list[str]
) -> list[Candidate]:
    return [
        Candidate(
            preset_name=preset.name,
            score=score,
            reasons=list(reasons),
            source=source,
            preset=preset,
        )
        for preset in matches
    ]


def _selected_result(
    obs: Observation,
    preset: PresetInfo,
    source: str,
    score: int,
    reasons: list[str],
) -> MatchResult:
    return MatchResult(
        observation=obs,
        preset_name=preset.name,
        source=source,
        confidence=score,
        candidates=_candidates_for([preset], source, score, reasons),
    )


def _multi_result(
    obs: Observation,
    matches: list[PresetInfo],
    source: str,
    confidence: int,
    reasons_by_preset: Mapping[str, list[str]] | None = None,
    score_by_preset: Mapping[str, int] | None = None,
    default_reasons: list[str] | None = None,
) -> MatchResult:
    candidates = []
    for preset in matches:
        name = preset.name
        reasons = (reasons_by_preset or {}).get(name, default_reasons or [])
        score = (score_by_preset or {}).get(name, confidence)
        candidates.append(
            Candidate(name, score, list(reasons), source, preset)
        )
    return MatchResult(
        observation=obs,
        preset_name="",
        source=source,
        confidence=confidence,
        candidates=candidates,
    )


def _metadata_reasons(obs: Observation) -> list[str]:
    reasons = []
    for label, attr in (
        ("vendor", "vendor"),
        ("material", "material"),
        ("subtype", "subtype"),
        ("product", "product"),
    ):
        value = _core_text(getattr(obs, attr, "")).strip()
        if value:
            reasons.append(f"{label} {value}")
    return reasons or ["metadata match"]


def _exact_metadata(obs: Observation, preset: PresetInfo) -> bool:
    observed_vendor = normalize_vendor(getattr(obs, "vendor", ""))
    observed_material = normalize_material(getattr(obs, "material", ""))
    if not observed_vendor or not observed_material:
        return False
    if observed_vendor != normalize_vendor(getattr(preset, "vendor", "")):
        return False
    if observed_material != normalize_material(getattr(preset, "material", "")):
        return False
    observed_subtype = normalize_text(getattr(obs, "subtype", ""))
    if observed_subtype:
        subtype_level, _reason = _subtype_match(obs, preset)
        if subtype_level < 2:
            return False
    observed_product = _observed_product_text(obs)
    if observed_product and not _products_match(observed_product, preset):
        return False
    return True


def _history_matches_observation(obs: Observation, preset: PresetInfo) -> bool:
    """Whether a confirmed spool mapping still fits the current reading.

    The spool ID is stable, but its contents are not: a refilled spool can
    change material, subtype, product or colour. A stale mapping must fall
    through to normal matching instead of blocking the user's edits.
    """
    observed_vendor = normalize_vendor(getattr(obs, "vendor", ""))
    if observed_vendor and observed_vendor != normalize_vendor(getattr(preset, "vendor", "")):
        return False
    observed_material = normalize_material(getattr(obs, "material", ""))
    if observed_material and observed_material != normalize_material(getattr(preset, "material", "")):
        return False
    observed_subtype = normalize_text(getattr(obs, "subtype", ""))
    if observed_subtype:
        subtype_level, _reason = _subtype_match(obs, preset)
        if subtype_level < 2:
            return False
    else:
        # With no reported subtype, the product is the only field that can
        # distinguish e.g. a plain PLA spool from a SnapSpeed spool. Only
        # enforce it when the history preset actually names a variant; a base
        # preset remains valid for colour-only Spoolman names such as "Black".
        observed_product = _core_text(getattr(obs, "product", "")).strip()
        configured_subtype = normalize_text(getattr(preset, "subtype", ""))
        name_subtype = normalize_text(_name_without_vendor_material(preset))
        if observed_product and (configured_subtype or name_subtype):
            if not _products_match(observed_product, preset):
                return False
    distance = color_distance(
        getattr(obs, "color", ""), getattr(preset, "color", "")
    )
    if distance is not None and distance > 100:
        return False
    return True


def _coerce_score(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def _normalized_library_candidates(value: Any) -> list[Candidate]:
    if value is None:
        return []
    if isinstance(value, Candidate):
        items = [value]
    elif isinstance(value, Mapping) or hasattr(value, "preset_name"):
        items = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        items = list(value)
    else:
        items = [value]

    result: list[Candidate] = []
    known = {
        "preset_name",
        "profile_name",
        "name",
        "score",
        "reasons",
        "preset",
        "library_url",
        "url",
        "path",
        "library_meta",
        "meta",
        "metadata",
    }
    for item in items:
        if item is None:
            continue
        if isinstance(item, Candidate):
            preset = item.preset
            name = item.preset_name or (preset.name if preset is not None else "")
            meta = dict(item.library_meta or {})
            result.append(
                Candidate(
                    preset_name=name,
                    score=_coerce_score(item.score),
                    reasons=list(item.reasons) or ["library candidate"],
                    source="library",
                    preset=preset,
                    library_url=item.library_url,
                    library_meta=meta,
                )
            )
            continue
        if isinstance(item, Mapping):
            preset = item.get("preset")
            name = (
                item.get("preset_name")
                or item.get("profile_name")
                or item.get("name")
                or (getattr(preset, "name", "") if preset is not None else "")
            )
            meta = item.get("library_meta") or item.get("meta") or item.get("metadata")
            if not isinstance(meta, Mapping):
                meta = {key: val for key, val in item.items() if key not in known}
            reasons = item.get("reasons") or []
            if isinstance(reasons, str):
                reasons = [reasons]
            reasons = list(reasons) or ["library candidate"]
            result.append(
                Candidate(
                    preset_name=_core_text(name).strip(),
                    score=_coerce_score(item.get("score")),
                    reasons=reasons,
                    source="library",
                    preset=preset if isinstance(preset, PresetInfo) else None,
                    library_url=_core_text(
                        item.get("library_url") or item.get("url") or item.get("path") or ""
                    ).strip(),
                    library_meta=dict(meta),
                )
            )
            continue
        name = getattr(item, "preset_name", "") or getattr(item, "name", "")
        url = getattr(item, "library_url", "") or getattr(item, "url", "")
        meta = getattr(item, "library_meta", {})
        preset = item if isinstance(item, PresetInfo) else getattr(item, "preset", None)
        reasons = list(getattr(item, "reasons", []) or []) or ["library candidate"]
        result.append(
            Candidate(
                preset_name=_core_text(name).strip(),
                score=_coerce_score(getattr(item, "score", 0)),
                reasons=reasons,
                source="library",
                preset=preset if isinstance(preset, PresetInfo) else None,
                library_url=_core_text(url).strip(),
                library_meta=dict(meta) if isinstance(meta, Mapping) else {},
            )
        )

    result = [candidate for candidate in result if candidate.preset_name or candidate.library_url]
    result.sort(key=lambda candidate: (-candidate.score, normalize_text(candidate.preset_name)))
    return result


def _generic_matches(obs: Observation, installed: list[PresetInfo]) -> list[PresetInfo]:
    material = normalize_material(getattr(obs, "material", ""))
    if not material:
        return []
    expected = "generic " + normalize_material(getattr(obs, "material", ""))
    exact: list[tuple[float, int, str, str, PresetInfo]] = []
    prefixed: list[tuple[float, int, str, str, PresetInfo]] = []
    material_only: list[tuple[float, int, str, str, PresetInfo]] = []
    for preset in installed:
        if not _is_generic(preset):
            continue
        preset_material = normalize_material(getattr(preset, "material", ""))
        name = normalize_text(
            strip_printer_suffix(strip_bundle_prefix(getattr(preset, "name", "")))
        )
        distance = color_distance(
            getattr(obs, "color", ""), getattr(preset, "color", "")
        )
        sort_distance = distance if distance is not None else float("inf")
        row = (
            sort_distance,
            _preset_rank(preset),
            normalize_text(preset.name),
            preset.name,
            preset,
        )
        if name == expected:
            exact.append(row)
        elif name.startswith(expected + " "):
            prefixed.append(row)
        elif preset_material == material:
            material_only.append(row)
    pool = exact or prefixed or material_only
    pool.sort(key=lambda row: row[:4])
    return [row[4] for row in pool]


def match_observation(
    obs: Observation,
    presets: Iterable[PresetInfo],
    history: HistoryStore | Mapping[str, Any] | None = None,
    printer: str = "",
    library_lookup: Any = None,
) -> MatchResult:
    """Run the matching pipeline. This function never writes history."""
    preset_list = [preset for preset in presets if preset is not None]
    installed = sorted(_compatible(preset_list), key=_preset_sort_key)
    printer_name = _core_text(printer).strip()

    # 1. User-confirmed history, provided the preset is still present/compatible.
    history_name = _lookup_history(history, obs, printer_name)
    if history_name:
        wanted = history_name.strip()
        by_name = [p for p in preset_list if _core_text(p.name).strip() == wanted]
        if not by_name:
            folded = wanted.casefold()
            by_name = [p for p in preset_list if _core_text(p.name).strip().casefold() == folded]
        compatible = [
            preset for preset in sorted(_compatible(by_name), key=_preset_sort_key)
            if _history_matches_observation(obs, preset)
        ]
        if compatible:
            reasons = ["confirmed history"]
            if printer_name:
                reasons.append(f"printer {printer_name}")
            if len(compatible) == 1:
                return _selected_result(obs, compatible[0], "history", 100, reasons)
            return _multi_result(
                obs, compatible, "history", 100, default_reasons=reasons
            )

    # 2. Exact printer-reported filament_id.
    observed_id = _core_text(getattr(obs, "filament_id", "")).strip()
    if observed_id:
        matches = [
            p
            for p in installed
            if _core_text(getattr(p, "filament_id", "")).strip() == observed_id
        ]
        if matches:
            reasons = [f"filament_id {observed_id}"]
            if len(matches) == 1:
                return _selected_result(obs, matches[0], "installed", 100, reasons)
            return _multi_result(obs, matches, "installed", 100)

    # 3. Exact vendor + material + subtype + product (blank obs parts unknown).
    matches = [p for p in installed if _exact_metadata(obs, p)]
    if matches:
        if not normalize_text(getattr(obs, "subtype", "")):
            matches.sort(key=_exact_match_sort_key)
        reasons = _metadata_reasons(obs)
        if len(matches) == 1:
            return _selected_result(obs, matches[0], "installed", 95, reasons)
        return _multi_result(
            obs, matches, "installed", 95, default_reasons=reasons
        )

    # 4. Fuzzy score: threshold, then margin over the next qualifying candidate.
    scored: list[tuple[int, list[str], PresetInfo]] = []
    for preset in installed:
        if _is_generic(preset):
            continue
        score, reasons = score_preset(obs, preset)
        if score > 0:
            scored.append((score, reasons, preset))
    scored.sort(
        key=lambda row: (
            -row[0],
            0 if _is_price_copy(row[2]) else 1,
            _preset_rank(row[2]),
            normalize_text(row[2].name),
            row[2].name,
        )
    )
    qualified = [row for row in scored if row[0] >= FUZZY_THRESHOLD]
    if qualified:
        top_score = qualified[0][0]
        if len(qualified) == 1 or top_score - qualified[1][0] >= FUZZY_MARGIN:
            return _selected_result(
                obs, qualified[0][2], "installed", top_score, qualified[0][1]
            )
        reasons_by_preset = {row[2].name: row[1] for row in qualified}
        scores_by_preset = {row[2].name: row[0] for row in qualified}
        return _multi_result(
            obs,
            [row[2] for row in qualified],
            "installed",
            top_score,
            reasons_by_preset,
            scores_by_preset,
        )

    # 5. Library lookup: candidates for review, never selected/installed here.
    if callable(library_lookup):
        try:  # A dead library must not block the generic fallback.
            library = _normalized_library_candidates(library_lookup(obs))
        except Exception:
            library = []
        if library:
            return MatchResult(
                observation=obs,
                source="library",
                confidence=max(candidate.score for candidate in library),
                candidates=library,
            )

    # 6. Generic material fallback. Never recorded as history by this function.
    generic_matches = _generic_matches(obs, installed)
    if generic_matches:
        reasons = [f"generic {_core_text(getattr(obs, 'material', '')).strip()} fallback"]
        best = generic_matches[0]

        def tie_key(preset: PresetInfo) -> tuple[float, int]:
            distance = color_distance(
                getattr(obs, "color", ""), getattr(preset, "color", "")
            )
            return (
                distance if distance is not None else float("inf"),
                _preset_rank(preset),
            )

        best_distance = tie_key(best)[0]
        if best_distance != float("inf"):
            reasons.append(f"colour Δ{best_distance:.0f}")
        tied = [preset for preset in generic_matches if tie_key(preset) == tie_key(best)]
        if len(tied) == 1:
            return _selected_result(obs, best, "generic", 30, reasons)
        return _multi_result(
            obs, tied, "generic", 30, default_reasons=reasons
        )

    # 7. Nothing matched.
    material = _core_text(getattr(obs, "material", "")).strip()
    error = f"{NO_MATCH_ERROR}: {material}" if material else NO_MATCH_ERROR
    return MatchResult(observation=obs, error=error)
