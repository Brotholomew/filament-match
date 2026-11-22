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
``filament:<sha1(vendor|material|subtype|product|color)>``. Normalization is
Unicode-safe (NFKD, combining marks removed, trademark marks dropped), so
non-ASCII vendors cannot collapse into one empty identity. History schema
version 2 stores those keys under the ``spools`` / ``tags`` / ``filaments``
sections with a ``default`` and per-printer names, writes atomically, and
reads pre-fix keys as a legacy fallback without writing them again.

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
    "material_family",
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
_HISTORY_VERSION = 2
_TRADEMARK_MARKS = str.maketrans("", "", "\u2122\u00ae\u00a9")
_TAG_PLACEHOLDERS = frozenset({"", "0", "NA", "NONE", "NULL", "UNKNOWN"})
_GENERIC_VARIANT_TOKENS = frozenset(
    {"cf", "gf", "silk", "matte", "hf", "snapspeed", "basic"}
)


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
    """Unicode-safe casefold/normalization for vendors, materials and names.

    NFKD decomposition plus combining-mark removal makes accented Latin,
    Greek and Cyrillic names comparable, while Unicode letters/numbers are
    retained (CJK included). Trademark marks are removed instead of becoming
    a fake ``tm`` token. ``+``/``&`` keep their historical expansion so the
    existing identity format and product matching stay stable.
    """
    text = _core_text(value).translate(_TRADEMARK_MARKS)
    text = text.replace("+", " plus ").replace("&", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        character for character in text
        if not unicodedata.combining(character)
    )
    text = text.casefold()
    text = "".join(
        character if (character.isalnum() or character.isspace()) else " "
        for character in text
    )
    return " ".join(text.split())


def _canonical_material_words(text: str) -> str:
    """Canonicalize material families after :func:`normalize_text`."""
    text = re.sub(r"\bpet\s+g\b", "petg", text)
    text = re.sub(r"\bpla\s+plus\b", "pla", text)
    text = re.sub(r"\bnylon\b", "pa", text)
    return text


def _identity_text(value: Any, normalizer) -> str:
    """Normalized identity component, retaining raw value if it normalizes empty."""
    normalized = normalizer(value)
    if normalized:
        return normalized
    raw = _core_text(value).strip().casefold()
    return f"raw:{raw}" if raw else ""


def _vendor_token(value: Any) -> str:
    """Vendor comparison token that cannot silently become empty."""
    return _identity_text(value, normalize_vendor)


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
    # Only known provenance markers may be removed. A leading parenthetical
    # or bracket is part of the product name ("(Matte) eSUN PETG") unless it
    # is the OFD marker.
    text = re.sub(r"^\s*\[OFD\]\s*", "", text, flags=re.I)
    text = re.sub(r"^(?:bundle|local|user|system)\s*:\s*", "", text, flags=re.I)
    return text.strip()


def normalize_vendor(value: Any) -> str:
    return normalize_text(strip_bundle_prefix(value))


def normalize_material(value: Any) -> str:
    """Canonical material family + optional variant/hardness.

    ``PLA+``/``PLA Plus`` collapse to ``PLA``; ``PET-G`` to ``PETG``;
    ``Nylon`` to ``PA``; ``PLA-CF`` becomes ``pla cf`` and ``TPU-95A``
    becomes ``tpu 95a`` so the family and detail stay usable for exact
    matching, generic lookup and identity.
    """
    return _canonical_material_words(normalize_text(value))


def material_family(value: Any) -> str:
    """The base material family: ``pla cf`` → ``pla``, ``tpu 95a`` → ``tpu``."""
    material = normalize_material(value)
    return material.split(" ", 1)[0] if material else ""


def _is_material_variant_token(token: str) -> bool:
    """CF/GF and hardness tokens that must be confirmed by evidence."""
    return token in {"cf", "gf"} or bool(
        re.fullmatch(r"\d{2,3}[ad]", token)
    )


def _material_variant_tokens_from_text(value: Any) -> set[str]:
    return {
        token for token in normalize_text(value).split()
        if _is_material_variant_token(token)
    }


def _material_variant_tokens_from_material(value: Any) -> set[str]:
    parts = normalize_material(value).split()
    return {
        token for token in parts[1:]
        if _is_material_variant_token(token)
    }


def _observation_variant_tokens(obs: Observation) -> set[str]:
    tokens = _material_variant_tokens_from_material(
        getattr(obs, "material", "")
    )
    tokens.update(_material_variant_tokens_from_text(
        getattr(obs, "subtype", "")
    ))
    tokens.update(_material_variant_tokens_from_text(
        getattr(obs, "product", "")
    ))
    return tokens


def _preset_variant_tokens(preset: Any) -> set[str]:
    tokens = _material_variant_tokens_from_material(
        getattr(preset, "material", "")
    )
    tokens.update(_material_variant_tokens_from_text(
        getattr(preset, "subtype", "")
    ))
    tokens.update(_material_variant_tokens_from_text(
        _name_without_vendor_material(preset)
    ))
    return tokens


def _material_variants_match(obs: Observation, preset: Any) -> bool:
    """CF/GF/hardness evidence must agree in both directions."""
    return _observation_variant_tokens(obs) == _preset_variant_tokens(preset)


def _parse_hex_color(value: Any) -> tuple[int, int, int] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if 0 <= value <= 0xFFFFFF:
            return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
        if 0 <= value <= 0xFFFFFFFF:
            # Klipper/Snapmaker call the field RGBA, so alpha is last.
            return (
                (value >> 24) & 0xFF,
                (value >> 16) & 0xFF,
                (value >> 8) & 0xFF,
            )
        return None

    raw_text = _core_text(value).strip()
    text = raw_text.lower()
    prefixed = False
    if text.startswith("#"):
        text = text[1:]
        prefixed = True
    elif text.startswith("0x"):
        text = text[2:]
        prefixed = True
    if not text or not _HEX_RE.match(text):
        return None
    # A bare lowercase six/eight-letter token such as "facade" is a word, not
    # a colour. Uppercase forms and forms containing digits remain accepted
    # because printers emit raw RRGGBB/RRGGBBAA without a prefix.
    if (
        not prefixed
        and len(text) in (6, 8)
        and text.isalpha()
        and raw_text.islower()
    ):
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
    """Normalize RFID UIDs: uppercase, separators removed.

    All-zero values and the usual sentinel strings (Orca uses
    ``0000000000000000`` when a tray has no tag) are not identities. All-F
    values of realistic UID length are also treated as placeholders.
    """
    text = re.sub(r"[^0-9A-Za-z]", "", _core_text(value)).upper()
    if not text or text in _TAG_PLACEHOLDERS:
        return ""
    if set(text) == {"0"}:
        return ""
    if len(text) >= 8 and set(text) == {"F"}:
        return ""
    return text


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
    # Never persist ``repr(preset)`` for a malformed/unrecognised object.
    # An empty name makes remember() fail loudly instead of writing garbage.
    return ""


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
    result = stripped or raw
    if result:
        return result
    # Non-empty text that normalization drops (e.g. only trademark marks)
    # must stay comparable instead of becoming "unknown".
    text = _core_text(value).strip().casefold()
    return f"raw:{text}" if text else ""


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

    Spoolman's filament name is often only a colour ("White", "Yellow"), a
    colour plus the material ("PETG Pink"), or the material plus a grade word
    ("PLA Basic"). None of those identify a product line, so vendor and
    material tokens are removed alongside colour words and grade qualifiers.
    A name with an actual extra token ("ABS Pastel", "PETG Matte") still
    constrains it. Policy: ``PLA Basic`` is a grade label, not a product line;
    a reported subtype Basic is still enforced by the subtype check.
    """
    raw = getattr(obs, "product", "")
    text = strip_color_words(raw)
    if not text:
        # A non-empty product that normalizes to nothing must still constrain
        # the history check rather than silently becoming "unknown".
        if _core_text(raw).strip() and not normalize_text(raw):
            return _core_text(raw).strip().casefold()
        return ""
    subtype_tokens = set(normalize_text(getattr(obs, "subtype", "")).split())
    tokens = [
        token for token in text.split()
        if token not in _PRODUCT_QUALIFIERS or token in subtype_tokens
    ]
    if not tokens:
        return ""
    # If nothing remains after removing the vendor/material/grade tokens, the
    # name was only a colour or material label and must not constrain a match.
    metadata_tokens: set[str] = set()
    metadata_tokens.update(
        normalize_material(getattr(obs, "material", "")).split()
    )
    metadata_tokens.update(normalize_text(getattr(obs, "material", "")).split())
    metadata_tokens.update(
        normalize_vendor(getattr(obs, "vendor", "")).split()
    )
    if not any(token not in metadata_tokens for token in tokens):
        return ""
    return " ".join(tokens)


def _is_color_only_product(value: Any) -> bool:
    tokens = normalize_text(value).split()
    return bool(tokens) and all(token in _COLOR_WORDS for token in tokens)


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


def _generic_name_text(preset: Any) -> str:
    """Normalized preset name with material spelling aliases applied."""
    return _canonical_material_words(normalize_text(
        strip_printer_suffix(strip_bundle_prefix(getattr(preset, "name", "")))
    ))


def _is_generic(preset: Any) -> bool:
    name = _generic_name_text(preset)
    if name == "generic" or name.startswith("generic "):
        return True
    return normalize_vendor(getattr(preset, "vendor", "")) == "generic"


def _generic_residual_tokens(name: str, material: str) -> list[str]:
    """Tokens left after removing ``generic`` and observed material tokens."""
    tokens = name.split()
    if tokens and tokens[0] == "generic":
        tokens = tokens[1:]
    remaining = list(tokens)
    for material_token in material.split():
        if material_token in remaining:
            remaining.remove(material_token)
    return remaining


def _generic_variant_supported(obs: Observation, residual: Iterable[str]) -> bool:
    """Whether the observation explicitly supports generic variant tokens."""
    evidence = " ".join(
        part for part in (
            normalize_text(getattr(obs, "subtype", "")),
            normalize_text(getattr(obs, "product", "")),
            normalize_text(getattr(obs, "material", "")),
            normalize_material(getattr(obs, "material", "")),
        ) if part
    )
    evidence_tokens = set(evidence.split())
    return all(
        token not in _GENERIC_VARIANT_TOKENS or token in evidence_tokens
        for token in residual
    )


def _name_prefix_score(obs: Observation, preset: Any) -> int:
    name = normalize_text(
        strip_printer_suffix(strip_bundle_prefix(getattr(preset, "name", "")))
    )
    if not name:
        return 0
    candidates = (
        _vendor_token(getattr(obs, "vendor", "")),
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
    if not configured:
        # Sources normally split base material and subtype, but some presets
        # store the variant in the material field ("PLA-CF", "TPU-95A").
        configured = " ".join(
            normalize_material(getattr(preset, "material", "")).split()[1:]
        )
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

    observed_family = material_family(getattr(obs, "material", ""))
    preset_family = material_family(getattr(preset, "material", ""))
    if observed_family and preset_family and observed_family != preset_family:
        # Material family mismatch is a veto, not a reduced score. Variants
        # such as CF/GF/hardness are constrained by subtype/name checks.
        return 0, []
    if observed_family and observed_family == preset_family:
        score += 30
        reasons.append(f"material {_core_text(getattr(obs, 'material', '')).strip()}")

    observed_vendor = _vendor_token(getattr(obs, "vendor", ""))
    preset_vendor = _vendor_token(getattr(preset, "vendor", ""))
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


def _filament_identity_key(obs: Observation) -> str:
    """Fingerprint identity, independent of any spool/tag identifiers."""
    color = normalize_color(getattr(obs, "color", ""))
    if not color:
        color = normalize_text(getattr(obs, "color", "")).replace(" ", "")
    payload = "|".join(
        (
            _identity_text(getattr(obs, "vendor", ""), normalize_vendor),
            _identity_text(getattr(obs, "material", ""), normalize_material),
            _identity_text(getattr(obs, "subtype", ""), normalize_text),
            _identity_text(getattr(obs, "product", ""), normalize_text),
            color.lstrip("#"),
        )
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"filament:{digest}"


def identity_key(obs: Observation) -> str:
    """Return the stable identity used by history, in the documented order."""
    spool_id = getattr(obs, "spool_id", None)
    if spool_id is not None and _core_text(spool_id).strip():
        return f"spool:{_core_text(spool_id).strip()}"

    tag = normalize_tag_uid(getattr(obs, "tag_uid", ""))
    if tag:
        return f"tag:{tag}"

    return _filament_identity_key(obs)


def _identity_keys(obs: Observation) -> list[str]:
    """All history identities for an observation, highest priority first."""
    keys: list[str] = []
    spool_id = getattr(obs, "spool_id", None)
    if spool_id is not None and _core_text(spool_id).strip():
        keys.append(f"spool:{_core_text(spool_id).strip()}")
    tag = normalize_tag_uid(getattr(obs, "tag_uid", ""))
    if tag:
        keys.append(f"tag:{tag}")
    filament = _filament_identity_key(obs)
    if filament not in keys:
        keys.append(filament)
    return keys


def _legacy_normalize_text(value: Any) -> str:
    """The pre-Unicode-fix normalization, used only for old history keys."""
    text = unicodedata.normalize("NFKC", _core_text(value))
    text = text.replace("+", " plus ").replace("&", " and ")
    text = text.casefold()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return " ".join(text.split())


def _legacy_strip_bundle_prefix(value: Any) -> str:
    text = _core_text(value).strip()
    text = re.sub(r"^_(?:local|subscribed)/[^/]+/", "", text, flags=re.I)
    text = re.sub(r"^\s*\[[^\]]*\]\s*", "", text)
    text = re.sub(r"^\s*\([^)]*\)\s*", "", text)
    text = re.sub(r"^(?:bundle|local|user|system)\s*[:\-]\s*", "", text, flags=re.I)
    return text.strip()


def _legacy_normalize_vendor(value: Any) -> str:
    return _legacy_normalize_text(_legacy_strip_bundle_prefix(value))


def _legacy_normalize_material(value: Any) -> str:
    text = _legacy_normalize_text(value)
    return re.sub(r"\bpet\s+g\b", "petg", text)


def _legacy_filament_key(obs: Observation) -> str:
    """The filament fingerprint an older plugin version wrote."""
    color = normalize_color(getattr(obs, "color", ""))
    if not color:
        color = _legacy_normalize_text(
            getattr(obs, "color", "")
        ).replace(" ", "")
    payload = "|".join(
        (
            _legacy_normalize_vendor(getattr(obs, "vendor", "")),
            _legacy_normalize_material(getattr(obs, "material", "")),
            _legacy_normalize_text(getattr(obs, "subtype", "")),
            _legacy_normalize_text(getattr(obs, "product", "")),
            color.lstrip("#"),
        )
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"filament:{digest}"


def _legacy_identity_key(obs: Observation) -> str:
    """Compute the key an older plugin version wrote for this observation."""
    spool_id = getattr(obs, "spool_id", None)
    if spool_id is not None and _core_text(spool_id).strip():
        return f"spool:{_core_text(spool_id).strip()}"
    # Keep the placeholder rejection: an all-zero old tag entry must not
    # reappear as a real identity.
    legacy_tag = normalize_tag_uid(getattr(obs, "tag_uid", ""))
    if legacy_tag:
        return f"tag:{legacy_tag}"
    return _legacy_filament_key(obs)


def _history_keys(obs: Observation) -> list[str]:
    keys = _identity_keys(obs)
    legacy = _legacy_filament_key(obs)
    if legacy not in keys:
        keys.append(legacy)
    return keys


def _split_identity(key: str) -> tuple[str, str]:
    if key.startswith("spool:"):
        return "spools", key[len("spool:") :]
    if key.startswith("tag:"):
        return "tags", normalize_tag_uid(key[len("tag:") :])
    if key.startswith("filament:"):
        return "filaments", key[len("filament:") :]
    return "filaments", key


def _entry_names(entry: Any, printer: str) -> list[str]:
    """Printer-specific name first, then the entry's default; deduplicated."""
    names: list[str] = []
    if isinstance(entry, str):
        value = entry.strip()
        if value:
            names.append(value)
    elif isinstance(entry, Mapping):
        if printer:
            printers = entry.get("printers")
            if isinstance(printers, Mapping):
                value = printers.get(printer)
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
        default = entry.get("default")
        if isinstance(default, str) and default.strip():
            names.append(default.strip())
    result: list[str] = []
    for name in names:
        if name not in result:
            result.append(name)
    return result


def _entry_name(entry: Any, printer: str) -> str:
    names = _entry_names(entry, printer)
    return names[0] if names else ""


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
            # utf-8-sig accepts a BOM and is identical to utf-8 otherwise.
            with open(os.fspath(self.path), "r", encoding="utf-8-sig") as stream:
                text = stream.read()
        except FileNotFoundError:
            return
        except (OSError, UnicodeError) as exc:
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

    def _names_for_keys(self, keys: Iterable[str], printer: str) -> list[str]:
        names: list[str] = []
        printer_name = _core_text(printer).strip()
        for key in keys:
            section, part = _split_identity(key)
            section_map = self._data.get(section)
            if not isinstance(section_map, Mapping):
                continue
            for name in _entry_names(section_map.get(part), printer_name):
                if name not in names:
                    names.append(name)
        return names

    def get(self, obs: Observation | str, printer: str = "") -> str:
        """Return the printer-specific mapping, else the default (or ``""``).

        A miss on the current Unicode-safe identity falls back to the key the
        pre-0.9 plugin wrote. New entries are always written under the new key.
        """
        key = obs if isinstance(obs, str) else identity_key(obs)
        keys = [key]
        if not isinstance(obs, str):
            legacy = _legacy_identity_key(obs)
            if legacy != key:
                keys.append(legacy)
        with self._lock:
            names = self._names_for_keys(keys, printer)
        return names[0] if names else ""

    def lookup(self, obs: Observation, printer: str = "") -> list[str]:
        """All matching history names for an observation, in priority order.

        Cascade spool → tag → filament, then the legacy filament key. Within
        one identity the printer-specific entry comes before its default so a
        stale printer-specific preset can fall back to a valid default.
        """
        printer_name = _core_text(printer).strip()
        with self._lock:
            return self._names_for_keys(_history_keys(obs), printer_name)

    def remember(
        self,
        obs: Observation,
        printer: str,
        preset: str | PresetInfo | Candidate,
    ) -> str:
        """Persist a user-confirmed mapping and return the identity key."""
        if self.load_error:
            raise RuntimeError(
                "The history file could not be read; refusing to overwrite it: %s"
                % self.load_error)
        name = _preset_name(preset)
        if not name:
            raise ValueError("preset name is required")
        key = identity_key(obs)
        section, part = _split_identity(key)
        printer_name = _core_text(printer).strip()

        with self._lock:
            new_data = copy.deepcopy(self._data)
            new_data["version"] = _HISTORY_VERSION
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
            self.load_error = ""
        return key

    def forget(self, obs: Observation | str) -> bool:
        """Remove one identity entirely. Returns whether anything was deleted."""
        key = obs if isinstance(obs, str) else identity_key(obs)
        keys = [key]
        if not isinstance(obs, str):
            legacy = _legacy_identity_key(obs)
            if legacy != key:
                keys.append(legacy)
        with self._lock:
            new_data = copy.deepcopy(self._data)
            removed = False
            for candidate in keys:
                section, part = _split_identity(candidate)
                section_map = new_data.get(section)
                if isinstance(section_map, dict) and part in section_map:
                    del section_map[part]
                    removed = True
            if not removed:
                return False
            new_data["version"] = _HISTORY_VERSION
            self._write(new_data)
            self._data = new_data
            self.load_error = ""
        return True

    def clear(self) -> None:
        """Remove every mapping, leaving a valid empty history file."""
        with self._lock:
            new_data = _empty_history()
            self._write(new_data)
            self._data = new_data
            self.load_error = ""

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
    return {"version": _HISTORY_VERSION, "spools": {}, "tags": {}, "filaments": {}}


def _history_names(history: Any, obs: Observation, printer: str) -> list[str]:
    """Ordered history names: spool → tag → filament, printer then default."""
    if history is None:
        return []
    if isinstance(history, HistoryStore):
        lookup = getattr(history, "lookup", None)
        if callable(lookup):
            return lookup(obs, printer)
        value = history.get(obs, printer)
        return [value] if value else []
    if isinstance(history, Mapping):
        names: list[str] = []
        for key in _history_keys(obs):
            section, part = _split_identity(key)
            section_map = history.get(section)
            if isinstance(section_map, Mapping):
                for name in _entry_names(section_map.get(part), printer):
                    if name not in names:
                        names.append(name)
            direct = history.get(key)
            for name in _entry_names(direct, printer):
                if name not in names:
                    names.append(name)
        return names
    getter = getattr(history, "get", None)
    if callable(getter):
        try:
            value = getter(obs, printer)
        except TypeError:
            value = getter(obs)
        return [value.strip()] if isinstance(value, str) and value.strip() else []
    return []


def _lookup_history(history: Any, obs: Observation, printer: str) -> str:
    names = _history_names(history, obs, printer)
    return names[0] if names else ""


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _as_bool(value: Any) -> bool:
    """Boolean coercion that treats ``"false"``/``"0"`` as false."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() not in {
            "", "0", "false", "no", "off", "none", "null",
        }
    return bool(value)


def _compatible(presets: Iterable[PresetInfo]) -> list[PresetInfo]:
    return [
        preset for preset in presets
        if _as_bool(getattr(preset, "compatible", False))
    ]


def _dedupe_presets(presets: Iterable[PresetInfo]) -> list[PresetInfo]:
    """Drop presets that are indistinguishable (name, bundle and file)."""
    result: list[PresetInfo] = []
    seen: set[tuple[str, str, str]] = set()
    for preset in presets:
        name = _core_text(getattr(preset, "name", "")).strip()
        key = (
            name,
            _core_text(getattr(preset, "bundle_id", "")).strip(),
            _core_text(getattr(preset, "file", "")).strip(),
        )
        if name and key in seen:
            continue
        if name:
            seen.add(key)
        result.append(preset)
    return result


def _vendor_matches(obs: Observation, preset: Any) -> bool:
    observed = _vendor_token(getattr(obs, "vendor", ""))
    if not observed:
        return True
    preset_vendor = _vendor_token(getattr(preset, "vendor", ""))
    if preset_vendor:
        return observed == preset_vendor
    # A preset with no vendor config may still carry the brand in its name.
    name_tokens = normalize_text(
        strip_printer_suffix(strip_bundle_prefix(getattr(preset, "name", "")))
    ).split()
    wanted = observed.split()
    return bool(wanted) and name_tokens[: len(wanted)] == wanted


def _material_matches(obs: Observation, preset: Any) -> bool:
    observed_family = material_family(getattr(obs, "material", ""))
    if not observed_family:
        return True
    preset_family = material_family(getattr(preset, "material", ""))
    if not preset_family:
        return True
    if observed_family != preset_family:
        return False
    return _material_variants_match(obs, preset)


def _colour_veto(obs: Observation, preset: Any) -> bool:
    distance = color_distance(
        getattr(obs, "color", ""), getattr(preset, "color", "")
    )
    return distance is not None and distance > 100


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
    reasons_by_preset: Mapping[Any, list[str]] | None = None,
    score_by_preset: Mapping[Any, int] | None = None,
    default_reasons: list[str] | None = None,
) -> MatchResult:
    candidates = []
    reasons_map = reasons_by_preset or {}
    score_map = score_by_preset or {}
    for preset in matches:
        name = preset.name
        # Duplicate preset names are legal (different bundles/files). Prefer
        # the object identity key, then fall back to the name for callers
        # that still provide name-keyed diagnostic maps.
        reasons = reasons_map.get(id(preset))
        if reasons is None:
            reasons = reasons_map.get(name, default_reasons or [])
        score = score_map.get(id(preset))
        if score is None:
            score = score_map.get(name, confidence)
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
    observed_vendor = _vendor_token(getattr(obs, "vendor", ""))
    observed_family = material_family(getattr(obs, "material", ""))
    if not observed_vendor or not observed_family:
        return False
    if not _vendor_matches(obs, preset):
        return False
    if observed_family != material_family(getattr(preset, "material", "")):
        return False
    if not _material_variants_match(obs, preset):
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
    if not _vendor_matches(obs, preset):
        return False
    observed_family = material_family(getattr(obs, "material", ""))
    preset_family = material_family(getattr(preset, "material", ""))
    if observed_family:
        if observed_family != preset_family:
            return False
        # Without a reported material the confirmed history is still trusted;
        # with one, CF/GF/hardness must agree.
        if not _material_variants_match(obs, preset):
            return False
    observed_subtype = normalize_text(getattr(obs, "subtype", ""))
    if observed_subtype:
        subtype_level, _reason = _subtype_match(obs, preset)
        if subtype_level < 2:
            return False
    else:
        # With no reported subtype, the product is the only field that can
        # distinguish e.g. a plain PLA spool from a SnapSpeed spool. Use the
        # qualifier-aware product text so "PLA Basic" does not block a base
        # preset. A colour-only Spoolman name gives no evidence that a
        # variant-named history preset is still the same filament, so keep
        # the conservative variant check for that case.
        observed_product = _observed_product_text(obs)
        configured_subtype = normalize_text(getattr(preset, "subtype", ""))
        name_subtype = normalize_text(_name_without_vendor_material(preset))
        if observed_product and (configured_subtype or name_subtype):
            if not _products_match(observed_product, preset):
                return False
        elif _is_color_only_product(getattr(obs, "product", "")):
            if (configured_subtype or name_subtype) and not _products_match(
                getattr(obs, "product", ""), preset
            ):
                return False
    if _colour_veto(obs, preset):
        return False
    return True


def _coerce_score(value: Any) -> int:
    """Accept both 0–100 scores and 0.0–1.0 fractional scores."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return 0
            number = float(text)
            fractional = "." in text or "e" in text.casefold()
        elif isinstance(value, float):
            number = value
            fractional = True
        else:
            number = float(value)
            fractional = False
        if not math.isfinite(number):
            return 0
        if fractional and 0.0 <= number <= 1.0:
            number *= 100.0
        return max(0, min(100, int(round(number))))
    except (TypeError, ValueError, OverflowError):
        return 0


def _candidate_reasons(value: Any) -> list[str]:
    """Normalize candidate reasons: string → one item, None → empty."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Mapping):
        values = list(value.values())
    else:
        try:
            values = list(value)
        except TypeError:
            values = [value]
    return [str(item) for item in values if item is not None and str(item)]


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
            meta = item.library_meta
            result.append(
                Candidate(
                    preset_name=_core_text(name).strip(),
                    score=_coerce_score(item.score),
                    reasons=_candidate_reasons(item.reasons),
                    source="library",
                    preset=preset,
                    library_url=_core_text(item.library_url).strip(),
                    library_meta=dict(meta) if isinstance(meta, Mapping) else {},
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
            result.append(
                Candidate(
                    preset_name=_core_text(name).strip(),
                    score=_coerce_score(item.get("score")),
                    reasons=_candidate_reasons(item.get("reasons")),
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
        result.append(
            Candidate(
                preset_name=_core_text(name).strip(),
                score=_coerce_score(getattr(item, "score", 0)),
                reasons=_candidate_reasons(getattr(item, "reasons", None)),
                source="library",
                preset=preset if isinstance(preset, PresetInfo) else None,
                library_url=_core_text(url).strip(),
                library_meta=dict(meta) if isinstance(meta, Mapping) else {},
            )
        )

    deduped: dict[tuple[str, str], Candidate] = {}
    for candidate in result:
        if not (candidate.preset_name or candidate.library_url):
            continue
        key = (candidate.preset_name, candidate.library_url)
        existing = deduped.get(key)
        if existing is None or candidate.score > existing.score:
            deduped[key] = candidate
    result = list(deduped.values())
    result.sort(key=lambda candidate: (-candidate.score, normalize_text(candidate.preset_name)))
    return result


def _generic_matches(obs: Observation, installed: list[PresetInfo]) -> list[PresetInfo]:
    material = normalize_material(getattr(obs, "material", ""))
    if not material:
        return []
    expected = "generic " + material
    exact: list[tuple[float, int, str, str, PresetInfo]] = []
    prefixed: list[tuple[float, int, str, str, PresetInfo]] = []
    material_only: list[tuple[float, int, str, str, PresetInfo]] = []
    for preset in installed:
        if not _is_generic(preset):
            continue
        preset_material = normalize_material(getattr(preset, "material", ""))
        name = _generic_name_text(preset)
        residual = _generic_residual_tokens(name, material)
        # A generic variant (CF/GF/Silk/Matte/HF/SnapSpeed/Basic) must be
        # supported by the observation. This stops a plain PLA spool from
        # silently inheriting Generic PLA-CF just because it is the only
        # generic preset installed.
        if not _generic_variant_supported(obs, residual):
            continue
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
    # An exact base generic always outranks any variant.
    pool = exact or prefixed or material_only
    pool.sort(key=lambda row: row[:4])
    return [row[4] for row in pool]


def _filament_id_compatible(obs: Observation, preset: PresetInfo) -> bool:
    """An ID match still has to agree with the observed metadata."""
    if not _vendor_matches(obs, preset):
        return False
    observed_family = material_family(getattr(obs, "material", ""))
    if observed_family:
        preset_family = material_family(getattr(preset, "material", ""))
        if preset_family:
            if observed_family != preset_family:
                return False
            if not _material_variants_match(obs, preset):
                return False
    if _colour_veto(obs, preset):
        return False
    return True


def _filament_id_confidence(obs: Observation) -> int:
    """Corroborated ID matches are authoritative; an ID alone is not 100."""
    has_vendor = bool(_vendor_token(getattr(obs, "vendor", "")))
    has_material = bool(normalize_material(getattr(obs, "material", "")))
    if has_vendor and has_material:
        return 100
    if has_vendor or has_material or normalize_color(getattr(obs, "color", "")):
        return 95
    return 90


def _brand_evidence(obs: Observation, preset: PresetInfo) -> bool:
    """Strong product/subtype evidence for an unknown-vendor observation."""
    if normalize_text(getattr(obs, "subtype", "")):
        subtype_level, _reason = _subtype_match(obs, preset)
        if subtype_level == 2:
            return True
    product = _observed_product_text(obs)
    return bool(product) and _products_match(product, preset)


def match_observation(
    obs: Observation,
    presets: Iterable[PresetInfo],
    history: HistoryStore | Mapping[str, Any] | None = None,
    printer: str = "",
    library_lookup: Any = None,
) -> MatchResult:
    """Run the matching pipeline. This function never writes history."""
    preset_list = _dedupe_presets(
        [preset for preset in presets if preset is not None]
    )
    installed = sorted(_compatible(preset_list), key=_preset_sort_key)
    printer_name = _core_text(printer).strip()
    observed_vendor = _vendor_token(getattr(obs, "vendor", ""))

    # 1. User-confirmed history: spool → tag → filament, printer → default.
    for history_name in _history_names(history, obs, printer_name):
        wanted = history_name.strip()
        if not wanted:
            continue
        by_name = [p for p in preset_list if _core_text(p.name).strip() == wanted]
        if not by_name:
            folded = wanted.casefold()
            by_name = [
                p for p in preset_list
                if _core_text(p.name).strip().casefold() == folded
            ]
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

    # 2. Exact printer-reported filament_id, validated against metadata.
    observed_id = _core_text(getattr(obs, "filament_id", "")).strip()
    id_matches: list[PresetInfo] = []
    if observed_id:
        id_matches = [
            p
            for p in installed
            if _core_text(getattr(p, "filament_id", "")).strip() == observed_id
        ]
        validated = [
            preset for preset in id_matches
            if _filament_id_compatible(obs, preset)
        ]
        if validated:
            reasons = [f"filament_id {observed_id}"]
            confidence = _filament_id_confidence(obs)
            if len(validated) == 1:
                return _selected_result(
                    obs, validated[0], "installed", confidence, reasons
                )
            reasons_by_preset = {id(preset): reasons for preset in validated}
            scores_by_preset = {id(preset): confidence for preset in validated}
            return _multi_result(
                obs,
                validated,
                "installed",
                confidence,
                reasons_by_preset,
                scores_by_preset,
            )

    # 3. Exact vendor + material + subtype + product (blank obs parts unknown).
    matches = [p for p in installed if _exact_metadata(obs, p)]
    if matches:
        colour_ok = [p for p in matches if not _colour_veto(obs, p)]
        if colour_ok:
            matches = colour_ok
            if not normalize_text(getattr(obs, "subtype", "")):
                matches.sort(key=_exact_match_sort_key)
            reasons = _metadata_reasons(obs)
            if len(matches) == 1:
                return _selected_result(obs, matches[0], "installed", 95, reasons)
            return _multi_result(
                obs, matches, "installed", 95, default_reasons=reasons
            )
        # Only colour differs: present candidates instead of auto-selecting.
        reasons_by_preset = {}
        scores_by_preset = {}
        for preset in matches:
            distance = color_distance(
                getattr(obs, "color", ""), getattr(preset, "color", "")
            )
            reasons = list(_metadata_reasons(obs))
            if distance is not None:
                reasons.append(f"colour Δ{distance:.0f}")
            reasons_by_preset[id(preset)] = reasons
            scores_by_preset[id(preset)] = 90
        return _multi_result(
            obs,
            matches,
            "installed",
            90,
            reasons_by_preset,
            scores_by_preset,
        )

    # 4. Fuzzy score: threshold, hard vendor/material gates, and a margin
    #    against the next scored preset even when it is below the threshold.
    scored: list[tuple[int, list[str], PresetInfo]] = []
    for preset in installed:
        if _is_generic(preset):
            continue
        if not _material_matches(obs, preset):
            continue
        if observed_vendor and not _vendor_matches(obs, preset):
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
    if scored and scored[0][0] >= FUZZY_THRESHOLD:
        top_score, top_reasons, top_preset = scored[0]
        runner_score = scored[1][0] if len(scored) > 1 else None
        margin_ok = (
            runner_score is None
            or top_score - runner_score >= FUZZY_MARGIN
        )
        evidence_ok = bool(observed_vendor) or _brand_evidence(obs, top_preset)
        if margin_ok and evidence_ok and not _colour_veto(obs, top_preset):
            return _selected_result(
                obs, top_preset, "installed", top_score, top_reasons
            )
        rows = [
            row for row in scored
            if row[0] >= top_score - FUZZY_MARGIN + 1
        ]
        reasons_by_preset = {id(row[2]): row[1] for row in rows}
        scores_by_preset = {id(row[2]): row[0] for row in rows}
        return _multi_result(
            obs,
            [row[2] for row in rows],
            "installed",
            top_score,
            reasons_by_preset,
            scores_by_preset,
        )

    # A printer-reported ID that conflicts with the observed metadata is a
    # strong hint, but it must be a chooser rather than an automatic apply.
    if id_matches:
        reasons = [
            f"filament_id {observed_id} conflicts with observed metadata"
        ]
        return _multi_result(
            obs, id_matches, "installed", 80, default_reasons=reasons
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
