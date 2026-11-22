"""Printer, Spoolman, and Open Filament Database adapters.

This module is deliberately free of any OrcaSlicer imports so it can be tested
with plain ``unittest``.  The only project import is :class:`Observation` from
``filament_match_core``.

Public entry points used by the plugin UI:

* :func:`read_printer` -- Snapmaker then Moonraker, merged by Orca slot.
* :class:`SpoolmanClient` -- spool/filament lookup and decoded extra fields.
* :class:`OfdLibrary` -- cached OFD index lookup and one-preset fetch.

Network failures are reported through :class:`SourceError` for the Spoolman and
OFD clients.  :func:`read_printer` never raises for a printer/network problem;
it returns whatever was readable plus a list of human-readable errors.
"""

from __future__ import annotations

import copy
import http.client
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from filament_match_core import Observation

_SOURCE_PUBLIC_NAMES = [
    "DEFAULT_OFD_CACHE_TTL",
    "DEFAULT_OFD_INDEX_URL",
    "DEFAULT_TIMEOUT",
    "OfdLibrary",
    "SourceError",
    "SpoolmanClient",
    "normalize_base_url",
    "normalize_color",
    "read_printer",
    "safe_int",
    "safe_str",
]
if "__all__" in globals():
    # The single-file builder concatenates core, sources, and entry into one
    # module. Keep the core's public names too instead of replacing its list.
    __all__ = sorted(set(__all__) | set(_SOURCE_PUBLIC_NAMES))
else:
    __all__ = list(_SOURCE_PUBLIC_NAMES)
del _SOURCE_PUBLIC_NAMES

DEFAULT_TIMEOUT = 8.0
DEFAULT_OFD_INDEX_URL = "https://api.openfilamentdatabase.org/orcaslicer/index.json"
DEFAULT_OFD_CACHE_TTL = 24 * 60 * 60
USER_AGENT = "OrcaSlicer-FilamentMatch/0.1"

SNAPMAKER_PATH = "/printer/objects/query?print_task_config&filament_detect"
MOONRAKER_PATH = "/server/database/item?namespace=lane_data"

SNAPMAKER_SOURCE = "snapmaker"
MOONRAKER_SOURCE = "moonraker"

_EMPTY_WORDS = {"", "none", "null", "n/a", "na", "unknown", "unknown filament", "-", "--"}
_SNAPMAKER_ARRAY_KEYS = (
    "filament_exist",
    "filament_type",
    "filament_sub_type",
    "filament_color_rgba",
    "filament_vendor",
    "filament_spool_id",
    "filament_id",
    "filament_info_idx",
)
_MOONRAKER_LANE_KEYS = (
    "lane",
    "material",
    "color",
    "filament_id",
    "spool_id",
    "bed_temp",
    "nozzle_temp",
)


class SourceError(RuntimeError):
    """A filament source could not be read or returned unusable data."""


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def safe_str(value: Any, default: str = "") -> str:
    """Return a trimmed string, or ``default`` for unusable values."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return str(value)
    return default


def safe_int(value: Any, default: int | None = None) -> int | None:
    """Best-effort integer conversion.

    Accepts integers, finite floats, and numeric strings such as ``"210"`` or
    ``"210.0"``.  Anything else (including ``"#None"``) returns ``default``.
    """

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return default
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            number = float(text)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(number):
            return default
        return int(number)
    return default


def normalize_base_url(url: Any) -> str:
    """Normalize a printer / Spoolman / OFD base URL.

    A missing scheme becomes ``http`` and trailing slashes are removed.
    """

    text = safe_str(url)
    if not text:
        return ""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        text = "http://" + text
    text = text.rstrip("/")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*:", text):
        return ""
    return text


def _normalize_source_color(value: Any) -> str:
    """Normalize a printer colour to ``#RRGGBB``.

    Accepts ``#RGB``, ``#RGBA``, ``#RRGGBB``, ``#RRGGBBAA``, the same values
    without ``#``/with ``0x``, integer ``0xRRGGBB`` / ``0xRRGGBBAA`` values,
    and ``[r, g, b(, a)]`` sequences.  Alpha is discarded.  Garbage, including
    the ``#None`` value seen from Moonraker, becomes ``""``.
    """

    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        if 0 <= value <= 0xFFFFFF:
            return f"#{value:06X}"
        if 0 <= value <= 0xFFFFFFFF:
            # Klipper/Snapmaker call this field RGBA, so alpha is last.
            return f"#{value >> 8:06X}"
        return ""
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return ""
        return _normalize_source_color(int(value))
    if isinstance(value, (list, tuple)):
        if len(value) < 3:
            return ""
        channels: list[int] = []
        for item in value[:3]:
            channel = safe_int(item)
            if channel is None or not 0 <= channel <= 255:
                return ""
            channels.append(channel)
        return "#%02X%02X%02X" % tuple(channels)

    text = safe_str(value)
    if not text:
        return ""
    if text.casefold() in _EMPTY_WORDS:
        return ""

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            pass
        else:
            return _normalize_source_color(parsed)

    if text.casefold().startswith("rgb"):
        match = re.fullmatch(
            r"rgba?\(\s*([0-9]+)\s*,\s*([0-9]+)\s*,\s*([0-9]+)(?:\s*,\s*[0-9.]+\s*)?\)",
            text,
            re.IGNORECASE,
        )
        if not match:
            return ""
        return _normalize_source_color([match.group(1), match.group(2), match.group(3)])

    if "," in text and not text.startswith("#"):
        parts = [part.strip() for part in text.split(",") if part.strip()]
        if len(parts) >= 3:
            return _normalize_source_color(parts[:3])

    if text.casefold().startswith("0x"):
        text = text[2:]
    text = text.lstrip("#").strip()
    if not re.fullmatch(r"[0-9A-Fa-f]+", text):
        return ""
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    elif len(text) == 4:
        text = "".join(character * 2 for character in text[:3])
    elif len(text) == 6:
        pass
    elif len(text) == 8:
        text = text[:6]
    else:
        return ""
    return "#" + text.upper()


if "normalize_color" not in globals():
    # In a normal multi-file install this module owns the source-side helper.
    # The single-file build already has the core's canonical helper in the
    # shared global namespace; keep that one (and core's identity keys stable).
    def normalize_color(value: Any) -> str:
        """Normalize a printer colour to ``#RRGGBB``."""

        return _normalize_source_color(value)


def _positive_int(value: Any) -> int | None:
    number = safe_int(value)
    if number is None or number <= 0:
        return None
    return number


def _clean_material(value: Any) -> str:
    text = safe_str(value)
    if text.casefold() in _EMPTY_WORDS:
        return ""
    return text.upper()


def _clean_subtype(value: Any) -> str:
    text = safe_str(value)
    if text.casefold() in _EMPTY_WORDS:
        return ""
    return text


def _clean_vendor(value: Any) -> str:
    text = safe_str(value)
    if text.casefold() in _EMPTY_WORDS:
        return ""
    return text


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = safe_str(value).casefold()
    if text in {"", "0", "false", "no", "off", "none", "null", "unloaded", "empty"}:
        return False
    if text in {"1", "true", "yes", "on", "loaded", "present"}:
        return True
    try:
        return float(text) != 0
    except ValueError:
        return bool(text)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _join_url(base: str, path: str) -> str:
    if not base:
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


def _open_url(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """Fetch one URL with urllib and turn transport errors into SourceError."""

    request_headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        request_headers.update(
            {safe_str(key): safe_str(value) for key, value in headers.items() if value is not None}
        )
    if "://" not in url and os.path.exists(url):
        # Also makes local fixture files usable without an HTTP server.
        try:
            with open(url, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise SourceError(f"GET {url} failed: {exc}") from exc

    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"GET {url} failed: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise SourceError(f"GET {url} failed: {reason}") from exc
    except (TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
        raise SourceError(f"GET {url} failed: {exc}") from exc


def _decode_json(raw: Any, url: str) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceError(f"{url} returned non-UTF-8 data") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise SourceError(f"{url} returned no JSON data")
    if not text.strip():
        raise SourceError(f"{url} returned an empty response")
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"{url} returned invalid JSON: {exc}") from exc


def _request_json(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Callable[[str], Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Any:
    """Fetch JSON, optionally through an injectable opener used by tests."""

    if opener is None:
        raw = _open_url(url, timeout=timeout, headers=headers)
    else:
        try:
            raw = opener(url)
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - a source must never crash the UI
            raise SourceError(f"GET {url} failed: {exc}") from exc
    if isinstance(raw, Mapping) or isinstance(raw, list):
        return raw
    return _decode_json(raw, url)


# ---------------------------------------------------------------------------
# Snapmaker
# ---------------------------------------------------------------------------


def _collection_len(value: Any) -> int:
    if isinstance(value, Mapping):
        return len(value)
    if isinstance(value, (list, tuple)):
        return len(value)
    return 1 if value is not None else 0


def _at(value: Any, index: int) -> Any:
    """Read slot ``index`` (zero based) from a list, mapping, or scalar."""

    if isinstance(value, Mapping):
        for key in (index, str(index), index + 1, str(index + 1), f"T{index + 1}", f"lane{index + 1}"):
            if key in value:
                return value[key]
        return None
    if isinstance(value, (list, tuple)):
        if 0 <= index < len(value):
            return value[index]
        return None
    if index == 0:
        return value
    return None


def _slot_mapping(value: Any, index: int) -> Mapping[str, Any]:
    entry = _at(value, index)
    if isinstance(entry, Mapping):
        return entry
    if index == 0 and isinstance(value, Mapping):
        if any(key in value for key in ("BED_TEMP", "HOTEND_MAX_TEMP")):
            return value
    return {}


def _parse_snapmaker(payload: Any) -> list[Observation]:
    """Parse a ``/printer/objects/query`` response into observations.

    Malformed or empty data produces an empty list rather than an exception.
    """

    if not isinstance(payload, Mapping):
        return []

    status: Any = payload
    result = payload.get("result")
    if isinstance(result, Mapping):
        status = result.get("status")
        if not isinstance(status, Mapping):
            status = result
    if not isinstance(status, Mapping):
        return []

    config = status.get("print_task_config")
    if not isinstance(config, Mapping):
        if any(key in status for key in _SNAPMAKER_ARRAY_KEYS):
            config = status
        else:
            return []

    detect = status.get("filament_detect")
    if not isinstance(detect, Mapping):
        nested_detect = config.get("filament_detect")
        detect = nested_detect if isinstance(nested_detect, Mapping) else {}
    info = detect.get("info")

    slot_count = max(
        (_collection_len(config.get(key)) for key in _SNAPMAKER_ARRAY_KEYS),
        default=0,
    )
    if slot_count <= 0:
        return []

    observations: list[Observation] = []
    for index in range(slot_count):
        exist_raw = _at(config.get("filament_exist"), index)
        type_raw = _at(config.get("filament_type"), index)
        sub_raw = _at(config.get("filament_sub_type"), index)
        color_raw = _at(config.get("filament_color_rgba"), index)
        vendor_raw = _at(config.get("filament_vendor"), index)
        spool_raw = _at(config.get("filament_spool_id"), index)
        filament_raw = _at(config.get("filament_id"), index)
        if filament_raw is None:
            filament_raw = _at(config.get("filament_info_idx"), index)

        material = _clean_material(type_raw)
        subtype = _clean_subtype(sub_raw)
        vendor = _clean_vendor(vendor_raw)
        color = _normalize_source_color(color_raw)
        present = (
            _truthy(exist_raw)
            if exist_raw is not None
            else bool(material or subtype or vendor or color)
        )

        if present:
            filament_id = safe_str(filament_raw)
            spool_id = _positive_int(spool_raw)
        else:
            material = ""
            subtype = ""
            vendor = ""
            color = ""
            filament_id = ""
            spool_id = None

        info_raw = _slot_mapping(info, index)
        bed_temp = _positive_int(info_raw.get("BED_TEMP")) if info_raw else None
        nozzle_temp = _positive_int(info_raw.get("HOTEND_MAX_TEMP")) if info_raw else None
        if not present:
            bed_temp = None
            nozzle_temp = None

        observations.append(
            Observation(
                slot=index + 1,
                present=present,
                vendor=vendor,
                material=material,
                subtype=subtype,
                product="",
                color=color,
                filament_id=filament_id,
                spool_id=spool_id,
                tag_uid="",
                bed_temp=bed_temp,
                nozzle_temp=nozzle_temp,
                source=SNAPMAKER_SOURCE,
                raw={
                    "slot_index": index,
                    "filament_exist": exist_raw,
                    "filament_type": type_raw,
                    "filament_sub_type": sub_raw,
                    "filament_color_rgba": color_raw,
                    "filament_vendor": vendor_raw,
                    "filament_spool_id": spool_raw,
                    "filament_id": filament_raw,
                    "filament_detect": dict(info_raw),
                },
            )
        )
    return observations


def _read_snapmaker(base_url: str, api_key: str, timeout: float) -> list[Observation]:
    headers = {"X-Api-Key": api_key} if api_key else None
    payload = _request_json(
        _join_url(base_url, SNAPMAKER_PATH),
        timeout=timeout,
        headers=headers,
    )
    return _parse_snapmaker(payload)


# ---------------------------------------------------------------------------
# Moonraker
# ---------------------------------------------------------------------------


def _moonraker_value(payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        return None
    value: Any = payload
    result = payload.get("result")
    if isinstance(result, Mapping):
        value = result.get("value", result)
    elif "value" in payload:
        value = payload.get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, Mapping) and isinstance(value.get("lane_data"), (Mapping, list)):
        value = value["lane_data"]
    return value


def _moonraker_slot(lane: Mapping[str, Any], key: Any, order: int) -> int:
    """Map Moonraker's 0-based lane number to a 1-based Orca slot."""

    raw_lane = lane.get("lane")
    number = safe_int(raw_lane)
    if number is not None and number >= 0:
        return number + 1

    text = safe_str(raw_lane)
    match = re.fullmatch(r"[Tt]\s*(\d+)", text)
    if match:
        return int(match.group(1)) + 1

    key_text = safe_str(key)
    match = re.fullmatch(r"(?:lane|L)\s*[_\-]?\s*(\d+)", key_text, re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    if key_text.isdigit():
        return int(key_text) + 1
    return order + 1


def _parse_moonraker(payload: Any) -> list[Observation]:
    """Parse ``/server/database/item?namespace=lane_data`` into observations."""

    value = _moonraker_value(payload)
    if value is None:
        return []

    if isinstance(value, list):
        items: list[tuple[Any, Any]] = [(str(index), item) for index, item in enumerate(value)]
    elif isinstance(value, Mapping):
        if any(key in value for key in _MOONRAKER_LANE_KEYS) and not isinstance(
            value.get("lane_data"), (Mapping, list)
        ):
            items = [("", value)]
        else:
            items = list(value.items())
    else:
        return []

    observations: list[Observation] = []
    seen_slots: set[int] = set()

    for order, (key, lane) in enumerate(items):
        if isinstance(lane, str):
            try:
                lane = json.loads(lane)
            except (TypeError, ValueError):
                continue
        if not isinstance(lane, Mapping):
            continue

        slot = _moonraker_slot(lane, key, order)
        if slot in seen_slots:
            continue

        material = _clean_material(
            lane.get("material") or lane.get("filament_type") or lane.get("type")
        )
        vendor = _clean_vendor(
            lane.get("vendor") or lane.get("vendor_name") or lane.get("brand")
        )
        subtype = _clean_subtype(
            lane.get("subtype") or lane.get("sub_type") or lane.get("variant")
        )
        product = safe_str(lane.get("product"))
        color = _normalize_source_color(
            lane.get("color") or lane.get("color_hex") or lane.get("filament_color")
        )
        filament_id_raw = lane.get("filament_id")
        # Orca only accepts a string here; Happy Hare's numeric Spoolman
        # filament id under the same key must be ignored.
        filament_id = filament_id_raw.strip() if isinstance(filament_id_raw, str) else ""
        spool_id = _positive_int(lane.get("spool_id"))
        bed_temp = _positive_int(lane.get("bed_temp"))
        nozzle_temp = _positive_int(lane.get("nozzle_temp"))
        tag_uid = safe_str(lane.get("tag_uid") or lane.get("uid") or lane.get("card_uid"))

        # A lane is loaded when it has a material, or when a bridge has put a
        # Spoolman spool on it (the material can be filled from Spoolman later).
        # Colour/vendor alone are not enough: stale/empty lane_data commonly
        # keeps a colour but clears the material.
        if not material and spool_id is None:
            continue

        seen_slots.add(slot)
        raw = dict(lane)
        raw["_key"] = key
        observations.append(
            Observation(
                slot=slot,
                present=True,
                vendor=vendor,
                material=material,
                subtype=subtype,
                product=product,
                color=color,
                filament_id=filament_id,
                spool_id=spool_id,
                tag_uid=tag_uid,
                bed_temp=bed_temp,
                nozzle_temp=nozzle_temp,
                source=MOONRAKER_SOURCE,
                raw=raw,
            )
        )

    observations.sort(key=lambda observation: observation.slot)
    return observations


def _read_moonraker(base_url: str, api_key: str, timeout: float) -> list[Observation]:
    headers = {"X-Api-Key": api_key} if api_key else None
    payload = _request_json(
        _join_url(base_url, MOONRAKER_PATH),
        timeout=timeout,
        headers=headers,
    )
    return _parse_moonraker(payload)


# ---------------------------------------------------------------------------
# Printer merge
# ---------------------------------------------------------------------------


def _merge_observations(
    primary: Sequence[Observation],
    secondary: Sequence[Observation],
) -> list[Observation]:
    """Merge by slot.  ``primary`` wins on non-empty values.

    Snapmaker is intentionally the primary source because it carries vendor and
    subtype; Moonraker fills blanks (notably Spoolman ``spool_id``) and adds
    slots that Snapmaker did not report.
    """

    merged: dict[int, Observation] = {observation.slot: observation for observation in primary}

    for extra in secondary:
        current = merged.get(extra.slot)
        if current is None:
            merged[extra.slot] = extra
            continue

        for name in ("vendor", "material", "subtype", "product", "color", "filament_id", "tag_uid"):
            if not getattr(current, name) and getattr(extra, name):
                setattr(current, name, getattr(extra, name))
        for name in ("spool_id", "bed_temp", "nozzle_temp"):
            if getattr(current, name) is None and getattr(extra, name) is not None:
                setattr(current, name, getattr(extra, name))

        was_present = bool(current.present)
        current.present = bool(current.present or extra.present)
        if not was_present and extra.present:
            # The slot was empty in the primary source; the usable data (and
            # therefore the provenance) comes from the secondary source.
            current.source = extra.source

        raw = dict(current.raw or {})
        others = raw.get("other_sources")
        if not isinstance(others, dict):
            others = {}
        others[extra.source or "other"] = extra.raw
        raw["other_sources"] = others
        current.raw = raw

    return [merged[slot] for slot in sorted(merged)]


def read_printer(
    base_url: str,
    api_key: str = "",
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[list[Observation], list[str]]:
    """Read loaded filaments from a Snapmaker or Moonraker printer.

    Both endpoints are attempted; Snapmaker wins on conflicts and Moonraker
    fills missing fields/slots.  The return value is ``(observations, errors)``
    and this function never raises because the printer is offline or returns
    bad JSON.
    """

    base = normalize_base_url(base_url)
    if not base:
        return [], ["no printer URL configured"]

    observations: list[Observation] = []
    moonraker_observations: list[Observation] = []
    errors: list[str] = []

    try:
        observations = _read_snapmaker(base, api_key, timeout)
    except SourceError as exc:
        errors.append(f"{SNAPMAKER_SOURCE}: {exc}")
    except Exception as exc:  # noqa: BLE001 - one bad source must not crash the UI
        errors.append(f"{SNAPMAKER_SOURCE}: unexpected error: {exc}")

    try:
        moonraker_observations = _read_moonraker(base, api_key, timeout)
    except SourceError as exc:
        errors.append(f"{MOONRAKER_SOURCE}: {exc}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{MOONRAKER_SOURCE}: unexpected error: {exc}")

    if moonraker_observations:
        observations = _merge_observations(observations, moonraker_observations)

    if not observations and not errors:
        errors.append("no filament slots reported by Snapmaker or Moonraker")

    return observations, errors


# ---------------------------------------------------------------------------
# Spoolman
# ---------------------------------------------------------------------------


class SpoolmanClient:
    """Small Spoolman REST client.

    Extra field values are JSON-encoded strings on the wire.  Use
    :meth:`extras` / :meth:`extra_value` / :meth:`preset_hint` rather than
    reading ``record["extra"]`` directly.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Callable[[str], Any] | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout = timeout
        self._opener = opener

    def _request(self, path: str) -> Any:
        if not self.base_url:
            raise SourceError("Spoolman URL is not configured")
        url = _join_url(self.base_url, path)
        return _request_json(url, timeout=self.timeout, opener=self._opener)

    @staticmethod
    def _identifier(value: Any, kind: str) -> str:
        if value is None or isinstance(value, bool):
            raise SourceError(f"Spoolman {kind} id is missing")
        text = safe_str(value)
        if not text:
            raise SourceError(f"Spoolman {kind} id is missing")
        return text

    def get_spool(self, id: Any) -> dict[str, Any]:  # noqa: A002 - matches the documented API
        """Return one spool object from ``/api/v1/spool/{id}``."""

        identifier = self._identifier(id, "spool")
        payload = self._request(f"/api/v1/spool/{urllib.parse.quote(identifier, safe='')}")
        if not isinstance(payload, Mapping):
            raise SourceError(f"Spoolman spool {identifier} response is not an object")
        return dict(payload)

    def get_filament(self, id: Any) -> dict[str, Any]:  # noqa: A002 - matches the documented API
        """Return one filament object from ``/api/v1/filament/{id}``."""

        identifier = self._identifier(id, "filament")
        payload = self._request(f"/api/v1/filament/{urllib.parse.quote(identifier, safe='')}")
        if not isinstance(payload, Mapping):
            raise SourceError(f"Spoolman filament {identifier} response is not an object")
        return dict(payload)

    def find_filament_by_extra(self, key: str, value: Any) -> dict[str, Any] | None:
        """Find the first filament whose decoded ``extra[key]`` equals ``value``.

        Spoolman does not offer a portable server-side filter for arbitrary
        extra fields, so this fetches the filament list and filters locally.
        It is a fallback, not part of the normal spool-id lookup path.
        """

        key_text = safe_str(key)
        if not key_text:
            raise SourceError("Spoolman extra key is empty")
        payload = self._request("/api/v1/filament")
        if isinstance(payload, Mapping):
            payload = payload.get("filaments", payload.get("items"))
        if not isinstance(payload, list):
            raise SourceError("Spoolman filament list response is not an array")
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            extras = self.extras(item)
            if key_text in extras and str(extras[key_text]) == str(value):
                return dict(item)
        return None

    @classmethod
    def decode_extra_value(cls, value: Any) -> Any:
        """Decode one Spoolman extra value.

        Values are normally JSON-encoded strings (``"\\"Matte\\""``); plain
        strings that are not valid JSON are returned unchanged.
        """

        if isinstance(value, Mapping) and "value" in value:
            return cls.decode_extra_value(value["value"])
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return value
        return value

    @classmethod
    def decode_extra(cls, raw: Any) -> dict[str, Any]:
        """Decode a whole ``extra`` object (mapping or list form)."""

        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return {}
            try:
                raw = json.loads(text)
            except (TypeError, ValueError):
                return {}
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            result: dict[str, Any] = {}
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                name = safe_str(item.get("name") or item.get("key"))
                if name:
                    result[name] = cls.decode_extra_value(item.get("value"))
            return result
        if not isinstance(raw, Mapping):
            return {}
        return {str(key): cls.decode_extra_value(value) for key, value in raw.items()}

    @classmethod
    def extras(cls, record: Mapping[str, Any]) -> dict[str, Any]:
        """Return decoded extra fields for a spool or filament record.

        Spool-level extras take precedence over extras on the nested
        ``filament`` object.
        """

        if not isinstance(record, Mapping):
            return {}
        result = cls.decode_extra(record.get("extra"))
        filament = record.get("filament")
        if isinstance(filament, Mapping):
            for key, value in cls.decode_extra(filament.get("extra")).items():
                result.setdefault(key, value)
        return result

    @classmethod
    def extra_value(cls, record: Mapping[str, Any], key: str, default: Any = None) -> Any:
        return cls.extras(record).get(safe_str(key), default)

    @classmethod
    def preset_hint(cls, record: Mapping[str, Any]) -> str:
        """Return ``orca_filament_preset`` or ``orca_preset`` as a plain string."""

        extras = cls.extras(record)
        for key in ("orca_filament_preset", "orca_preset"):
            value = extras.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value not in (None, ""):
                return str(value)
        return ""


# ---------------------------------------------------------------------------
# Open Filament Database
# ---------------------------------------------------------------------------


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", safe_str(value).casefold())


def _tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", safe_str(value).casefold())
        if token
    }


def _material_family(value: Any) -> str:
    """Reduce a material label to the OFD family used in the index.

    OFD's ``material`` is the base family (``PETG``), while printers and
    Spoolman often append a variant (``PETG-CF``, ``PLA+``, ``PLA High Speed``).
    """

    text = safe_str(value).upper()
    if not text:
        return ""
    return re.split(r"[-+/\s]", text, maxsplit=1)[0]


def _brand_match_score(vendor: str, brand: str) -> int | None:
    vendor_norm = _norm(vendor)
    brand_norm = _norm(brand)
    if not vendor_norm:
        return 0
    if not brand_norm:
        return None
    if vendor_norm == brand_norm:
        return 35
    if len(vendor_norm) >= 3 and len(brand_norm) >= 3 and (
        vendor_norm in brand_norm or brand_norm in vendor_norm
    ):
        return 25
    vendor_tokens = _tokens(vendor)
    brand_tokens = _tokens(brand)
    if vendor_tokens and brand_tokens and (vendor_tokens & brand_tokens):
        return 15
    return None


def _product_match_score(product: str, filament: str, profile_name: str, brand: str) -> int | None:
    product_norm = _norm(product)
    if not product_norm:
        return 0

    brand_norm = _norm(brand)
    if brand_norm and product_norm.startswith(brand_norm) and len(product_norm) > len(brand_norm):
        product_norm = product_norm[len(brand_norm) :]

    filament_norm = _norm(filament)
    profile_norm = _norm(profile_name)
    if not product_norm:
        return None
    if product_norm == filament_norm:
        return 40
    if len(product_norm) >= 4 and filament_norm and (
        product_norm in filament_norm or filament_norm in product_norm
    ):
        return 30
    if len(product_norm) >= 4 and profile_norm and product_norm in profile_norm:
        return 25

    product_tokens = _tokens(product) - _tokens(brand)
    filament_tokens = _tokens(filament)
    if product_tokens and filament_tokens:
        overlap = len(product_tokens & filament_tokens)
        if overlap:
            return min(24, 8 * overlap)
    return None


def _score_ofd_entry(
    entry: Mapping[str, Any],
    vendor: str,
    material: str,
    product: str,
    subtype: str,
) -> tuple[int, bool] | None:
    brand = safe_str(entry.get("brand"))
    filament = safe_str(entry.get("filament"))
    profile_name = safe_str(entry.get("profile_name"))
    entry_material = safe_str(entry.get("material"))

    if material and _material_family(material) != _material_family(entry_material):
        return None

    score = 20 if material else 0

    if vendor:
        brand_score = _brand_match_score(vendor, brand)
        if brand_score is None:
            return None
        score += brand_score

    if product:
        product_score = _product_match_score(product, filament, profile_name, brand)
        if product_score is None:
            return None
        score += product_score

    subtype_hit = False
    if subtype:
        haystack = " ".join((filament, profile_name, safe_str(entry.get("inherits"))))
        if _norm(subtype) and _norm(subtype) in _norm(haystack):
            score += 15
            subtype_hit = True
        else:
            subtype_tokens = _tokens(subtype)
            if subtype_tokens and subtype_tokens <= _tokens(haystack):
                score += 8
                subtype_hit = True

    return min(100, score), subtype_hit


def _directory_url(url: str) -> str:
    """Return the directory part of an index URL as a URL ending in ``/``."""

    parsed = urllib.parse.urlsplit(url)
    path = parsed.path
    if not path.endswith("/"):
        last_segment = path.rsplit("/", 1)[-1]
        if "." in last_segment:
            path = path.rsplit("/", 1)[0] + "/"
        else:
            path += "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _normalize_ofd_index(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        payload = {"profiles": payload}
    if not isinstance(payload, Mapping):
        raise SourceError("OFD index is not a JSON object")

    profiles = payload.get("profiles")
    if profiles is None:
        profiles = payload.get("filaments")
    if profiles is None:
        profiles = []
    if isinstance(profiles, Mapping):
        profiles = list(profiles.values())
    if not isinstance(profiles, list):
        raise SourceError("OFD index has no profiles list")

    result = dict(payload)
    result["profiles"] = [profile for profile in profiles if isinstance(profile, Mapping)]
    return result


class OfdLibrary:
    """Cached lookup for the Open Filament Database OrcaSlicer index.

    Only the index and the one chosen preset are ever fetched.  The index is
    cached on disk (atomically) for :attr:`cache_ttl` seconds.  If the network
    is down but a stale cache exists, the stale cache is used.
    """

    def __init__(
        self,
        index_url: str = DEFAULT_OFD_INDEX_URL,
        cache_path: str | os.PathLike[str] | None = None,
        *,
        preset_cache_dir: str | os.PathLike[str] | None = None,
        cache_ttl: float = DEFAULT_OFD_CACHE_TTL,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Callable[[str], Any] | None = None,
    ) -> None:
        if isinstance(index_url, os.PathLike):
            index_url = os.fspath(index_url)
        self.index_url = safe_str(index_url) or DEFAULT_OFD_INDEX_URL
        self._preset_base_url = _directory_url(self.index_url)
        self.cache_path = os.fspath(cache_path) if cache_path is not None else None
        self.preset_cache_dir = (
            os.fspath(preset_cache_dir) if preset_cache_dir is not None else None
        )
        self.cache_ttl = max(0.0, float(cache_ttl))
        self.timeout = timeout
        self._opener = opener
        self._index: dict[str, Any] | None = None
        self._profiles: list[Mapping[str, Any]] = []
        self._presets: dict[str, dict[str, Any]] = {}
        self.last_error = ""
        self.cache_error = ""

    # -- index cache ------------------------------------------------------

    def _cache_is_fresh(self) -> bool:
        if not self.cache_path or self.cache_ttl <= 0:
            return False
        try:
            age = time.time() - os.path.getmtime(self.cache_path)
        except OSError:
            return False
        return age < self.cache_ttl

    def _read_cache(self) -> dict[str, Any] | None:
        if not self.cache_path:
            return None
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, TypeError, ValueError):
            return None
        try:
            return _normalize_ofd_index(payload)
        except SourceError:
            return None

    def _write_cache(self, payload: Mapping[str, Any]) -> None:
        if not self.cache_path:
            return
        parent = os.path.dirname(os.path.abspath(self.cache_path)) or "."
        temporary_path: str | None = None
        try:
            os.makedirs(parent, exist_ok=True)
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".ofd-index-",
                suffix=".tmp",
                dir=parent,
            )
            os.close(descriptor)
            with open(temporary_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.cache_path)
            temporary_path = None
            self.cache_error = ""
        except (OSError, TypeError, ValueError) as exc:
            self.cache_error = str(exc)
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _install_index(self, payload: Any) -> None:
        normalized = _normalize_ofd_index(payload)
        self._index = normalized
        profiles = normalized.get("profiles", [])
        self._profiles = [profile for profile in profiles if isinstance(profile, Mapping)]

    def load_index(self, force: bool = False) -> dict[str, Any]:
        """Load the index from memory, the cache, or the network."""

        if not force and self._index is not None:
            return self._index

        cached = self._read_cache()
        if not force and self._cache_is_fresh() and cached is not None:
            self._install_index(cached)
            return self._index  # type: ignore[return-value]

        try:
            payload = _request_json(
                self.index_url,
                timeout=self.timeout,
                opener=self._opener,
            )
            self._install_index(payload)
            assert self._index is not None
            self._write_cache(self._index)
            self.last_error = ""
            return self._index
        except SourceError as exc:
            self.last_error = str(exc)
            fallback = cached if cached is not None else self._index
            if fallback is not None:
                self._install_index(fallback)
                assert self._index is not None
                return self._index
            raise

    def refresh(self) -> dict[str, Any]:
        """Force a network refresh, falling back to cache if it fails."""

        return self.load_index(force=True)

    # -- lookup -----------------------------------------------------------

    def find(
        self,
        vendor: str = "",
        material: str = "",
        product: str = "",
        subtype: str = "",
        *,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Find OFD profiles matching the supplied filament metadata.

        Material is a hard filter; vendor and product must have some affinity
        when supplied.  Subtype is used to narrow results when any candidate
        actually carries it, otherwise it only improves ranking.  At least one
        search field is required (an empty query returns no candidates rather
        than the whole library).
        """

        vendor = safe_str(vendor)
        material = safe_str(material)
        product = safe_str(product)
        subtype = safe_str(subtype)
        if not any((vendor, material, product, subtype)):
            return []
        if _norm(vendor) == "generic":
            return []

        max_results = safe_int(limit, 25)
        if max_results is None or max_results <= 0:
            return []

        self.load_index()

        scored: list[tuple[int, bool, Mapping[str, Any]]] = []
        for entry in self._profiles:
            result = _score_ofd_entry(entry, vendor, material, product, subtype)
            if result is None:
                continue
            score, subtype_hit = result
            scored.append((score, subtype_hit, entry))

        if subtype:
            subtype_matches = [item for item in scored if item[1]]
            if subtype_matches:
                scored = subtype_matches

        scored.sort(
            key=lambda item: (
                -item[0],
                safe_str(item[2].get("brand")).casefold(),
                safe_str(item[2].get("filament")).casefold(),
                safe_str(item[2].get("profile_name")).casefold(),
            )
        )

        candidates: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for score, _subtype_hit, entry in scored:
            path = safe_str(entry.get("path"))
            profile_name = safe_str(entry.get("profile_name"))
            if not path or not profile_name:
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)
            candidates.append(
                {
                    "brand": safe_str(entry.get("brand")),
                    "material": safe_str(entry.get("material")),
                    "filament": safe_str(entry.get("filament")),
                    "profile_name": profile_name,
                    "path": path,
                    "source": safe_str(entry.get("source")),
                    "inherits": safe_str(entry.get("inherits")),
                    "orca_filament_code": safe_str(entry.get("orca_filament_code")),
                    "id": safe_str(entry.get("id")),
                    "score": score,
                }
            )
            if len(candidates) >= max_results:
                break
        return candidates

    # -- one-preset fetch -------------------------------------------------

    def _preset_cache_path(self, path: str) -> str:
        if not self.preset_cache_dir:
            return ""
        parts = [part for part in safe_str(path).split("/") if part]
        if not parts or any(part in (".", "..") or "\\" in part for part in parts):
            return ""
        return os.path.join(self.preset_cache_dir, *parts)

    def preset_is_cached(self, candidate: Mapping[str, Any] | str) -> bool:
        """Whether the preset JSON is already on disk and no fetch is needed."""

        if isinstance(candidate, Mapping):
            path = safe_str(candidate.get("path"))
        else:
            path = safe_str(candidate)
        cache_path = self._preset_cache_path(path)
        return bool(cache_path and os.path.isfile(cache_path))

    def _write_preset_cache(self, cache_path: str, preset: Mapping[str, Any]) -> None:
        parent = os.path.dirname(cache_path)
        temporary = None
        try:
            os.makedirs(parent, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".ofd-preset-", suffix=".tmp", dir=parent)
            os.close(descriptor)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(preset, handle, ensure_ascii=False)
            os.replace(temporary, cache_path)
            temporary = None
        except (OSError, TypeError, ValueError):
            pass
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def fetch(self, candidate: Mapping[str, Any] | str) -> dict[str, Any]:
        """Fetch one preset JSON for a candidate returned by :meth:`find`."""

        if isinstance(candidate, Mapping):
            path = safe_str(candidate.get("path"))
        else:
            path = safe_str(candidate)
        if not path:
            raise SourceError("OFD candidate has no preset path")

        if path.startswith("/"):
            raise SourceError(f"invalid OFD preset path: {path!r}")
        clean_path = path.strip()
        parts = clean_path.split("/")
        if (
            not clean_path
            or any(part in ("", ".", "..") for part in parts)
            or urllib.parse.urlsplit(clean_path).scheme
        ):
            raise SourceError(f"invalid OFD preset path: {path!r}")

        cached = self._presets.get(clean_path)
        if cached is not None:
            return copy.deepcopy(cached)

        cache_path = self._preset_cache_path(clean_path)
        if cache_path:
            try:
                with open(cache_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, Mapping):
                    preset = dict(payload)
                    self._presets[clean_path] = preset
                    return copy.deepcopy(preset)
            except (OSError, TypeError, ValueError):
                pass

        url = urllib.parse.urljoin(
            self._preset_base_url,
            urllib.parse.quote(clean_path, safe="/._-~"),
        )
        payload = _request_json(url, timeout=self.timeout, opener=self._opener)
        if not isinstance(payload, Mapping):
            raise SourceError(f"OFD preset {clean_path} is not a JSON object")
        preset = dict(payload)
        self._presets[clean_path] = preset
        if cache_path:
            self._write_preset_cache(cache_path, preset)
        return copy.deepcopy(preset)

    @staticmethod
    def validate_preset(preset: Any) -> list[str]:
        """Return installation problems for a fetched preset.

        The entry point calls this before writing a preset; :meth:`fetch`
        itself only requires a JSON object so callers can inspect odd payloads
        before deciding what to do.
        """

        if not isinstance(preset, Mapping):
            return ["preset is not a JSON object"]
        problems: list[str] = []
        name = preset.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append("missing or empty name")
        from_value = preset.get("from")
        if not isinstance(from_value, str) or not from_value.strip():
            problems.append("missing from")
        elif from_value.strip().casefold() == "system":
            problems.append("system presets cannot be installed")
        inherits = preset.get("inherits")
        if not isinstance(inherits, str) or not inherits.strip():
            problems.append("missing inherits")
        for key in (
            "setting_id",
            "filament_id",
            "filament_ids",
            "base_id",
            "is_system",
            "system",
            "bundle_id",
        ):
            if key in preset:
                problems.append(f"system-only key present: {key}")
        return problems
