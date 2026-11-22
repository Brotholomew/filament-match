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
import hashlib
import http.client
import ipaddress
import json
import math
import ntpath
import os
import re
import shutil
import tempfile
import threading
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
    "normalize_spoolman_url",
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
MAX_TIMEOUT = 300.0
SPOOLMAN_PAGE_SIZE = 100

SNAPMAKER_PATH = "/printer/objects/query?print_task_config&filament_detect"
MOONRAKER_PATH = "/server/database/item?namespace=lane_data"
MOONRAKER_MMU_PATH = "/printer/objects/query?mmu"
MOONRAKER_BOX_PATH = "/printer/objects/query?box"

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
_CREALITY_MATERIAL_TYPES = {
    "000001": "PLA",
    "000002": "ABS",
    "000003": "PETG",
    "000004": "TPU",
    "000005": "ASA",
    "000006": "PA",
    "000007": "PC",
    "000008": "PLA-CF",
    "000009": "PETG-CF",
    "000010": "PA-CF",
}
_SPOOLMAN_API_SUFFIX_RE = re.compile(r"/api/v1/?$", re.IGNORECASE)
_REDIRECT_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization", "x-api-key"}
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


def _bracket_authority(authority: str) -> str:
    """Bracket an unbracketed IPv6 literal in a URL authority."""

    text = safe_str(authority)
    if not text or text.startswith("["):
        return text
    userinfo = ""
    if "@" in text:
        prefix, text = text.rsplit("@", 1)
        userinfo = prefix + "@"
    if text.count(":") >= 2:
        return userinfo + "[" + text + "]"
    return userinfo + text


def _authority_has_port(authority: str) -> bool:
    text = safe_str(authority)
    if not text:
        return False
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if text.startswith("["):
        closing = text.find("]")
        return closing >= 0 and bool(re.fullmatch(r":\d+", text[closing + 1 :]))
    return bool(re.fullmatch(r"[^:]+:\d+", text))


def normalize_base_url(url: Any, port: Any = None) -> str:
    """Normalize a printer / Spoolman / OFD base URL.

    A missing scheme becomes ``http``, trailing slashes are removed, and IPv6
    literals are bracketed.  When ``port`` is supplied it is appended only when
    the URL does not already carry an explicit port; this keeps the configured
    Orca ``printhost_port`` authoritative instead of probing common ports.
    """

    text = safe_str(url)
    if not text:
        return ""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        text = "http://" + text
    scheme, _, remainder = text.partition("://")
    authority, separator, path = remainder.partition("/")
    authority = _bracket_authority(authority)
    port_text = safe_str(port)
    if port_text and re.fullmatch(r"\d+", port_text) and not _authority_has_port(authority):
        authority += ":" + port_text
    if not authority:
        return ""
    text = scheme + "://" + authority
    if separator:
        text += "/" + path
    text = text.rstrip("/")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*:", text):
        return ""
    return text


def normalize_spoolman_url(url: Any) -> str:
    """Normalize a Spoolman base URL and drop a trailing ``/api/v1``.

    Spoolman's REST paths already start with ``/api/v1``.  Accepting the
    documented base URL with that suffix avoids ``/api/v1/api/v1`` requests and
    lets users paste the URL they use in a browser.
    """

    base = normalize_base_url(url)
    if not base:
        return ""
    parsed = urllib.parse.urlsplit(base)
    path = _SPOOLMAN_API_SUFFIX_RE.sub("", parsed.path)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, "", "")
    ).rstrip("/")



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


def _coerce_timeout(value: Any) -> float:
    """Return a finite, positive timeout; never pass ``inf``/``None`` to urllib."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if not math.isfinite(number) or number <= 0:
        return DEFAULT_TIMEOUT
    return min(number, MAX_TIMEOUT)


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(safe_str(url))
    try:
        port = parsed.port
    except ValueError:
        port = None
    scheme = parsed.scheme.casefold()
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (parsed.hostname or "").casefold(), port


def _is_private_host(host: str) -> bool:
    text = safe_str(host).strip("[]").rstrip(".").casefold()
    if not text:
        return False
    if text == "localhost" or text.endswith(".localhost") or text.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    )


class _AuthStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never forward credentials to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.casefold() not in ("http", "https"):
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        if _url_origin(req.full_url) == _url_origin(newurl):
            return redirected
        for mapping in (redirected.headers, redirected.unredirected_hdrs):
            for key in list(mapping):
                if key.casefold() in _REDIRECT_HEADERS:
                    del mapping[key]
        return redirected


def _url_opener(url: str) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_AuthStrippingRedirectHandler()]
    host = urllib.parse.urlsplit(url).hostname or ""
    if _is_private_host(host):
        # A user's corporate/desktop proxy must not intercept a printer,
        # Spoolman, or OFD fixture on loopback/private space.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


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

    timeout = _coerce_timeout(timeout)
    opener = _url_opener(url)
    last_error: Exception | None = None
    for attempt in range(2):
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if attempt == 0 and (exc.code == 429 or 500 <= exc.code < 600):
                continue
            raise SourceError(f"GET {url} failed: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == 0:
                continue
            reason = getattr(exc, "reason", exc)
            raise SourceError(f"GET {url} failed: {reason}") from exc
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            last_error = exc
            if attempt == 0:
                continue
            raise SourceError(f"GET {url} failed: {exc}") from exc
        except ValueError as exc:
            raise SourceError(f"GET {url} failed: {exc}") from exc
    raise SourceError(f"GET {url} failed: {last_error}")


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


def _snapmaker_combine_type(base: Any, subtype: Any) -> str:
    """Mirror SnapmakerPrinterAgent::combine_filament_type."""

    material = _clean_material(base)
    sub = _clean_subtype(subtype).upper()
    if not material:
        return "PLA"
    if not sub or sub == "NONE":
        return material
    fibre = sub
    match = re.fullmatch(r"(CF|GF)\d*", sub)
    if match:
        fibre = match.group(1)
    if fibre in ("CF", "GF"):
        suffix = "-" + fibre
        return material if material.endswith(suffix) else material + suffix
    # Every other subtype is a finish or a brand, not a material type.
    return material


def _parse_snapmaker(payload: Any) -> list[Observation]:
    """Parse a ``/printer/objects/query`` response into observations.

    This mirrors ``SnapmakerPrinterAgent::fetch_filament_info``: slots are the
    ``filament_exist`` array, arrays are parallel, and NFC temperatures are
    optional.  Malformed or empty data produces an empty list rather than an
    exception.
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

    exist = config.get("filament_exist")
    if isinstance(exist, Mapping):
        slot_count = len(exist)
    elif isinstance(exist, (list, tuple)):
        slot_count = len(exist)
    else:
        slot_count = 0
    if slot_count <= 0:
        return []

    detect = status.get("filament_detect")
    if not isinstance(detect, Mapping):
        nested_detect = config.get("filament_detect")
        detect = nested_detect if isinstance(nested_detect, Mapping) else {}
    info = detect.get("info")

    observations: list[Observation] = []
    for index in range(slot_count):
        exist_raw = _at(exist, index)
        type_raw = _at(config.get("filament_type"), index)
        sub_raw = _at(config.get("filament_sub_type"), index)
        color_raw = _at(config.get("filament_color_rgba"), index)
        vendor_raw = _at(config.get("filament_vendor"), index)
        spool_raw = _at(config.get("filament_spool_id"), index)
        filament_raw = _at(config.get("filament_id"), index)
        if filament_raw is None:
            filament_raw = _at(config.get("filament_info_idx"), index)

        present = _truthy(exist_raw)
        if present:
            material = _snapmaker_combine_type(type_raw, sub_raw)
            subtype = _clean_subtype(sub_raw)
            if subtype.casefold() in _EMPTY_WORDS:
                subtype = ""
            vendor = _clean_vendor(vendor_raw)
            color = _normalize_source_color(color_raw)
            if not safe_str(color_raw):
                # Native Snapmaker reports FFFFFFFF when a loaded tag has no
                # colour; keep that default while still treating malformed
                # values such as "#None" as unknown.
                color = _normalize_source_color("FFFFFFFF")
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


def _moonraker_lane_index(lane: Mapping[str, Any]) -> int | None:
    """Return the native ``lane`` value only when it is a JSON string.

    ``safe_json_string`` in MoonrakerPrinterAgent returns an empty string for a
    JSON number, so numeric lanes must be skipped rather than coerced.  The
    object key (`lane1`, `0`, ...) is deliberately never used for numbering.
    """

    raw_lane = lane.get("lane")
    if not isinstance(raw_lane, str):
        return None
    text = raw_lane.strip()
    if not re.fullmatch(r"\d+", text):
        return None
    return int(text)


def _parse_moonraker_lane_data(payload: Any) -> list[Observation]:
    """Parse Moonraker's ``lane_data`` database item into observations."""

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

    for key, lane in items:
        if isinstance(lane, str):
            try:
                lane = json.loads(lane)
            except (TypeError, ValueError):
                continue
        if not isinstance(lane, Mapping):
            continue

        lane_index = _moonraker_lane_index(lane)
        if lane_index is None or lane_index < 0:
            continue
        slot = lane_index + 1
        if slot in seen_slots:
            continue

        raw_material = lane.get("material")
        if not isinstance(raw_material, str):
            # safe_json_string returns empty for non-string values.
            continue
        material = _clean_material(raw_material)
        if not material:
            # Native sets has_filament from the material and skips empty lanes.
            continue

        vendor = _clean_vendor(
            lane.get("vendor") or lane.get("vendor_name") or lane.get("brand")
        )
        subtype = _clean_subtype(
            lane.get("subtype") or lane.get("sub_type") or lane.get("variant")
        )
        product = safe_str(lane.get("product"))
        raw_color = lane.get("color")
        color = _normalize_source_color(raw_color) if isinstance(raw_color, str) else ""
        filament_id_raw = lane.get("filament_id")
        # Orca only accepts a string here; Happy Hare's numeric Spoolman
        # filament id under the same key must be ignored.
        filament_id = filament_id_raw.strip() if isinstance(filament_id_raw, str) else ""
        spool_id = _positive_int(lane.get("spool_id"))
        bed_temp = _positive_int(lane.get("bed_temp"))
        nozzle_temp = _positive_int(lane.get("nozzle_temp"))
        tag_uid = safe_str(lane.get("tag_uid") or lane.get("uid") or lane.get("card_uid"))

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


def _moonraker_mmu_object(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    status = result.get("status")
    if not isinstance(status, Mapping):
        return None
    mmu = status.get("mmu")
    return mmu if isinstance(mmu, Mapping) else None


def _parse_happy_hare(payload: Any) -> list[Observation]:
    """Parse Happy Hare's ``/printer/objects/query?mmu`` response."""

    mmu = _moonraker_mmu_object(payload)
    if not mmu:
        return []
    num_gates = mmu.get("num_gates")
    if isinstance(num_gates, bool) or not isinstance(num_gates, (int, float)):
        return []
    try:
        gate_count = int(num_gates)
    except (TypeError, ValueError):
        return []
    if gate_count <= 0:
        return []

    arrays: dict[str, Any] = {}
    for key in ("gate_status", "gate_material", "gate_color", "gate_temperature"):
        value = mmu.get(key, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            return []
        arrays[key] = value

    observations: list[Observation] = []
    for gate_index in range(gate_count):
        def gate_value(key: str) -> Any:
            values = arrays[key]
            return values[gate_index] if gate_index < len(values) else None

        raw_status = gate_value("gate_status")
        if isinstance(raw_status, bool) or not isinstance(raw_status, (int, float)):
            continue
        status = int(raw_status)
        if status <= 0:
            continue
        raw_material = gate_value("gate_material")
        if not isinstance(raw_material, str):
            continue
        material = _clean_material(raw_material)
        if not material:
            continue
        raw_color = gate_value("gate_color")
        color = _normalize_source_color(raw_color) if isinstance(raw_color, str) else ""
        raw_nozzle = gate_value("gate_temperature")
        nozzle_temp = (
            _positive_int(raw_nozzle)
            if isinstance(raw_nozzle, (int, float)) and not isinstance(raw_nozzle, bool)
            else None
        )
        observations.append(
            Observation(
                slot=gate_index + 1,
                present=True,
                material=material,
                color=color,
                bed_temp=0,
                nozzle_temp=nozzle_temp,
                source=MOONRAKER_SOURCE,
                raw={"gate_index": gate_index, "mmu": dict(mmu)},
            )
        )
    return observations


def _creality_same_material_types(box: Mapping[str, Any]) -> dict[str, str]:
    entries = box.get("same_material")
    if not isinstance(entries, list):
        return {}
    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        material_id = entry[0]
        material_type = entry[3]
        if isinstance(material_id, str) and isinstance(material_type, str):
            if material_id and material_type:
                result.setdefault(material_id, material_type)
    return result


def _parse_creality_cfs(payload: Any) -> list[Observation]:
    """Parse Creality CFS's ``/printer/objects/query?box`` response."""

    if not isinstance(payload, Mapping):
        return []
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return []
    status = result.get("status")
    if not isinstance(status, Mapping):
        return []
    box = status.get("box")
    if not isinstance(box, Mapping):
        return []

    same_material = _creality_same_material_types(box)
    observations: list[Observation] = []
    for tray_index in range(1, 5):
        tray = box.get(f"T{tray_index}")
        if not isinstance(tray, Mapping):
            continue
        if safe_str(tray.get("state")) != "connect":
            continue
        material_types = tray.get("material_type")
        colors = tray.get("color_value")
        if not isinstance(material_types, list):
            material_types = []
        if not isinstance(colors, list):
            colors = []

        for slot_index in range(4):
            raw_material_id = (
                material_types[slot_index]
                if slot_index < len(material_types)
                else ""
            )
            material_id = raw_material_id if isinstance(raw_material_id, str) else ""
            material = _CREALITY_MATERIAL_TYPES.get(material_id, "")
            if not material:
                material = same_material.get(material_id, "")
            if not material:
                continue

            raw_color = colors[slot_index] if slot_index < len(colors) else ""
            color_text = raw_color if isinstance(raw_color, str) else ""
            if len(color_text) == 7 and color_text.startswith("0"):
                # Creality prefixes six-digit RGB colours with a leading zero.
                color_text = color_text[1:]
            color = _normalize_source_color(color_text)
            observations.append(
                Observation(
                    slot=(tray_index - 1) * 4 + slot_index + 1,
                    present=True,
                    material=safe_str(material),
                    color=color,
                    source=MOONRAKER_SOURCE,
                    raw={
                        "tray": f"T{tray_index}",
                        "slot_index": (tray_index - 1) * 4 + slot_index,
                        "material_id": material_id,
                    },
                )
            )
    observations.sort(key=lambda observation: observation.slot)
    return observations


def _parse_moonraker(payload: Any) -> list[Observation]:
    """Parse any of the native Moonraker filament sources.

    ``lane_data`` is the base schema; the Happy Hare ``mmu`` and Creality CFS
    ``box`` shapes are detected for callers that already have a payload.
    """

    mmu = _moonraker_mmu_object(payload)
    if mmu is not None:
        return _parse_happy_hare(payload)
    if isinstance(payload, Mapping):
        result = payload.get("result")
        if isinstance(result, Mapping):
            status = result.get("status")
            if isinstance(status, Mapping) and isinstance(status.get("box"), Mapping):
                return _parse_creality_cfs(payload)
    return _parse_moonraker_lane_data(payload)


def _read_moonraker(base_url: str, api_key: str, timeout: float) -> list[Observation]:
    """Try the native Moonraker sources in order; the first data source wins."""

    headers = {"X-Api-Key": api_key} if api_key else None
    endpoints = (
        (MOONRAKER_PATH, _parse_moonraker_lane_data, "lane_data"),
        (MOONRAKER_MMU_PATH, _parse_happy_hare, "mmu"),
        (MOONRAKER_BOX_PATH, _parse_creality_cfs, "box"),
    )
    errors: list[str] = []
    succeeded = False
    for path, parser, label in endpoints:
        try:
            payload = _request_json(
                _join_url(base_url, path),
                timeout=timeout,
                headers=headers,
            )
        except SourceError as exc:
            errors.append(f"{label}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one endpoint must not stop the fallback
            errors.append(f"{label}: unexpected error: {exc}")
            continue
        succeeded = True
        observations = parser(payload)
        if observations:
            return observations
    if not succeeded and errors:
        raise SourceError("; ".join(errors))
    return []


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
    port: Any = None,
    agent: str = "",
) -> tuple[list[Observation], list[str]]:
    """Read loaded filaments from a Snapmaker or Moonraker printer.

    The configured host and port are authoritative; this function never probes
    common ports.  With no ``agent`` hint both sources are attempted (legacy
    behaviour) and Snapmaker wins conflicts while Moonraker fills blanks.  When
    Orca reports an agent, only that native source family is queried:
    Snapmaker's ``print_task_config`` or Moonraker's lane_data -> mmu -> box.
    The return value is ``(observations, errors)`` and this function never
    raises because the printer is offline or returns bad JSON.
    """

    base = normalize_base_url(base_url, port=port)
    if not base:
        return [], ["no printer URL configured"]

    agent_text = safe_str(agent).casefold()
    moonraker_tokens = ("moonraker", "klipper", "creality", "happy", "mmu", "cfs")
    if "snapmaker" in agent_text:
        read_snapmaker, read_moonraker = True, False
    elif any(token in agent_text for token in moonraker_tokens):
        read_snapmaker, read_moonraker = False, True
    else:
        read_snapmaker, read_moonraker = True, True

    observations: list[Observation] = []
    moonraker_observations: list[Observation] = []
    errors: list[str] = []

    if read_snapmaker:
        try:
            observations = _read_snapmaker(base, api_key, timeout)
        except SourceError as exc:
            errors.append(f"{SNAPMAKER_SOURCE}: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad source must not crash the UI
            errors.append(f"{SNAPMAKER_SOURCE}: unexpected error: {exc}")

    if read_moonraker:
        try:
            moonraker_observations = _read_moonraker(base, api_key, timeout)
        except SourceError as exc:
            errors.append(f"{MOONRAKER_SOURCE}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{MOONRAKER_SOURCE}: unexpected error: {exc}")

    if moonraker_observations:
        observations = _merge_observations(observations, moonraker_observations)

    if not observations and not errors:
        if read_snapmaker and read_moonraker:
            errors.append("no filament slots reported by Snapmaker or Moonraker")
        elif read_snapmaker:
            errors.append("no filament slots reported by Snapmaker")
        else:
            errors.append("no filament slots reported by Moonraker")

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
        self.base_url = normalize_spoolman_url(base_url)
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

    @staticmethod
    def _extra_values_equal(left: Any, right: Any) -> bool:
        """Compare decoded extras without stringifying unrelated types.

        Spoolman stores ``true``, ``5`` and ``"5"`` as different values.  A
        plain ``str()`` comparison made ``"5"`` match an integer extra, which
        could attach the wrong filament preset.
        """

        if isinstance(left, bool) or isinstance(right, bool):
            return isinstance(left, bool) and isinstance(right, bool) and left == right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left == right
        if type(left) is not type(right):
            return False
        return left == right

    @staticmethod
    def _filament_page(payload: Any) -> tuple[list[Any], Mapping[str, Any]]:
        if isinstance(payload, list):
            return payload, {}
        if not isinstance(payload, Mapping):
            raise SourceError("Spoolman filament list response is not an array")
        for key in ("filaments", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value, payload
        raise SourceError("Spoolman filament list response is not an array")

    def find_filament_by_extra(self, key: str, value: Any) -> dict[str, Any] | None:
        """Find the first filament whose decoded ``extra[key]`` equals ``value``.

        Spoolman has no portable server-side filter for arbitrary extra fields,
        so this pages through the filament list and filters locally.  The first
        request keeps the legacy ``/api/v1/filament`` URL; later pages use
        ``limit``/``offset`` and stop on an empty/repeated page or when the
        response reports no more records.  It is a fallback, not part of the
        normal spool-id lookup path.
        """

        key_text = safe_str(key)
        if not key_text:
            raise SourceError("Spoolman extra key is empty")

        first_payload: Any = None
        first_error: SourceError | None = None
        try:
            first_payload = self._request("/api/v1/filament")
        except SourceError as exc:
            first_error = exc
        if first_payload is None and first_error is not None:
            # Some Spoolman deployments only paginate when limit is supplied.
            try:
                first_payload = self._request(
                    f"/api/v1/filament?limit={SPOOLMAN_PAGE_SIZE}&offset=0"
                )
            except SourceError:
                raise first_error

        offset = 0
        seen: set[str] = set()
        payload = first_payload
        for _page in range(1000):
            try:
                records, metadata = self._filament_page(payload)
            except SourceError:
                if offset == 0:
                    raise
                return None
            if not records:
                return None

            new_records = 0
            for item in records:
                if not isinstance(item, Mapping):
                    continue
                record_id = safe_str(item.get("id"))
                marker = record_id or repr(sorted(item.keys()))
                if marker in seen:
                    continue
                seen.add(marker)
                new_records += 1
                extras = self.extras(item)
                if key_text in extras and self._extra_values_equal(
                    extras[key_text], value
                ):
                    return dict(item)

            offset += len(records)
            total = safe_int(metadata.get("total"))
            if total is not None and offset >= total:
                return None
            if "has_more" in metadata and metadata.get("has_more") is False:
                return None
            if "next" in metadata and metadata.get("next") in (None, "", False):
                return None
            if new_records == 0:
                return None
            try:
                payload = self._request(
                    f"/api/v1/filament?limit={SPOOLMAN_PAGE_SIZE}&offset={offset}"
                )
            except SourceError:
                return None
        return None

    @classmethod
    def decode_extra_value(cls, value: Any) -> Any:
        """Decode one Spoolman extra value.

        Values are normally JSON-encoded strings (``"\\"Matte\\""``); plain
        strings that are not valid JSON are returned unchanged.  Byte strings
        and the older ``{"value": ...}`` wrapper are accepted too.
        """

        if isinstance(value, (bytes, bytearray)):
            try:
                value = bytes(value).decode("utf-8-sig")
            except UnicodeDecodeError:
                return value
        if isinstance(value, Mapping) and "value" in value:
            return cls.decode_extra_value(value["value"])
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                return value
            return decoded
        return value

    @classmethod
    def decode_extra(cls, raw: Any) -> dict[str, Any]:
        """Decode a whole ``extra`` object (mapping or list form)."""

        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = bytes(raw).decode("utf-8-sig")
            except UnicodeDecodeError:
                return {}
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


def _is_windows_source_path(value: str) -> bool:
    text = safe_str(value)
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", text)
        or text.startswith("\\\\")
        or text.startswith("//?/")
        or text.startswith("//./")
    )


def _is_local_source_path(value: Any) -> bool:
    text = safe_str(value)
    if not text:
        return False
    if text.casefold().startswith("file://"):
        return True
    if _is_windows_source_path(text):
        return True
    if os.path.isabs(text):
        return True
    return "://" not in text and os.path.exists(text)


def _local_source_path(value: Any) -> str:
    text = safe_str(value)
    if text.casefold().startswith("file://"):
        parsed = urllib.parse.urlsplit(text)
        path = urllib.request.url2pathname(parsed.path)
        if parsed.netloc and parsed.netloc.casefold() != "localhost":
            path = "//" + parsed.netloc + path
        return path
    return text


def _url_cache_tag(url: str) -> str:
    return hashlib.sha256(safe_str(url).encode("utf-8")).hexdigest()[:16]


def _scoped_cache_file(path: str | os.PathLike[str], tag: str) -> str:
    text = os.fspath(path)
    if text.endswith(("/", os.sep)) or os.path.isdir(text):
        return os.path.join(text, tag, "ofd_index.json")
    root, extension = os.path.splitext(text)
    if not extension:
        extension = ".json"
    return f"{root}-{tag}{extension}"


def _scoped_cache_dir(path: str | os.PathLike[str], tag: str) -> str:
    return os.path.join(os.fspath(path), tag)


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

    Only the index and the one chosen preset are ever fetched.  The index and
    preset JSON files are cached on disk (atomically) inside URL-scoped paths,
    so switching ``index_url`` cannot return another index's profiles.  The
    in-memory index also honours :attr:`cache_ttl`; :meth:`refresh` clears the
    memory index and the URL's preset cache before fetching again.
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
        self._cache_tag = _url_cache_tag(self.index_url)
        self._lock = threading.RLock()

        self._local_index = _is_local_source_path(self.index_url)
        self._local_index_path = (
            _local_source_path(self.index_url) if self._local_index else ""
        )
        self._windows_local_index = self._local_index and _is_windows_source_path(
            self._local_index_path
        )
        if self._local_index:
            directory = (
                ntpath.dirname(self._local_index_path)
                if self._windows_local_index
                else os.path.dirname(self._local_index_path)
            )
            separator = "\\" if self._windows_local_index else os.sep
            self._preset_base_url = directory.rstrip("/\\") + separator
        else:
            self._preset_base_url = _directory_url(self.index_url)

        self.cache_path_root = (
            os.fspath(cache_path) if cache_path is not None else None
        )
        self.cache_path = (
            _scoped_cache_file(self.cache_path_root, self._cache_tag)
            if self.cache_path_root
            else None
        )
        self._legacy_cache_path = (
            os.fspath(cache_path)
            if cache_path is not None
            and self.index_url.rstrip("/") == DEFAULT_OFD_INDEX_URL.rstrip("/")
            else None
        )
        self.preset_cache_root = (
            os.fspath(preset_cache_dir) if preset_cache_dir is not None else None
        )
        self.preset_cache_dir = (
            _scoped_cache_dir(self.preset_cache_root, self._cache_tag)
            if self.preset_cache_root
            else None
        )
        self._legacy_preset_cache_dir = (
            self.preset_cache_root
            if self.preset_cache_root
            and self.index_url.rstrip("/") == DEFAULT_OFD_INDEX_URL.rstrip("/")
            else None
        )
        self.cache_ttl = max(0.0, float(cache_ttl))
        self.timeout = timeout
        self._opener = opener
        self._index: dict[str, Any] | None = None
        self._profiles: list[Mapping[str, Any]] = []
        self._presets: dict[str, dict[str, Any]] = {}
        self._loaded_at = 0.0
        self.last_error = ""
        self.cache_error = ""

    # -- index cache ------------------------------------------------------

    def _memory_is_fresh(self) -> bool:
        if self._index is None or self.cache_ttl <= 0 or self._loaded_at <= 0:
            return False
        return (time.time() - self._loaded_at) < self.cache_ttl

    def _cache_read_paths(self) -> list[str]:
        if self._legacy_cache_path:
            # The unscoped file is the compatibility marker used by the entry
            # point's "Clear OFD cache".  If it is gone, a leftover scoped file
            # must not resurrect a cache the user just cleared.
            if not os.path.isfile(self._legacy_cache_path):
                return []
            if self.cache_path and os.path.isfile(self.cache_path):
                return [self.cache_path]
            return [self._legacy_cache_path]
        return [self.cache_path] if self.cache_path else []

    def _cache_is_fresh(self) -> bool:
        if self.cache_ttl <= 0:
            return False
        for path in self._cache_read_paths():
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                continue
            if age < self.cache_ttl:
                return True
        return False

    def _read_cache(self) -> dict[str, Any] | None:
        for path in self._cache_read_paths():
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    payload = json.load(handle)
            except (OSError, TypeError, ValueError):
                continue
            try:
                return _normalize_ofd_index(payload)
            except SourceError:
                continue
        return None

    def _write_cache_file(self, path: str, payload: Mapping[str, Any]) -> None:
        parent = os.path.dirname(os.path.abspath(path)) or "."
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
            os.replace(temporary_path, path)
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

    def _write_cache(self, payload: Mapping[str, Any]) -> None:
        if not self.cache_path:
            return
        self._write_cache_file(self.cache_path, payload)
        if self._legacy_cache_path and self._legacy_cache_path != self.cache_path:
            self._write_cache_file(self._legacy_cache_path, payload)

    def _install_index(self, payload: Any) -> None:
        normalized = _normalize_ofd_index(payload)
        profiles = [
            profile
            for profile in normalized.get("profiles", [])
            if isinstance(profile, Mapping)
        ]
        with self._lock:
            # Publish the profile list before the index object so a concurrent
            # reader never observes a new index with an old (empty) profile set.
            self._profiles = profiles
            self._index = normalized
            self._loaded_at = time.time()

    def load_index(self, force: bool = False) -> dict[str, Any]:
        """Load the index from memory, the cache, or the network."""

        if not force and self._memory_is_fresh():
            with self._lock:
                if self._index is not None:
                    return self._index

        cached = self._read_cache()
        if not force and cached is not None and self._cache_is_fresh():
            self._install_index(cached)
            self.last_error = ""
            return self._index  # type: ignore[return-value]

        try:
            payload = _request_json(
                self.index_url,
                timeout=self.timeout,
                opener=self._opener,
            )
            self._install_index(payload)
            with self._lock:
                index = self._index
            assert index is not None
            self._write_cache(index)
            self.last_error = ""
            return index
        except SourceError as exc:
            self.last_error = str(exc)
            with self._lock:
                fallback = cached if cached is not None else self._index
            if fallback is not None:
                self._install_index(fallback)
                assert self._index is not None
                return self._index
            raise

    def _clear_preset_cache(self) -> None:
        directories = [self.preset_cache_dir]
        if (
            self._legacy_preset_cache_dir
            and self._legacy_preset_cache_dir != self.preset_cache_dir
        ):
            directories.append(self._legacy_preset_cache_dir)
        for directory in directories:
            if not directory:
                continue
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.cache_error = str(exc)

    def refresh(self) -> dict[str, Any]:
        """Force a network refresh and discard this URL's memory/preset cache."""

        with self._lock:
            self._index = None
            self._profiles = []
            self._loaded_at = 0.0
            self._presets = {}
            self._clear_preset_cache()
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
        with self._lock:
            profiles = list(self._profiles)

        scored: list[tuple[int, bool, Mapping[str, Any]]] = []
        for entry in profiles:
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

    def _preset_url(self, path: str) -> str:
        clean_path = safe_str(path)
        if self._local_index:
            parts = [part for part in clean_path.split("/") if part]
            if self._windows_local_index:
                return ntpath.join(
                    ntpath.dirname(self._local_index_path), *parts
                )
            return os.path.join(
                os.path.dirname(self._local_index_path), *parts
            )
        return urllib.parse.urljoin(
            self._preset_base_url,
            urllib.parse.quote(clean_path, safe="/._-~"),
        )

    def _preset_cache_path(self, path: str) -> str:
        if not self.preset_cache_dir:
            return ""
        parts = [part for part in safe_str(path).split("/") if part]
        if not parts or any(part in (".", "..") or "\\" in part for part in parts):
            return ""
        return os.path.join(self.preset_cache_dir, *parts)

    def _legacy_preset_cache_path(self, path: str) -> str:
        """Read-only path used before caches were scoped by index URL."""

        if not self._legacy_preset_cache_dir:
            return ""
        parts = [part for part in safe_str(path).split("/") if part]
        if not parts or any(part in (".", "..") or "\\" in part for part in parts):
            return ""
        return os.path.join(self._legacy_preset_cache_dir, *parts)

    def _read_cached_preset(self, cache_path: str) -> dict[str, Any] | None:
        if not cache_path:
            return None
        try:
            with open(cache_path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None
        preset = dict(payload)
        if self.validate_preset(preset):
            return None
        return preset

    def preset_is_cached(self, candidate: Mapping[str, Any] | str) -> bool:
        """Whether a valid preset JSON is already on disk and needs no fetch."""

        if isinstance(candidate, Mapping):
            path = safe_str(candidate.get("path"))
        else:
            path = safe_str(candidate)
        if self._read_cached_preset(self._preset_cache_path(path)) is not None:
            return True
        return self._read_cached_preset(self._legacy_preset_cache_path(path)) is not None

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
            or any(part in ("", ".", "..") or "\\" in part for part in parts)
            or urllib.parse.urlsplit(clean_path).scheme
        ):
            raise SourceError(f"invalid OFD preset path: {path!r}")

        with self._lock:
            cached = self._presets.get(clean_path)
        if cached is not None and not self.validate_preset(cached):
            return copy.deepcopy(cached)

        cache_path = self._preset_cache_path(clean_path)
        cached_preset = self._read_cached_preset(cache_path)
        if cached_preset is None:
            cached_preset = self._read_cached_preset(
                self._legacy_preset_cache_path(clean_path)
            )
        if cached_preset is not None:
            with self._lock:
                self._presets[clean_path] = cached_preset
            return copy.deepcopy(cached_preset)

        url = self._preset_url(clean_path)
        payload = _request_json(url, timeout=self.timeout, opener=self._opener)
        if not isinstance(payload, Mapping):
            raise SourceError(f"OFD preset {clean_path} is not a JSON object")
        preset = dict(payload)
        valid = not self.validate_preset(preset)
        if valid:
            with self._lock:
                self._presets[clean_path] = preset
            if cache_path:
                self._write_preset_cache(cache_path, preset)
        return copy.deepcopy(preset)

    @staticmethod
    def validate_preset(preset: Any) -> list[str]:
        """Return installation problems for a fetched preset.

        The entry point calls this before writing a preset; :meth:`fetch`
        itself only requires a JSON object so callers can inspect odd payloads
        before deciding what to do.  A user ``filament_id`` is legitimate (it
        is how a root/local preset participates in native AMS sync), so it is
        not rejected; the other system identity keys remain forbidden.
        """

        if not isinstance(preset, Mapping):
            return ["preset is not a JSON object"]
        problems: list[str] = []
        preset_type = preset.get("type")
        if preset_type is not None and safe_str(preset_type).casefold() not in (
            "",
            "filament",
        ):
            problems.append("not a filament preset")
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
            "filament_ids",
            "base_id",
            "is_system",
            "system",
            "bundle_id",
        ):
            if key in preset:
                problems.append(f"system-only key present: {key}")
        return problems
