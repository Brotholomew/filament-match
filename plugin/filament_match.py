# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# id = "filament-match"
# name = "Filament Match"
# description = "Matches the filament loaded in each printer slot to your installed OrcaSlicer presets or a ready-to-use Open Filament Database profile, with optional Spoolman enrichment and per-kg price sync."
# author = "Filament Match contributors"
# version = "0.8.1"
# ///
"""Filament Match.

Run from the Plugins dialog or Speed Dial. The plugin opens a docked panel,
reads the filaments reported by the active Moonraker or Snapmaker printer,
resolves product names through the Open Filament Database and optionally spool
IDs through Spoolman, and matches every slot to a compatible preset from the
installed Orca library or the Open Filament Database.

Scanning runs automatically when the panel opens. It only selects the best
candidate in each row; nothing is written to Orca until the user presses the
top Apply button. Choosing a preset or pressing Match changes the selection
only, and Match re-runs the pipeline from the expanded fields. OrcaSlicer does
not let a Python plugin override or hook its native "Sync filaments" button.

Confirmed mappings live in the plugin's own storage (history.json). The plugin
does not treat Orca's automatic generic fallback as previous usage.

The plugin is split into three modules during development:

    filament_match.py          this file - action, UI, Orca host calls
    filament_match_core.py     matching, history, value types
    filament_match_sources.py  Moonraker, Spoolman and OFD adapters

Install all three next to each other under orca_plugins/filament_match/, or use
the single-file build produced by build_single_file.py.
"""

import dataclasses
import hashlib
import json
import math
import os
import queue
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

try:
    import orca
except ImportError:  # syntax checks and unit tests outside OrcaSlicer
    orca = None

# Orca loads a local plugin as a package folder, but it does not add that folder
# to sys.path for imports of sibling modules.
sys.path.insert(0, os.path.dirname(__file__))

PLUGIN_VERSION = "0.8.1"
BUNDLE_ID = "filament_match"
BUNDLE_NAME = "Filament Match Library"
DEFAULT_OFD_INDEX = "https://api.openfilamentdatabase.org/orcaslicer/index.json"

OBSERVATION_FIELDS = (
    "slot", "present", "vendor", "material", "subtype", "product", "color",
    "filament_id", "spool_id", "tag_uid", "bed_temp", "nozzle_temp", "source",
)

# Keys that identify a system preset. A copied user or local-bundle preset must
# not carry them: Orca rejects a user preset with a foreign filament_id or
# setting_id, and a local template must not claim to be a system profile.
SYSTEM_ONLY_KEYS = {
    "setting_id", "filament_id", "filament_ids", "base_id", "is_system",
    "system", "bundle_id",
}

BAD_FILENAME_CHARS = set('\\/:*?"<>|')
PRESET_FILE_CACHE = {}


def _load_dependencies():
    """Import the sibling modules.

    Kept in a function so one missing module leaves this entry importable and
    produces a useful error when the action is run, instead of failing at load.
    """
    from filament_match_core import Candidate, HistoryStore, MatchResult, Observation, PresetInfo, match_observation
    from filament_match_sources import OfdLibrary, SpoolmanClient, read_printer
    return {
        "Candidate": Candidate,
        "HistoryStore": HistoryStore,
        "MatchResult": MatchResult,
        "Observation": Observation,
        "PresetInfo": PresetInfo,
        "match_observation": match_observation,
        "OfdLibrary": OfdLibrary,
        "SpoolmanClient": SpoolmanClient,
        "read_printer": read_printer,
    }


def _load_optional_helpers():
    """identity_key / score_preset are useful but not required by the plan."""
    try:
        from filament_match_core import identity_key, score_preset
    except Exception:
        return None, None
    return identity_key, score_preset


_DEPS = {}
_DEPS_ERROR = ""
try:
    _DEPS = _load_dependencies()
except Exception as error:  # ImportError, SyntaxError, anything from a bad install
    _DEPS_ERROR = "%s: %s" % (type(error).__name__, error)

_IDENTITY_KEY, _SCORE_PRESET = _load_optional_helpers()


# ---------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------

def _deps_error():
    return (
        "Filament Match is missing a plugin module (%s). Install filament_match_core.py "
        "and filament_match_sources.py next to filament_match.py, or use the single-file "
        "build." % (_DEPS_ERROR or "unknown import error")
    )


def _require_deps():
    if not _DEPS:
        raise RuntimeError(_deps_error())


def _error_text(error):
    text = str(error).strip()
    return text or type(error).__name__


def _permission_hint(value):
    """Explain Orca's audit denial, which is retryable after the user approves.

    Network calls run on the background Script worker. Orca's audit dialog is
    posted to the UI thread asynchronously and the first event is denied
    (fail closed), so the user has to approve it and try again.
    """
    text = _error_text(value).casefold()
    blocked = ("permission denied" in text or "permissionerror" in text
               or "errno 13" in text or "without permission" in text
               or "permission required" in text
               or ("audit" in text and "denied" in text))
    if not blocked:
        return ""
    return ("Orca blocked this network request until you approve its permission "
            "dialog. Approve it (the dialog can appear behind the main window), "
            "then press the rescan (↻) button again.")


_PREFLIGHTED_HOSTS = set()


def _preflight_host(base_url, timeout=4.0):
    """Approach one host at its bare origin so Orca's grant is host-wide.

    Orca stores a reactive network grant as the exact URL it saw. If the first
    request is ``http://host:7125/api/...``, every different path prompts again.
    A request to ``http://host:7125`` produces a canonical scheme://host grant,
    which Orca treats as host-wide. The first request may be denied while the
    permission dialog is open; the caller reports that and the user rescans.
    """
    base = _text(base_url).rstrip("/")
    if not base:
        return
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return
    origin = "%s://%s" % (parts.scheme, parts.netloc)
    if origin in _PREFLIGHTED_HOSTS:
        return
    try:
        response = urllib.request.urlopen(origin, timeout=timeout)
        try:
            response.close()
        except Exception:
            pass
    except urllib.error.HTTPError:
        pass
    except Exception as error:
        detail = "%s %s" % (error, getattr(error, "reason", ""))
        hint = _permission_hint(detail)
        if hint:
            # Remember the attempt for this session. A scan builds many rows;
            # without this every row would hit the same denied origin and
            # re-open the permission dialog. The real request still retries.
            _PREFLIGHTED_HOSTS.add(origin)
            raise RuntimeError(hint)
        # A connection failure is reported by the real request with more detail.
        return
    _PREFLIGHTED_HOSTS.add(origin)


def _warn(warnings, text):
    text = str(text or "").strip()
    if text and text not in warnings:
        warnings.append(text)


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value).strip()


def _int_or_none(value):
    if value is None or value == "":
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float_or_none(value):
    if value is None or value == "":
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _price_value(value):
    """Format a non-negative price as a two-decimal string, or ''."""
    number = _float_or_none(value)
    if number is None or number < 0:
        return ""
    return "%.2f" % number


def _spoolman_cost_per_kg(spool, filament):
    """Spoolman spool/filament price -> Orca filament_cost (currency/kg).

    Spoolman's spool.price and filament.price are whole-spool prices. Orca's
    filament_cost is per kilogram, so divide by the net filament weight.
    """
    price = _float_or_none((spool or {}).get("price"))
    if price is None:
        price = _float_or_none((filament or {}).get("price"))
    if price is None or price < 0:
        return ""
    weight = _float_or_none((spool or {}).get("initial_weight"))
    if weight is None or weight <= 0:
        weight = _float_or_none((filament or {}).get("weight"))
    if weight is None or weight <= 0:
        return ""
    return _price_value(price / (weight / 1000.0))


def _first(value):
    """First value of a serialized Orca config value: '"a";"b"' -> 'a'."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _first(value[0]) if value else ""
    text = str(value).strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 0:
            return text[1:end]
    return text.split(";", 1)[0].strip()


def _plain(value):
    """A human-readable config value for the read-only detail table."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _jsonable(value, depth=0):
    if depth > 6:
        return ""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, depth + 1) for item in value]
    if hasattr(value, "__dict__"):
        return {str(key): _jsonable(item, depth + 1) for key, item in vars(value).items()}
    return str(value)


def _normalize_color(value):
    """'#RRGGBB' from '#rrggbb', 'RRGGBB', '0xRRGGBB' or an RGBA value."""
    text = _text(value)
    if text[:2].lower() == "0x":
        text = text[2:]
    text = text.lstrip("#")
    if len(text) == 8:
        text = text[:6]
    return "#" + text.upper() if re.fullmatch(r"[0-9A-Fa-f]{6}", text) else ""


def _base_name(name, bundle_id=""):
    """Preset name without its bundle prefix or ' @Printer' suffix."""
    name = _text(name)
    if bundle_id:
        prefix = "_local/%s/" % bundle_id
        if name.startswith(prefix):
            name = name[len(prefix):]
        else:
            name = name.rsplit("/", 1)[-1]
    return name.split(" @", 1)[0].strip()


def _selector_name(preset):
    """The name the slot selector accepts: label/alias, not internal name.

    For a preset loaded from _local/<id>/, Preset.name is prefixed
    '_local/<id>/...', while begin_update() resolves the visible label/alias.
    Keep Preset.name as the stable identity, and carry this label separately.
    """
    internal = _text(_member(preset, "name", ""))
    alias = _text(_member(preset, "alias", ""))
    if alias:
        return alias
    labeller = getattr(preset, "label", None)
    if callable(labeller):
        try:
            label = _text(labeller())
            if label:
                return label
        except Exception:
            pass
    return internal


def _flag(obj, name, default=False):
    """Read a boolean member that may be a property or an is_x() method."""
    try:
        value = getattr(obj, name, default)
        if callable(value):
            value = value()
        return bool(value)
    except Exception:
        return bool(default)


def _member(obj, name, default=None):
    try:
        value = getattr(obj, name, default)
        return value() if callable(value) else value
    except Exception:
        return default


def _provenance(system, user, bundle_id):
    if system:
        return "system"
    if bundle_id:
        return "bundle"
    if user:
        return "user"
    return "preset"


# ---------------------------------------------------------------------------
# Atomic JSON files and plugin storage
# ---------------------------------------------------------------------------

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value
    except (OSError, ValueError):
        return default


def _write_json_atomic(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    os.close(descriptor)
    try:
        # Re-open by path: os.fdopen() produces a targetless audit "open" event
        # that Orca denies even inside the plugin's allowed storage folder.
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=4)
            stream.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _data_dir_from_plugin():
    """The Orca data directory, derived from an installed plugin path."""
    parts = os.path.abspath(__file__).replace("\\", "/").split("/")
    if "orca_plugins" in parts:
        return "/".join(parts[:parts.index("orca_plugins")])
    return ""


_USER_SECTIONS = ("filament", "machine", "process")


def _user_path(path):
    """(data directory, active user folder) for a preset file path.

    The path must be ``.../user/<folder>/{filament,machine,process}/...`` or a
    local bundle below that. Searching from the left for any ``user`` component
    would mistake Linux home paths such as ``/home/user/...`` for Orca's data
    folder, so only a real preset-layout suffix counts.
    """
    normalized = os.path.normpath(_text(path)).replace("\\", "/")
    parts = normalized.split("/")
    for index, part in enumerate(parts):
        if part != "user" or index + 2 >= len(parts):
            continue
        section = parts[index + 2]
        if section in _USER_SECTIONS:
            return "/".join(parts[:index]), parts[index + 1]
        if (section == "_local" and index + 4 < len(parts)
                and parts[index + 4] in _USER_SECTIONS):
            return "/".join(parts[:index]), parts[index + 1]
    return "", ""


def _settings_path(storage):
    return os.path.join(storage, "settings.json")


def _history_path(storage):
    return os.path.join(storage, "history.json")


def _default_settings():
    return {
        "spoolman_url": "",
        "ofd_enabled": True,
        "ofd_index_url": DEFAULT_OFD_INDEX,
        "ignored": [],
    }


def _normalize_settings(raw):
    defaults = _default_settings()
    raw = raw if isinstance(raw, dict) else {}
    for key in ("spoolman_url", "ofd_index_url"):
        value = _text(raw.get(key))
        if key == "ofd_index_url" and not value:
            value = DEFAULT_OFD_INDEX
        defaults[key] = value.rstrip("/") if value else ""
    flag = raw.get("ofd_enabled", True)
    if flag is None:
        flag = True
    if isinstance(flag, str):
        flag = flag.strip().lower() in ("1", "true", "yes", "on")
    defaults["ofd_enabled"] = bool(flag)
    ignored, seen = [], set()
    for item in raw.get("ignored") or []:
        item = _text(item)
        if item and item not in seen:
            seen.add(item)
            ignored.append(item)
    defaults["ignored"] = ignored[:500]
    return defaults


def _load_settings(storage):
    data = _read_json(_settings_path(storage), {})
    return _normalize_settings(data)


def _save_settings(storage, settings):
    settings = _normalize_settings(settings)
    _write_json_atomic(_settings_path(storage), dict(settings, version=1))
    return settings


def _merge_settings(base, override):
    """Overlay the fields the page sends (URLs, OFD) on stored settings.

    The page intentionally does not send the muted-identity list with every
    request, so treating its payload as a complete settings document would
    erase muted filaments whenever the user saves or scans.
    """
    merged = _normalize_settings(base)
    override = override if isinstance(override, dict) else {}
    for key in ("spoolman_url", "ofd_index_url"):
        if key in override:
            merged[key] = _text(override.get(key))
    if "ofd_index_url" in override and not _text(merged.get("ofd_index_url")):
        merged["ofd_index_url"] = DEFAULT_OFD_INDEX
    if "ofd_enabled" in override:
        flag = override.get("ofd_enabled")
        if isinstance(flag, str):
            flag = flag.strip().lower() in ("1", "true", "yes", "on")
        merged["ofd_enabled"] = bool(flag)
    if "ignored" in override:
        merged["ignored"] = _normalize_settings({"ignored": override.get("ignored")})["ignored"]
    return _normalize_settings(merged)


# ---------------------------------------------------------------------------
# Preset snapshot (UI thread only)
# ---------------------------------------------------------------------------

def _preset_file_json(path):
    """Parse one preset file, cached by path, mtime and size."""
    path = _text(path)
    if not path:
        return {}
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    key = (path, stat.st_mtime_ns, stat.st_size)
    cached = PRESET_FILE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if len(PRESET_FILE_CACHE) > 4096:
        PRESET_FILE_CACHE.clear()
    PRESET_FILE_CACHE[key] = data
    return data


def _preset_value(preset, file_data, keys):
    for key in keys:
        try:
            value = _first(preset.config_value(key))
        except Exception:
            value = ""
        if value:
            return value
    for key in keys:
        value = _first(file_data.get(key))
        if value:
            return value
    return ""


def _make_preset_info(values):
    cls = _DEPS["PresetInfo"]
    try:
        return cls(**values)
    except TypeError:
        try:
            allowed = {field.name for field in dataclasses.fields(cls)}
        except TypeError:
            raise
        return cls(**{key: value for key, value in values.items() if key in allowed})


def _bundle_config_value(bundle, key):
    """Read one flattened preset value without relying on a preset file."""
    getter = _member(bundle, "full_config_value", None)
    if not callable(getter):
        return ""
    try:
        return _first(getter(key))
    except Exception:
        return ""


def _selected_printer_info():
    """The printer selected in Orca's Device tab, as a JSON-safe dict.

    orca.host.selected_printer() is UI-thread-only and exposes no address by
    design. The address comes from the selected printer preset below.
    """
    if orca is None:
        return None
    value = _member(getattr(orca, "host", None), "selected_printer", None)
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ("agent", "model_id", "connection", "online", "connected", "firmware"):
        item = value.get(key)
        result[key] = item if isinstance(item, bool) else _text(item)
    return result


def _first_number(value):
    text = _first(value)
    match = re.search(r"\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""


def _printer_nozzle(printer_preset, printer_name):
    if printer_preset is not None:
        value = _first_number(_preset_value(printer_preset, {}, ("nozzle_diameter",)))
        if value:
            return value
    match = re.search(r"\(([\d.]+)\s*nozzle\)", _text(printer_name), re.I)
    return match.group(1) if match else ""


def _preset_nozzle(name):
    match = re.search(r"(\d+(?:\.\d+)?)\s*nozzle", _text(name), re.I)
    return match.group(1) if match else ""


def _nozzles_match(preset_nozzle, printer_nozzle):
    if not preset_nozzle or not printer_nozzle:
        return True
    try:
        return abs(float(preset_nozzle) - float(printer_nozzle)) < 0.001
    except (TypeError, ValueError):
        return preset_nozzle == printer_nozzle


def _printer_connection(bundle, printer_preset, printer_name):
    """Build the plugin's view of the active printer.

    Orca keeps the print host/API key in the selected printer preset (or in the
    flattened preset config when a physical printer is active). The plugin uses
    that instead of asking the user for an IP. It never displays the API key.
    """
    host = ""
    if printer_preset is not None:
        host = _preset_value(printer_preset, {}, ("print_host",))
        if not host:
            host = _preset_value(printer_preset, {}, ("print_host_webui",))
    if not host:
        host = _bundle_config_value(bundle, "print_host")
    if not host:
        host = _bundle_config_value(bundle, "print_host_webui")

    port = ""
    if printer_preset is not None:
        port = _preset_value(printer_preset, {}, ("printhost_port",))
    if not port:
        port = _bundle_config_value(bundle, "printhost_port")

    api_key = ""
    if printer_preset is not None:
        api_key = _preset_value(printer_preset, {}, ("printhost_apikey",))
    if not api_key:
        api_key = _bundle_config_value(bundle, "printhost_apikey")

    selected = _selected_printer_info()
    connected = bool(selected.get("connected")) if selected else False
    online = bool(selected.get("online")) if selected else False
    return {
        "name": _text(printer_name),
        "host": _text(host),
        "port": _text(port),
        "api_key": _text(api_key),
        "selected": selected,
        "agent": _text(selected.get("agent")) if selected else "",
        "connection": _text(selected.get("connection")) if selected else "",
        "connected": connected,
        "online": online,
        "available": bool(_text(host)),
    }


def _printer_problem(info):
    """A user-facing reason the active printer cannot be scanned yet, or ''."""
    info = info or {}
    selected = info.get("selected")
    if not _text(info.get("host")):
        if selected:
            return ("The printer selected in OrcaSlicer does not expose a Moonraker "
                    "or print-host address. Filament Match needs a printer with a "
                    "print host configured in its printer preset.")
        return ("No printer is selected in OrcaSlicer. Connect a printer in the Device "
                "tab (or select a printer preset with a print host), then press the "
                "rescan (↻) button.")
    if selected and not (selected.get("connected") or selected.get("online")):
        return ("The printer selected in OrcaSlicer is not connected. Connect it in "
                "the Device tab, then press the rescan (↻) button.")
    return ""


def _public_printer_info(info):
    """The printer fields the page may see. The API key stays on the worker."""
    info = info or {}
    return {key: info.get(key) for key in (
        "name", "host", "port", "selected", "agent", "connection",
        "connected", "online", "available")}


def _preset_snapshot(bundle):
    """A plain snapshot of all filament presets. Runs on the UI thread."""
    _require_deps()
    collection = bundle.filaments
    presets, ui_presets = [], []
    select_names = {}
    folder, data_dir = "", ""
    slots = _member(bundle, "current_filament_preset_names", []) or []
    if not isinstance(slots, (list, tuple)):
        slots = []
    printer_preset = _member(bundle, "current_printer_preset", None)
    printer = _text(_member(printer_preset, "name", "")) if printer_preset is not None else ""
    printer_info = _printer_connection(bundle, printer_preset, printer)
    printer_nozzle = _printer_nozzle(printer_preset, printer)

    # A local-bundle install needs the active user profile folder. Filament
    # presets often come only from system bundles (with no readable user path),
    # but the selected printer preset is usually a user machine preset under
    # user/<folder>/machine/. Infer the folder from it instead of hardcoding
    # "default", which is wrong on UUID-profile installs.
    if printer_preset is not None:
        found_data, found_folder = _user_path(
            _text(_member(printer_preset, "file", "")))
        if found_folder:
            data_dir = found_data
            folder = found_folder

    for index in range(int(collection.size())):
        preset = collection.preset(index)
        if _flag(preset, "is_default"):
            continue
        name = _text(_member(preset, "name", ""))
        if not name:
            continue
        system = _flag(preset, "is_system")
        user = _flag(preset, "is_user")
        bundle_id = _text(_member(preset, "bundle_id", ""))
        visible = _flag(preset, "is_visible", True)
        compatible_flag = _flag(preset, "is_compatible", True)
        selector = _selector_name(preset) or name
        file_path = _text(_member(preset, "file", ""))

        # Only user presets and local bundles have readable JSON. System
        # presets are usually served from .opc vendor caches in 2.5.2, so do
        # not touch their file path and do not rely on filament_id for them.
        readable = bool(user or bundle_id)
        file_data = _preset_file_json(file_path) if readable else {}
        if file_path and readable and not folder:
            found_data, found_folder = _user_path(file_path)
            if found_folder:
                data_dir = data_dir or found_data
                folder = found_folder

        vendor = _preset_value(preset, file_data, ("filament_vendor", "vendor"))
        material = _preset_value(preset, file_data, ("filament_type", "material"))
        subtype = _preset_value(
            preset, file_data, ("filament_sub_type", "filament_subtype", "subtype"))
        color = _normalize_color(_preset_value(
            preset, file_data,
            ("filament_colour", "filament_color", "default_filament_colour", "color")))
        # config_value("filament_id") is None in 2.5.2; a user/local-bundle
        # preset file may still carry one. Leave it blank otherwise.
        filament_id = _preset_value(
            preset, file_data, ("filament_id", "filament_ids"))
        cost = _price_value(_preset_value(
            preset, file_data, ("filament_cost", "cost")))
        inherits = (_text(file_data.get("inherits")) or
                    _text(preset.config_value("inherits")))
        base = _base_name(selector, bundle_id)
        rank = 2 if system else 1 if bundle_id else 0
        provenance = _provenance(system, user, bundle_id)
        preset_nozzle = _preset_nozzle(name)
        is_compatible = bool(
            compatible_flag and visible and _nozzles_match(preset_nozzle, printer_nozzle))

        info = _make_preset_info({
            "name": name,
            "base": base,
            "vendor": vendor,
            "material": material,
            "subtype": subtype,
            "color": color,
            "filament_id": filament_id,
            "compatible": is_compatible,
            "system": system,
            "user": user,
            "bundle_id": bundle_id,
            "file": file_path,
            "rank": rank,
            "config": {
                "filament_cost": cost,
                "inherits": inherits,
            },
        })
        # PresetInfo is a plain dataclass; the label is an extra attribute so
        # candidates returned by the matcher keep it without another lookup.
        try:
            info.select_name = selector
        except Exception:
            pass
        presets.append(info)
        select_names[name] = selector
        if is_compatible:
            ui_presets.append({
                "name": name,
                "select_name": selector,
                "base": base,
                "vendor": vendor,
                "material": material,
                "subtype": subtype,
                "color": color,
                "filament_id": filament_id,
                "cost": cost,
                "inherits": inherits,
                "prov": provenance,
                "rank": rank,
                "system": system,
                "user": user,
                "bundle_id": bundle_id,
                "visible": visible,
                "nozzle": preset_nozzle,
                "compatible": True,
            })

    # Resolve inherited costs for local/user presets. A plugin-created OFD
    # preset usually has no own filament_cost; its effective price comes from
    # the parent (Generic PLA/PETG/ABS).
    ui_by_name = {item["name"]: item for item in ui_presets}
    by_name = {item.name: item for item in presets}
    for _pass in range(8):
        changed = False
        for item in presets:
            if item.config.get("filament_cost"):
                continue
            parent = by_name.get(_text(item.config.get("inherits")))
            if parent is None:
                continue
            parent_cost = _text(parent.config.get("filament_cost"))
            if not parent_cost:
                continue
            item.config["filament_cost"] = parent_cost
            ui_item = ui_by_name.get(item.name)
            if ui_item is not None:
                ui_item["cost"] = parent_cost
            changed = True
        if not changed:
            break

    folder_inferred = bool(folder)
    if not data_dir:
        data_dir = _data_dir_from_plugin()
    if not folder:
        folder = "default"
    ui_presets.sort(key=lambda item: (item["rank"], item["base"].casefold(),
                                      item["select_name"].casefold(), item["name"].casefold()))
    compatible = [preset for preset in presets if preset.compatible]
    return {
        "presets": presets,
        "compatible": compatible,
        "ui_presets": ui_presets,
        "select_names": select_names,
        "slots": list(slots),
        "printer": printer,
        "printer_info": printer_info,
        "printer_nozzle": printer_nozzle,
        "folder": folder,
        "folder_inferred": folder_inferred,
        "data_dir": data_dir,
    }


def _find_preset(presets, name):
    name = _text(name)
    if not name:
        return None
    for preset in presets:
        if preset.name == name:
            return preset
    folded = name.casefold()
    same = [preset for preset in presets if preset.name.casefold() == folded]
    if same:
        return min(same, key=lambda preset: getattr(preset, "rank", 9))
    wanted = _base_name(name)
    same = [preset for preset in presets if _base_name(preset.name, preset.bundle_id).casefold()
            == wanted.casefold()]
    return min(same, key=lambda preset: getattr(preset, "rank", 9)) if same else None


def _find_live_preset(bundle, name):
    collection = bundle.filaments
    try:
        found = collection.find_preset(name)
    except Exception:
        found = None
    if found is not None:
        return found
    folded = _text(name).casefold()
    if not folded:
        return None
    for index in range(int(collection.size())):
        preset = collection.preset(index)
        internal = _text(_member(preset, "name", ""))
        selector = _selector_name(preset)
        if internal.casefold() == folded or selector.casefold() == folded:
            return preset
    for index in range(int(collection.size())):
        preset = collection.preset(index)
        if _base_name(_selector_name(preset)).casefold() == _base_name(name).casefold():
            return preset
    return None


def _live_preset_detail(bundle, name):
    preset = _find_live_preset(bundle, name)
    if preset is None:
        return None
    system = _flag(preset, "is_system")
    user = _flag(preset, "is_user")
    bundle_id = _text(_member(preset, "bundle_id", ""))
    selector = _selector_name(preset) or _text(_member(preset, "name", ""))
    file_path = _text(_member(preset, "file", ""))
    # System preset files are normally absent (OPC caches); only user and
    # local-bundle presets have readable JSON in 2.5.2.
    file_data = _preset_file_json(file_path) if (user or bundle_id) else {}
    config = []
    try:
        keys = sorted(preset.config_keys() or [])
    except Exception:
        keys = []
    for key in keys:
        try:
            value = _first(preset.config_value(key))
        except Exception:
            value = ""
        config.append([key, value])
    return {
        "name": _text(_member(preset, "name", "")),
        "select_name": selector,
        "base": _base_name(selector, bundle_id),
        "prov": _provenance(system, user, bundle_id),
        "system": system,
        "user": user,
        "bundle_id": bundle_id,
        "file": file_path,
        "compatible": _flag(preset, "is_compatible", True),
        "visible": _flag(preset, "is_visible", True),
        "vendor": _preset_value(preset, file_data, ("filament_vendor", "vendor")),
        "material": _preset_value(preset, file_data, ("filament_type", "material")),
        "subtype": _preset_value(
            preset, file_data, ("filament_sub_type", "filament_subtype", "subtype")),
        "color": _normalize_color(_preset_value(
            preset, file_data, ("filament_colour", "filament_color", "color"))),
        "filament_id": _preset_value(
            preset, file_data, ("filament_id", "filament_ids")),
        "inherits": _first(file_data.get("inherits")) or _first(
            preset.config_value("inherits") if _has_config_key(preset, "inherits") else ""),
        "from": _first(file_data.get("from")),
        "config": config,
    }


def _has_config_key(preset, key):
    try:
        return key in (preset.config_keys() or [])
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def _make_observation(values):
    cls = _DEPS["Observation"]
    try:
        return cls(**values)
    except TypeError:
        try:
            allowed = {field.name for field in dataclasses.fields(cls)}
        except TypeError:
            raise
        return cls(**{key: value for key, value in values.items() if key in allowed})


def _observation_from_form(slot, values):
    values = values if isinstance(values, dict) else {}
    color = _text(values.get("color"))
    hint = _text(values.get("hint"))
    observation = _make_observation({
        "slot": int(slot),
        "present": bool(values.get("present", True)),
        "vendor": _text(values.get("vendor")),
        "material": _text(values.get("material")),
        "subtype": _text(values.get("subtype")),
        "product": _text(values.get("product")),
        "color": _normalize_color(color) or color,
        "filament_id": _text(values.get("filament_id")),
        "spool_id": _int_or_none(values.get("spool_id")),
        "tag_uid": _text(values.get("tag_uid")),
        "bed_temp": _int_or_none(values.get("bed_temp")),
        "nozzle_temp": _int_or_none(values.get("nozzle_temp")),
        "source": _text(values.get("source")),
        "raw": {"orca_preset": hint} if hint else {},
    })
    return observation


def _serialize_observation(observation):
    result = {field: getattr(observation, field, None) for field in OBSERVATION_FIELDS}
    raw = getattr(observation, "raw", {}) or {}
    hint = _text(raw.get("orca_preset") if isinstance(raw, dict) else "")
    if hint:
        result["hint"] = hint
    return result


def _identity(observation):
    if _IDENTITY_KEY is not None:
        try:
            return _text(_IDENTITY_KEY(observation))
        except Exception:
            pass
    spool_id = getattr(observation, "spool_id", None)
    if spool_id:
        return "spool:%s" % spool_id
    tag_uid = _text(getattr(observation, "tag_uid", ""))
    if tag_uid:
        return "tag:%s" % tag_uid
    parts = [
        _text(getattr(observation, "vendor", "")).casefold(),
        _text(getattr(observation, "material", "")).casefold(),
        _text(getattr(observation, "subtype", "")).casefold(),
        _text(getattr(observation, "product", "")).casefold(),
        _text(getattr(observation, "color", "")).casefold(),
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return "filament:%s" % digest


def _decode_extra(value):
    """Spoolman stores extra values JSON-encoded: '"name"' -> 'name'."""
    if not isinstance(value, str):
        return ""
    try:
        decoded = json.loads(value)
    except ValueError:
        return value
    return decoded if isinstance(decoded, str) else ""


# ---------------------------------------------------------------------------
# Printer, Spoolman and OFD (worker thread only)
# ---------------------------------------------------------------------------

def _normalize_host(host):
    host = _text(host)
    if not host:
        return ""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", host):
        host = "http://" + host
    return host.rstrip("/")


def _url_has_port(url):
    try:
        return urllib.parse.urlsplit(url).port is not None
    except ValueError:
        return True


def _printer_base_urls(info):
    """Candidate base URLs for the active printer, best first."""
    base = _normalize_host((info or {}).get("host"))
    if not base:
        return []
    parts = urllib.parse.urlsplit(base)
    if parts.path not in ("", "/") or parts.query:
        return [base]
    configured_port = _text((info or {}).get("port"))
    if configured_port and not _url_has_port(base):
        return [base + ":" + configured_port]
    if _url_has_port(base):
        return [base]
    # A bare print host has no port. Snapmaker's print host serves the API on
    # its web port; generic Moonraker installs use 7125.
    agent = _text((info or {}).get("agent")).casefold()
    if "snapmaker" in agent:
        return [base, base + ":7125"]
    return [base + ":7125", base]


def _parse_printer_result(result):
    """(observations, non-permission errors, permission hint)."""
    observations = result
    errors, blocked = [], ""
    if isinstance(result, tuple):
        observations = result[0]
        raw_errors = result[1] if len(result) > 1 else []
        if not isinstance(raw_errors, (list, tuple)):
            raw_errors = [raw_errors] if raw_errors else []
        for item in raw_errors:
            hint = _permission_hint(item)
            if hint:
                blocked = hint
            else:
                errors.append(_error_text(item))
    if observations is None:
        observations = []
    if not isinstance(observations, (list, tuple)):
        raise RuntimeError("The printer source returned no filament list")
    return list(observations), errors, blocked


def _read_printer(snapshot, warnings):
    info = snapshot.get("printer_info") or {}
    urls = _printer_base_urls(info)
    if not urls:
        raise RuntimeError(
            "No printer is selected in OrcaSlicer. Connect a printer in the Device "
            "tab (or select a printer preset with a print host), then press the "
            "rescan (↻) button.")
    reader = _DEPS["read_printer"]
    api_key = _text(info.get("api_key"))
    attempts = []
    for url in urls:
        _preflight_host(url)
        try:
            try:
                result = reader(url, api_key, timeout=4.0)
            except TypeError:
                result = reader(base_url=url, api_key=api_key)
        except Exception as error:
            hint = _permission_hint(error)
            if hint:
                raise RuntimeError(hint)
            attempts.append(_error_text(error))
            continue
        observations, errors, blocked = _parse_printer_result(result)
        if blocked:
            raise RuntimeError(blocked)
        for error in errors:
            _warn(warnings, "Printer: %s" % error)
        if observations:
            return sorted(
                observations, key=lambda item: int(getattr(item, "slot", 0) or 0))
        attempts.extend(errors)
    detail = "; ".join(text for text in attempts if text)
    raise RuntimeError(
        "No filaments were reported by the printer at %s.%s"
        % (urls[0], (" " + detail) if detail else ""))


def _spoolman_client(settings, session, warnings):
    url = _text(settings.get("spoolman_url"))
    if not url:
        return None
    if not re.match(r"^https?://[^/]+", url):
        _warn(warnings, "Spoolman URL must start with http:// or https://")
        return None
    cached = session.get("spoolman_client")
    if cached is not None and session.get("spoolman_url") == url:
        return cached
    cls = _DEPS["SpoolmanClient"]
    try:
        client = cls(url)
    except TypeError:
        client = cls(base_url=url)
    session["spoolman_client"] = client
    session["spoolman_url"] = url
    session["spoolman_cache"] = {}
    return client


def _spoolman_filament(client, spool_id, session):
    cache = session.setdefault("spoolman_cache", {})
    key = str(spool_id)
    if key in cache:
        return cache[key]
    spool = client.get_spool(int(spool_id)) or {}
    filament = spool.get("filament") or {}
    if isinstance(filament, int):
        filament = client.get_filament(filament) or {}
    elif isinstance(filament, dict) and not filament.get("name") and filament.get("id") is not None:
        filament = client.get_filament(filament["id"]) or filament
    if not isinstance(filament, dict):
        filament = {}
    cache[key] = (spool, filament)
    return spool, filament


def _enrich_spoolman(observations, settings, session, warnings):
    session["spool_prices"] = {}
    url = _text(settings.get("spoolman_url"))
    status = {
        "available": False,
        "message": "Add a Spoolman URL in Settings to fetch prices.",
    }
    if not url:
        session["spoolman_status"] = status
        return
    if not re.match(r"^https?://[^/]+", url):
        status["message"] = (
            "Fix the Spoolman URL in Settings; it must start with http:// or "
            "https://."
        )
        session["spoolman_status"] = status
        _warn(warnings, "Spoolman URL must start with http:// or https://")
        return
    try:
        client = _spoolman_client(settings, session, warnings)
    except Exception as error:
        hint = _permission_hint(error)
        status["message"] = hint or (
            "Spoolman is not reachable. Fix the URL in Settings and rescan.")
        session["spoolman_status"] = status
        _warn(warnings, ("Spoolman: %s" % hint) if hint
              else ("Spoolman unavailable: %s" % _error_text(error)))
        return
    if client is None:
        status["message"] = (
            "Fix the Spoolman URL in Settings and rescan."
        )
        session["spoolman_status"] = status
        return
    try:
        _preflight_host(getattr(client, "base_url", "") or url)
    except RuntimeError as error:
        status["message"] = (
            "Spoolman is not reachable. Fix the URL in Settings and rescan."
        )
        session["spoolman_status"] = status
        _warn(warnings, "Spoolman: %s" % error)
        return
    session["spoolman_status"] = {"available": True, "message": ""}
    fetched_any = False
    failed_any = False
    for observation in observations:
        spool_id = getattr(observation, "spool_id", None)
        if not spool_id:
            continue
        try:
            spool, filament = _spoolman_filament(client, spool_id, session)
            fetched_any = True
        except Exception as error:
            failed_any = True
            hint = _permission_hint(error)
            _warn(warnings, ("Spoolman: %s" % hint) if hint
                  else ("Spoolman spool %s: %s" % (spool_id, _error_text(error))))
            continue

        vendor = _text((filament.get("vendor") or {}).get("name")
                       if isinstance(filament.get("vendor"), dict) else "")
        material = _text(filament.get("material"))
        product = _text(filament.get("name"))
        color = _normalize_color(filament.get("color_hex"))
        nozzle_temp = _int_or_none(filament.get("settings_extruder_temp"))
        bed_temp = _int_or_none(filament.get("settings_bed_temp"))

        if product and not getattr(observation, "product", ""):
            observation.product = product
        if vendor and not getattr(observation, "vendor", ""):
            observation.vendor = vendor
        if material and not getattr(observation, "material", ""):
            observation.material = material
        if color and not getattr(observation, "color", ""):
            observation.color = color
        if bed_temp and not getattr(observation, "bed_temp", None):
            observation.bed_temp = bed_temp
        if nozzle_temp and not getattr(observation, "nozzle_temp", None):
            observation.nozzle_temp = nozzle_temp

        extra = {}
        if isinstance(filament.get("extra"), dict):
            extra.update(filament["extra"])
        if isinstance(spool.get("extra"), dict):
            extra.update(spool["extra"])
        variant = _decode_extra(extra.get("variant"))
        if variant and not getattr(observation, "subtype", ""):
            observation.subtype = variant
        hint = _decode_extra(extra.get("orca_filament_preset")) or \
            _decode_extra(extra.get("orca_preset"))
        price = _spoolman_cost_per_kg(spool, filament)
        if price:
            session.setdefault("spool_prices", {})[str(spool_id)] = price
        if hint:
            raw = getattr(observation, "raw", None)
            if not isinstance(raw, dict):
                raw = {}
            raw["orca_preset"] = hint
            observation.raw = raw

    if failed_any and not fetched_any:
        session["spoolman_status"] = {
            "available": False,
            "message": ("Spoolman is reachable but did not return spool data. "
                        "Check the Spoolman URL/API and rescan."),
        }


def _fetch_spoolman_price(settings, session, warnings, observation_values):
    """Fetch and cache the per-kg price for one observation's spool."""
    spool_id = _int_or_none((observation_values or {}).get("spool_id"))
    if not spool_id:
        raise ValueError(
            "This feed has no Spoolman spool ID, so there is no Spoolman "
            "price to fetch.")
    url = _text(settings.get("spoolman_url"))
    if not url:
        raise ValueError("Add a Spoolman URL in Settings to fetch prices.")
    try:
        client = _spoolman_client(settings, session, warnings)
    except Exception as error:
        hint = _permission_hint(error)
        raise RuntimeError(hint or (
            "Spoolman is not reachable: %s" % _error_text(error)))
    if client is None:
        raise ValueError("Fix the Spoolman URL in Settings and rescan.")
    _preflight_host(getattr(client, "base_url", "") or url)
    # Force a fresh spool record so a price changed in Spoolman after the scan
    # is visible to this explicit fetch.
    session.setdefault("spoolman_cache", {}).pop(str(spool_id), None)
    spool, filament = _spoolman_filament(client, spool_id, session)
    price = _spoolman_cost_per_kg(spool, filament)
    if not price:
        raise ValueError("Spoolman has no price for spool %s." % spool_id)
    session.setdefault("spool_prices", {})[str(spool_id)] = price
    session["spoolman_status"] = {"available": True, "message": ""}
    return price, spool_id


def _clear_ofd_cache(storage, session=None):
    """Delete the local OFD index and preset JSON cache.

    History, settings and installed presets are untouched. The next scan builds
    a new OfdLibrary, so fresh data is downloaded from OFD.
    """
    index_path = os.path.join(_text(storage), "ofd_index.json")
    preset_dir = os.path.join(_text(storage), "ofd_presets")
    removed_index = False
    removed_presets = 0
    try:
        if os.path.isfile(index_path):
            os.remove(index_path)
            removed_index = True
        if os.path.isdir(preset_dir):
            for _root, _dirs, files in os.walk(preset_dir):
                removed_presets += len(files)
            shutil.rmtree(preset_dir)
    except OSError as error:
        raise RuntimeError(
            "Could not clear the OFD cache: %s" % _error_text(error))
    if isinstance(session, dict):
        session["ofd_library"] = None
        session["ofd_key"] = None
    return {
        "index_removed": removed_index,
        "preset_files_removed": removed_presets,
        "preset_dir": preset_dir,
    }


def _ofd_library(settings, storage, session, warnings):
    if not settings.get("ofd_enabled"):
        return None
    index_url = _text(settings.get("ofd_index_url")) or DEFAULT_OFD_INDEX
    key = (index_url, storage)
    if session.get("ofd_key") == key:
        return session.get("ofd_library")
    cache_path = os.path.join(storage, "ofd_index.json")
    preset_cache_dir = os.path.join(storage, "ofd_presets")
    cls = _DEPS["OfdLibrary"]
    try:
        library = cls(index_url, cache_path,
                      preset_cache_dir=preset_cache_dir,
                      cache_ttl=7 * 24 * 60 * 60)
    except TypeError:
        try:
            library = cls(index_url=index_url, cache_path=cache_path,
                          preset_cache_dir=preset_cache_dir)
        except TypeError:
            library = cls(index_url)
    session["ofd_key"] = key
    session["ofd_library"] = library
    return library


def _entry_get(entry, key, default=None):
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _library_url(index_url, entry):
    path = _text(_entry_get(entry, "url") or _entry_get(entry, "path"))
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path:
        return ""
    base = index_url.rsplit("/", 1)[0] + "/"
    return urllib.parse.urljoin(base, path)


def _library_candidates(observation, settings, storage, session, warnings):
    if not settings.get("ofd_enabled"):
        return []
    if not (_text(getattr(observation, "vendor", "")) or
            _text(getattr(observation, "material", "")) or
            _text(getattr(observation, "product", ""))):
        return []
    try:
        library = _ofd_library(settings, storage, session, warnings)
        if library is None:
            return []
        entries = library.find(
            _text(getattr(observation, "vendor", "")),
            _text(getattr(observation, "material", "")),
            _text(getattr(observation, "product", "")),
            _text(getattr(observation, "subtype", "")),
        )
    except Exception as error:
        hint = _permission_hint(error)
        _warn(warnings, ("Open Filament Database: %s" % hint) if hint
              else ("Open Filament Database: %s" % _error_text(error)))
        return []
    if isinstance(entries, dict):
        entries = entries.get("results") or entries.get("items") or entries.get("candidates") or []
    if entries is None:
        return []
    if isinstance(entries, (str, bytes)) or not isinstance(entries, (list, tuple)):
        entries = [entries]

    index_url = _text(settings.get("ofd_index_url")) or DEFAULT_OFD_INDEX
    candidate_cls = _DEPS["Candidate"]
    result, seen = [], set()
    for entry in entries:
        name = _text(_entry_get(entry, "profile_name") or _entry_get(entry, "preset_name")
                     or _entry_get(entry, "name"))
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        meta = _jsonable(entry)
        if not isinstance(meta, dict):
            meta = {}
        meta = dict(meta)
        meta["inherits"] = _text(_entry_get(entry, "inherits") or _entry_get(entry, "parent"))
        meta["brand"] = _text(_entry_get(entry, "brand") or _entry_get(entry, "vendor"))
        meta["filament"] = _text(_entry_get(entry, "filament") or _entry_get(entry, "product"))
        meta["path"] = _text(_entry_get(entry, "path"))
        url = _library_url(index_url, entry)
        if url:
            meta["url"] = url
        raw_score = _entry_get(entry, "score", 60)
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            score = 60
        reasons = ["Open Filament Database"]
        if meta["brand"]:
            reasons.append(meta["brand"])
        if meta["filament"]:
            reasons.append(meta["filament"])
        result.append(candidate_cls(
            preset_name=name,
            score=max(1, min(100, score)),
            reasons=reasons,
            source="library",
            preset=None,
            library_url=url,
            library_meta=meta,
        ))
        if len(result) >= 10:
            break
    return result


# ---------------------------------------------------------------------------
# Matching and row construction (worker thread only)
# ---------------------------------------------------------------------------

def _history_store(storage, warnings):
    cls = _DEPS["HistoryStore"]
    try:
        return cls(_history_path(storage))
    except Exception as error:
        _warn(warnings, "History could not be opened: %s" % _error_text(error))
        return _EmptyHistory()


class _EmptyHistory:
    """Fallback when HistoryStore cannot be opened; matching still works."""

    def get(self, observation, printer=""):
        return None

    def remember(self, observation, printer, preset_name):
        return False

    def forget(self, observation):
        return False

    def clear(self):
        return False


def _generic_name(observation, presets):
    material = _text(getattr(observation, "material", ""))
    if not material:
        return ""
    generic = [preset for preset in presets
               if _base_name(preset.name, preset.bundle_id).casefold().startswith("generic ")]
    if not generic:
        return ""
    exact = [preset for preset in generic
             if _base_name(preset.name, preset.bundle_id).casefold()
             == ("generic " + material).casefold()]
    pool = exact or generic
    return min(pool, key=lambda preset: (getattr(preset, "rank", 9), preset.name.casefold())).name


def _call_match(observation, presets, history, printer, lookup):
    matcher = _DEPS["match_observation"]
    try:
        return matcher(observation, presets, history, printer, library_lookup=lookup)
    except TypeError as error:
        if "library_lookup" not in str(error):
            raise
        return matcher(observation, presets, history, printer)


def _candidate_to_dict(candidate):
    preset = getattr(candidate, "preset", None)
    preset_name = _text(getattr(candidate, "preset_name", ""))
    if preset is not None:
        provenance = _provenance(
            getattr(preset, "system", False),
            getattr(preset, "user", False),
            getattr(preset, "bundle_id", ""),
        )
        vendor = getattr(preset, "vendor", "")
        material = getattr(preset, "material", "")
        subtype = getattr(preset, "subtype", "")
        color = getattr(preset, "color", "")
        base = getattr(preset, "base", "")
        filament_id = getattr(preset, "filament_id", "")
        bundle_id = getattr(preset, "bundle_id", "")
        select_name = (_text(getattr(preset, "select_name", "")) or
                       _text(getattr(preset, "name", "")) or preset_name)
    else:
        provenance = "library" if getattr(candidate, "source", "") == "library" else \
            getattr(candidate, "source", "")
        vendor = material = subtype = color = base = filament_id = bundle_id = ""
        select_name = preset_name
    meta = getattr(candidate, "library_meta", {}) or {}
    if not isinstance(meta, dict):
        meta = _jsonable(meta)
    inherits = _text(meta.get("inherits")) if isinstance(meta, dict) else ""
    if preset is not None:
        cost = _price_value((getattr(preset, "config", {}) or {}).get(
            "filament_cost", ""))
    else:
        cost = _price_value(meta.get("filament_cost", "")) if isinstance(
            meta, dict) else ""
    return {
        "preset_name": preset_name,
        "select_name": select_name,
        "score": int(getattr(candidate, "score", 0) or 0),
        "reasons": [_text(reason) for reason in (getattr(candidate, "reasons", []) or [])
                    if _text(reason)],
        "source": _text(getattr(candidate, "source", "")),
        "provenance": provenance,
        "vendor": vendor,
        "material": material,
        "subtype": subtype,
        "color": color,
        "base": base,
        "filament_id": filament_id,
        "bundle_id": bundle_id,
        "cost": cost,
        "library_url": _text(getattr(candidate, "library_url", "")),
        "library": _jsonable(meta) if meta else {},
        "inherits": inherits,
    }


def _result_value(result, name, default):
    return getattr(result, name, default)


def _build_row(observation, snapshot, history, settings, storage, session, warnings,
               use_history=True):
    if not use_history:
        history = {}
    compatible = snapshot.get("compatible") or []
    printer = snapshot.get("printer") or ""
    library = []
    lookup = None
    if settings.get("ofd_enabled") and compatible:
        library = _library_candidates(observation, settings, storage, session, warnings)
        lookup = lambda _observation, found=library: list(found)

    if not compatible:
        result = _DEPS["MatchResult"](
            observation=observation,
            error="No compatible, visible filament presets are installed for the current printer.",
        )
    else:
        try:
            result = _call_match(observation, compatible, history, printer, lookup)
        except Exception as error:
            result = _DEPS["MatchResult"](
                observation=observation,
                error="Matching failed: %s" % _error_text(error),
            )

    candidates = list(_result_value(result, "candidates", []) or [])
    preset_name = _text(_result_value(result, "preset_name", ""))
    source = _text(_result_value(result, "source", ""))
    confidence = int(_result_value(result, "confidence", 0) or 0)
    error = _text(_result_value(result, "error", ""))

    # A Spoolman orca_preset extra field is an explicit user hint. It wins over
    # the heuristic pipeline, as long as the named preset still exists.
    raw = getattr(observation, "raw", {}) or {}
    hint = _text(raw.get("orca_preset") if isinstance(raw, dict) else "")
    if hint:
        preset = _find_preset(snapshot.get("presets") or [], hint)
        if preset is not None and preset.compatible:
            hint_candidate = _DEPS["Candidate"](
                preset_name=preset.name,
                score=100,
                reasons=["Spoolman orca_preset"],
                source="history",
                preset=preset,
            )
            candidates = [item for item in candidates
                          if getattr(item, "preset_name", "") != preset.name]
            candidates.insert(0, hint_candidate)
            preset_name, source, confidence, error = preset.name, "history", 100, ""
        elif preset is not None:
            _warn(warnings, 'Spoolman preset "%s" is not compatible with this printer.' % hint)

    # The core pipeline may already have returned library candidates. If it
    # stopped at a generic fallback, offer the library matches as alternatives.
    if library and (not preset_name or source == "generic"):
        known = {getattr(item, "preset_name", "") for item in candidates}
        for item in library:
            if item.preset_name not in known:
                candidates.append(item)

    if not error and not preset_name and not candidates:
        error = "No matching preset."

    chosen = _find_preset(snapshot.get("presets") or [], preset_name) if preset_name else None
    chosen_candidate = next(
        (item for item in candidates if getattr(item, "preset_name", "") == preset_name), None)
    reasons = []
    if chosen_candidate is not None:
        reasons = [_text(reason) for reason in
                   (getattr(chosen_candidate, "reasons", []) or []) if _text(reason)]
    else:
        reasons = [_text(reason) for reason in (_result_value(result, "reasons", []) or [])
                   if _text(reason)]

    current_price = ""
    spool_price = ""
    serialized = [_candidate_to_dict(item) for item in candidates
                  if _text(getattr(item, "preset_name", ""))]
    select_names = snapshot.get("select_names") or {}
    for item in serialized:
        if item["source"] != "library":
            item["select_name"] = (_text(select_names.get(item["preset_name"])) or
                                   item["select_name"] or item["preset_name"])
        elif not item.get("cost") and item.get("inherits"):
            parent = _find_preset(
                snapshot.get("presets") or [], item["inherits"])
            if parent is not None:
                item["cost"] = _price_value(
                    (getattr(parent, "config", {}) or {}).get(
                        "filament_cost", ""))
    if not current_price and not preset_name and len(serialized) == 1:
        current_price = _text(serialized[0].get("cost"))
    chosen_select = _text(select_names.get(preset_name)) if preset_name else ""
    if not chosen_select and chosen_candidate is not None:
        chosen_select = _text(getattr(chosen_candidate, "select_name", ""))
    if not chosen_select:
        chosen_select = preset_name
    scores = {}
    if _SCORE_PRESET is not None:
        for preset in compatible:
            try:
                value, _reasons = _SCORE_PRESET(observation, preset)
                if value:
                    scores[preset.name] = int(value)
            except Exception:
                continue
    scores.update({item["preset_name"]: item["score"]
                   for item in serialized if item["score"]})
    if preset_name and confidence and preset_name not in scores:
        scores[preset_name] = confidence
    identity = _identity(observation)
    ignored = identity in (settings.get("ignored") or [])
    slots = snapshot.get("slots") or []
    slot = int(getattr(observation, "slot", 0) or 0)
    slot_exists = 1 <= slot <= len(slots)
    current_internal = _text(slots[slot - 1]) if slot_exists else ""
    current_label = _text(select_names.get(current_internal)) or current_internal
    if not current_price:
        if chosen is not None:
            current_price = _price_value(
                (getattr(chosen, "config", {}) or {}).get("filament_cost", ""))
        elif chosen_candidate is not None:
            meta = getattr(chosen_candidate, "library_meta", {}) or {}
            if isinstance(meta, dict):
                current_price = _price_value(meta.get("filament_cost", ""))
    spool_price = ""
    spool_id = getattr(observation, "spool_id", None)
    if spool_id and isinstance(session, dict):
        spool_price = _price_value(
            (session.get("spool_prices") or {}).get(str(spool_id), ""))

    return {
        "slot": slot,
        "identity": identity,
        "observation": _serialize_observation(observation),
        "preset_name": preset_name,
        "select_name": chosen_select,
        "source": source,
        "confidence": confidence,
        "reasons": reasons,
        "error": error,
        "candidates": serialized,
        "scores": scores,
        "choose": bool(not preset_name and len(serialized) > 1),
        "generic": _generic_name(observation, compatible),
        "current": current_label,
        "current_name": current_internal,
        "slot_exists": slot_exists,
        "slot_count": len(slots),
        "ignored": ignored,
        "current_price": current_price,
        "spool_price": spool_price,
        "applied": False,
        "user_applied": False,
        "apply_error": "",
        "preset": _candidate_to_dict(chosen_candidate) if chosen_candidate is not None else None,
    }


def _slot_mismatch(feeders, slots):
    """Describe a printer/project filament-slot count mismatch.

    Orca 2.5.2 exposes no plugin API to add, remove or resize project filament
    slots (see API_NOTES.md and OrcaSlicer issue #17357), so the panel reports
    the exact difference and applies only the feeds that have a slot.
    """
    try:
        feeders = max(0, int(feeders))
        slots = max(0, int(slots))
    except (TypeError, ValueError):
        feeders = slots = 0
    missing = max(0, feeders - slots)
    extra = max(0, slots - feeders)
    if missing:
        message = (
            "Printer reports %d filament feed(s), but this project has %d "
            "filament slot(s). Add %d slot(s) in Orca's Filament tab, then "
            "press the rescan (↻) button." % (feeders, slots, missing))
    elif extra:
        message = (
            "Printer reports %d filament feed(s), but this project has %d "
            "filament slot(s). %d extra slot(s) are left unchanged; remove "
            "them in Orca's Filament tab for an exact match."
            % (feeders, slots, extra))
    else:
        message = ""
    return {
        "feeders": feeders,
        "slots": slots,
        "missing_slots": missing,
        "extra_slots": extra,
        "message": message,
    }


def _scan_rows(settings, snapshot, storage, session, warnings):
    observations = _read_printer(snapshot, warnings)
    _enrich_spoolman(observations, settings, session, warnings)
    if not observations:
        raise RuntimeError(
            "The printer reported no filaments. Check that it is connected in "
            "OrcaSlicer and that a multi-material unit is attached.")
    history = _history_store(storage, warnings)
    rows = []
    for observation in observations:
        row = _build_row(observation, snapshot, history, settings, storage, session, warnings)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Preset selection and library install (worker thread only)
# ---------------------------------------------------------------------------

def _safe_filename(name):
    text = re.sub(r"[\x00-\x1f]", " ", _text(name))
    for char in BAD_FILENAME_CHARS:
        text = text.replace(char, "_")
    text = text.strip().strip(".")[:120].strip()
    if not text:
        raise ValueError("The preset name cannot be used as a file name")
    if text.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                        "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
                        "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}:
        text += "_preset"
    return text


def _fetch_library_preset(library, candidate, settings):
    meta = candidate.get("library") or {}
    if not isinstance(meta, dict):
        meta = {}
    cached = False
    checker = getattr(library, "preset_is_cached", None)
    if callable(checker):
        try:
            cached = bool(checker(meta))
        except Exception:
            cached = False
    if not cached:
        _preflight_host(_text(settings.get("ofd_index_url")) or DEFAULT_OFD_INDEX)
    if hasattr(library, "fetch"):
        data = library.fetch(meta)
    else:
        raise RuntimeError("The OFD source module has no fetch() method")
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise RuntimeError("The library returned no preset JSON")
    return data


def _is_our_bundle_preset(preset):
    if _text(getattr(preset, "bundle_id", "")) == BUNDLE_ID:
        return True
    path = _text(getattr(preset, "file", "")).replace("\\", "/")
    return ("/user/" in path and "/_local/%s/" % BUNDLE_ID in path)


def _normalized_label(value):
    return " ".join(_text(value).casefold().split())


def _compatible_library_parent(inherits, snapshot):
    """Resolve an OFD parent to a preset compatible with this printer.

    OFD presets inherit catalogue generics such as ``Generic PETG @System``.
    Printer bundles often shadow those with nozzle-specific generics
    (``Generic PETG @U1 0.4 nozzle``) and mark the system generic incompatible.
    Use the compatible same-base/same-material variant instead of refusing the
    install.
    """
    declared = _find_preset(snapshot.get("presets") or [], inherits)
    if declared is not None and declared.compatible:
        return declared

    base = ((getattr(declared, "base", "") if declared is not None else "")
            or _base_name(inherits))
    base_key = _normalized_label(base)
    if not base_key.startswith("generic "):
        return declared

    material_key = _normalized_label(
        getattr(declared, "material", "") if declared is not None else "")
    if not material_key and " " in base_key:
        material_key = _normalized_label(base_key.split(" ", 1)[1])
    printer_nozzle = _normalized_label(snapshot.get("printer_nozzle"))

    ranked = []
    for preset in snapshot.get("compatible") or []:
        if preset is declared:
            continue
        preset_base = (getattr(preset, "base", "") or
                       _base_name(preset.name, preset.bundle_id))
        if _normalized_label(preset_base) != base_key:
            continue
        preset_material = _normalized_label(getattr(preset, "material", ""))
        if material_key and preset_material and preset_material != material_key:
            continue
        preset_nozzle = _normalized_label(_preset_nozzle(preset.name))
        if printer_nozzle and preset_nozzle and preset_nozzle != printer_nozzle:
            continue
        exact_nozzle = bool(printer_nozzle and preset_nozzle == printer_nozzle)
        ranked.append((
            0 if exact_nozzle else 1 if not preset_nozzle else 2,
            0 if getattr(preset, "system", False) else 1,
            getattr(preset, "rank", 9),
            _normalized_label(preset.name),
            preset,
        ))
    if not ranked:
        return declared
    ranked.sort(key=lambda row: row[:4])
    return ranked[0][4]


def _validated_library_preset(data, snapshot, warnings):
    """Sanitize and validate one fetched preset before it is written."""
    preset = dict(data)

    # The sources module owns the catalog-specific validation. Call it first so
    # a preset claiming to be a system profile, or carrying system identity
    # keys, is refused rather than silently rewritten.
    validator = getattr(_DEPS.get("OfdLibrary"), "validate_preset", None)
    if callable(validator):
        problems = list(validator(preset) or [])
        if problems:
            raise ValueError(
                "The library preset was refused: %s" % "; ".join(problems))

    name = _text(preset.get("name"))
    source = _text(preset.get("from"))
    if not name:
        raise ValueError("The library preset has no name")
    if not source:
        raise ValueError('The library preset "%s" has no "from" field' % name)
    if source.casefold() == "system":
        raise ValueError("System presets cannot be installed as user presets")
    inherits = _text(preset.get("inherits"))
    if not inherits:
        raise ValueError('The library preset "%s" has no parent (inherits)' % name)

    parent = _compatible_library_parent(inherits, snapshot)
    if parent is None:
        raise ValueError(
            'The library preset "%s" inherits "%s", which is not installed. '
            "Install the parent preset first." % (name, inherits))
    if not parent.compatible:
        raise ValueError(
            'The library preset "%s" inherits "%s", which is not compatible with '
            "the current printer." % (name, parent.name))
    if parent.name != inherits:
        preset["inherits"] = parent.name
        _warn(warnings,
              'Using compatible parent "%s" for "%s" (OFD declares "%s").'
              % (parent.name, name, inherits))

    removed = sorted(key for key in SYSTEM_ONLY_KEYS if key in preset)
    for key in SYSTEM_ONLY_KEYS:
        preset.pop(key, None)
    if removed:
        _warn(warnings, 'Removed system-only key(s) %s from "%s".' % (", ".join(removed), name))

    existing_matches = [
        item for item in (snapshot.get("presets") or [])
        if item.name.casefold() == name.casefold()
        or _text(getattr(item, "select_name", "")).casefold() == name.casefold()
    ]
    blockers = [item for item in existing_matches if not _is_our_bundle_preset(item)]
    if blockers:
        existing = blockers[0]
        raise ValueError(
            'A preset named "%s" already exists (%s). It will not be overwritten.'
            % (name, _provenance(existing.system, existing.user, existing.bundle_id)))
    if any(item.user for item in existing_matches):
        raise ValueError(
            'A user preset named "%s" exists. It will not be overwritten.' % name)

    preset["name"] = name
    preset["from"] = source
    preset["type"] = "filament"
    preset["instantiation"] = "true"
    preset.setdefault("version", PLUGIN_VERSION)
    preset["filament_settings_id"] = [name]
    return preset


def _write_local_preset(preset, snapshot):
    """Write one preset into the plugin's local bundle and reload it."""
    name = _text(preset.get("name"))
    data_dir = _text(snapshot.get("data_dir"))
    folder = _text(snapshot.get("folder"))
    if not snapshot.get("folder_inferred") or not folder:
        raise RuntimeError(
            "Could not determine the active Orca user profile folder from a loaded user or "
            "local-bundle preset. Save one user preset in Orca (or select one), press "
            "Refresh presets, then try again.")
    if not data_dir:
        raise RuntimeError(
            "Could not find Orca's data folder from the loaded presets. Open a user "
            "preset in Orca first, or restart Orca with a user profile loaded.")
    user_root = os.path.join(data_dir, "user")
    if not os.path.isdir(user_root):
        raise RuntimeError("Orca's user preset folder was not found at %s" % user_root)

    bundle_dir = os.path.join(user_root, folder, "_local", BUNDLE_ID)
    filament_dir = os.path.join(bundle_dir, "filament")
    target = os.path.join(filament_dir, _safe_filename(name) + ".json")
    _write_json_atomic(target, preset)
    _write_json_atomic(os.path.join(bundle_dir, "bundle_metadata.json"), {
        "id": BUNDLE_ID,
        "name": BUNDLE_NAME,
        "version": PLUGIN_VERSION,
    })

    try:
        orca.host.preset.bundles.reload_local(BUNDLE_ID)
    except Exception as error:
        raise RuntimeError(
            'The preset was written to %s but Orca could not reload the bundle: %s'
            % (bundle_dir, _error_text(error)))
    return name


def _install_library_preset(candidate, snapshot, settings, storage, session,
                            warnings, price=""):
    library = _ofd_library(settings, storage, session, warnings)
    if library is None:
        raise RuntimeError("Open Filament Database lookup is not enabled")
    data = _fetch_library_preset(library, candidate, settings)
    preset = _validated_library_preset(data, snapshot, warnings)
    if price:
        preset["filament_cost"] = [price]
    return _write_local_preset(preset, snapshot)


def _strip_local_suffix(value):
    return re.sub(r"\s*\(local\)\s*$", "", _text(value), flags=re.I).strip()


def _price_copy_name(preset, snapshot):
    """A unique local price-copy name such as ``eSUN PETG (local)``."""
    base = _strip_local_suffix(
        getattr(preset, "base", "") or
        _base_name(getattr(preset, "name", ""), preset.bundle_id)) or "Filament"
    presets = snapshot.get("presets") or []

    def taken(name):
        folded = name.casefold()
        matches = [
            item for item in presets
            if item.name.casefold() == folded or
            _text(getattr(item, "select_name", "")).casefold() == folded
        ]
        return any(not _is_our_bundle_preset(item) for item in matches)

    candidate = "%s (local)" % base
    if not taken(candidate):
        return candidate
    for number in range(2, 100):
        candidate = "%s %d (local)" % (base, number)
        if not taken(candidate):
            return candidate
    raise RuntimeError('Could not create a unique local price preset for "%s"' % base)


_PRICE_COPY_METADATA = {
    "name", "from", "type", "instantiation", "inherits",
    "filament_settings_id", "setting_id", "filament_id", "filament_ids",
    "base_id", "is_system", "system", "bundle_id", "version",
}


def _flatten_user_preset(preset, snapshot, depth=0, seen=None):
    """Return a user preset's config and its first non-user parent.

    Orca's local bundle loader cannot resolve a local preset whose ``inherits``
    points into the user preset folder; it logs "can not find parent" and skips
    the file. For a price copy of a user preset, flatten the user ancestry into
    the copy and inherit only the system/other parent.
    """
    if depth > 8:
        return {}, ""
    seen = set(seen or ())
    path = _text(getattr(preset, "file", ""))
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    name_key = _text(getattr(preset, "name", "")).casefold()
    seen.add(name_key)
    parent_name = _text(data.get("inherits"))
    parent = _find_preset(snapshot.get("presets") or [], parent_name) \
        if parent_name else None
    if (parent is not None and parent.user
            and _text(parent.name).casefold() not in seen):
        parent_data, root = _flatten_user_preset(
            parent, snapshot, depth + 1, seen)
        merged = dict(parent_data)
        merged.update(data)
        return merged, root
    return data, parent_name


def _user_price_copy_data(preset, copy_name, price, snapshot):
    flat, root_name = _flatten_user_preset(preset, snapshot)
    root = _find_preset(snapshot.get("presets") or [], root_name) \
        if root_name else None
    if root is None or not root.compatible:
        material = _text(getattr(preset, "material", "")) or _text(
            _first(flat.get("filament_type")))
        root = _compatible_library_parent(
            "Generic %s @System" % material, snapshot) if material else None
    if root is None or not root.compatible:
        raise RuntimeError(
            'Could not find a compatible parent for the user preset "%s".' %
            _text(getattr(preset, "name", "")))
    data = {
        key: value for key, value in flat.items()
        if key not in _PRICE_COPY_METADATA
    }
    data.update({
        "name": copy_name,
        "from": "User",
        "type": "filament",
        "instantiation": "true",
        "inherits": root.name,
        "filament_cost": [price],
    })
    if getattr(preset, "vendor", ""):
        data["filament_vendor"] = [preset.vendor]
    elif flat.get("filament_vendor"):
        data["filament_vendor"] = flat["filament_vendor"]
    if getattr(preset, "material", ""):
        data["filament_type"] = [preset.material]
    elif flat.get("filament_type"):
        data["filament_type"] = flat["filament_type"]
    if getattr(preset, "color", ""):
        data["filament_colour"] = [preset.color]
    return data


def _create_price_copy(preset, price, snapshot, warnings):
    """Create or replace a local ``(local)`` copy of a non-plugin preset."""
    copy_name = _price_copy_name(preset, snapshot)
    if getattr(preset, "user", False):
        data = _user_price_copy_data(preset, copy_name, price, snapshot)
    else:
        data = {
            "name": copy_name,
            "from": "User",
            "type": "filament",
            "instantiation": "true",
            "inherits": preset.name,
            "filament_cost": [price],
        }
        if getattr(preset, "vendor", ""):
            data["filament_vendor"] = [preset.vendor]
        if getattr(preset, "material", ""):
            data["filament_type"] = [preset.material]
        if getattr(preset, "color", ""):
            data["filament_colour"] = [preset.color]
    validated = _validated_library_preset(data, snapshot, warnings)
    validated["filament_cost"] = [price]
    return _write_local_preset(validated, snapshot)


def _update_local_preset_price(preset, price, warnings):
    """Set filament_cost in a plugin-owned local preset JSON and reload it."""
    path = _text(getattr(preset, "file", ""))
    if not path or not os.path.isfile(path):
        raise RuntimeError(
            'The local preset "%s" has no readable JSON file to update.' %
            _text(getattr(preset, "name", "")))
    data = _read_json(path, {})
    if not isinstance(data, dict) or not data:
        raise RuntimeError(
            'The local preset "%s" could not be read.' %
            _text(getattr(preset, "name", "")))
    data["filament_cost"] = [price]
    _write_json_atomic(path, data)
    try:
        orca.host.preset.bundles.reload_local(BUNDLE_ID)
    except Exception as error:
        raise RuntimeError(
            'The price was written to %s but Orca could not reload the bundle: %s'
            % (path, _error_text(error)))
    return True


def _select_slot(slot, name):
    try:
        updater = orca.host.preset.filament.begin_update(int(slot), name)
    except Exception as error:
        raise RuntimeError(
            "Orca could not update slot %s: %s. (The slot API begin_update is "
            "undocumented; a restart may clear the problem.)" % (slot, _error_text(error)))
    if updater is None:
        raise RuntimeError(
            "Orca returned no slot updater for slot %s. The begin_update API may "
            "have changed in this Orca version." % slot)
    try:
        # .commit() returns a PresetUpdateResult, not a bool. Only an exception
        # is a failure here; the result type is not part of the public API.
        updater.commit()
    except Exception as error:
        raise RuntimeError(
            "Orca rejected the preset for slot %s: %s" % (slot, _error_text(error)))


def _apply_selection(message, snapshot, settings, storage, session, warnings):
    slot = int(message.get("slot") or 0)
    if slot < 1:
        raise ValueError("Invalid slot number")
    slots = snapshot.get("slots") or []
    if slot > len(slots):
        raise ValueError(
            "This printer reports slot %s, but the current project has only %d filament "
            "slot(s). Add filament slots in Orca or select a different printer."
            % (slot, len(slots)))
    observation = _observation_from_form(slot, message.get("observation") or {})
    name = _text(message.get("preset_name"))
    source = _text(message.get("source")) or "installed"
    raw_price = message.get("price")
    price = _price_value(raw_price)
    if raw_price not in (None, "") and not price:
        raise ValueError("The price must be a non-negative number")
    price_copy = False
    price_persisted = False
    refresh_presets = False

    if source == "library":
        candidate = message.get("candidate") or {}
        if not isinstance(candidate, dict):
            candidate = {}
        if not candidate:
            raise ValueError("Choose a library candidate before installing")
        label = _install_library_preset(
            candidate, snapshot, settings, storage, session, warnings,
            price=price)
        # After reload_local, a bundle preset's internal Preset.name is
        # _local/<bundle>/<base>; the selector label is the base name. Store
        # the internal form in history so the next session can resolve it.
        name = "_local/%s/%s" % (BUNDLE_ID, label)
        source = "library"
        select_name = label
        price_persisted = bool(price)
        refresh_presets = True
    elif source == "generic" and not name:
        name = _generic_name(observation, snapshot.get("compatible") or [])
        if not name:
            raise ValueError("No Generic preset for this material is compatible with the printer")
    if not name:
        raise ValueError("Choose a preset first")

    if source != "library":
        select_name = name
        preset = _find_preset(snapshot.get("presets") or [], name)
        if preset is None:
            raise ValueError(
                'The preset "%s" is no longer loaded. Press Refresh presets and try again.' % name)
        if not preset.compatible:
            raise ValueError(
                'The preset "%s" is not compatible with the current printer.' % name)
        # Preset.name is the internal identity; the sidebar selector resolves
        # the visible label/alias. They differ for _local bundles.
        name = preset.name
        select_name = getattr(preset, "select_name", "") or name
        if price:
            if _is_our_bundle_preset(preset):
                _update_local_preset_price(preset, price, warnings)
                price_persisted = True
                refresh_presets = True
            else:
                # System and user presets cannot be saved through the plugin
                # API. Create a local copy so the price survives restarts.
                copy_name = _create_price_copy(
                    preset, price, snapshot, warnings)
                name = "_local/%s/%s" % (BUNDLE_ID, copy_name)
                select_name = copy_name
                price_copy = True
                price_persisted = True
                refresh_presets = True

    _select_slot(slot, select_name)

    remembered = False
    remember = bool(message.get("remember"))
    if remember and source != "generic":
        try:
            history = _history_store(storage, warnings)
            history.remember(observation, snapshot.get("printer") or "", name)
            remembered = True
        except Exception as error:
            _warn(warnings, "Selected, but could not remember the mapping: %s"
                   % _error_text(error))

    noun = "selected and remembered" if remembered else "selected"
    origin = "installed from Open Filament Database and " if source == "library" else ""
    message_text = "T%s: %s — %s%s." % (slot, select_name, origin, noun)
    if price:
        message_text += " Price set to %s/kg." % price
    if price_copy:
        message_text += " Created local preset %s." % select_name
    return {
        "applied": {
            "slot": slot,
            "preset_name": name,
            "select_name": select_name,
            "source": source,
            "remembered": remembered,
            "price": price,
            "price_persisted": price_persisted,
            "price_copy": price_copy,
            "price_target": name,
        },
        "message": message_text,
        "warnings": warnings,
        "refresh_presets": refresh_presets,
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><style>
:root { color-scheme: light dark; --line: var(--orca-border, rgba(127,127,127,.35));
        --field-line: var(--orca-border, rgba(127,127,127,.55));
        --field-line: color-mix(in srgb, var(--orca-fg, CanvasText) 45%, transparent);
        --soft: rgba(127,127,127,.12); --a: var(--orca-accent, #009688);
        --muted: var(--orca-muted, rgba(127,127,127,.9)); }
* { box-sizing: border-box; }
body { margin: 0; padding: 12px 14px 20px; font: 13px/1.4 var(--orca-font, system-ui, sans-serif);
       color: var(--orca-fg, CanvasText); background: var(--orca-bg, Canvas); }
[hidden] { display: none !important; }
.row { display: flex; align-items: center; gap: 8px; }
.topbar { display: flex; align-items: center; gap: 8px; min-height: 24px; }
#status { flex: 1 1 auto; min-width: 0; font-size: 12px; white-space: pre-wrap; }
#status.ok { color: #1b7f4b; }
#status.err { color: #c0392b; }
#status.warn { color: #a26200; }
#status.info { color: var(--muted); }
button { font: inherit; border: 1px solid var(--field-line); background: transparent;
         color: inherit; border-radius: 6px; padding: 5px 10px; cursor: pointer; }
button:hover { background: var(--soft); }
button:disabled { opacity: .4; cursor: default; }
button.icon { width: 28px; height: 28px; padding: 0; display: inline-flex;
              align-items: center; justify-content: center; flex: 0 0 auto; }
button.match { margin-top: 8px; border-color: var(--a); color: var(--a); }
button.primary { border-color: var(--a); color: var(--a); font-weight: 600; }
input[type=text], input[type=number], select {
  font: inherit; color: inherit; background: transparent; border: 1px solid var(--field-line);
  border-radius: 6px; padding: 5px 7px; min-width: 0; }
input[type=text], input[type=number] { width: 100%; }
input[type=checkbox] { accent-color: var(--a); }
select { flex: 1 1 auto; min-width: 0; }
.feed { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
        margin: 10px 0; background: var(--soft); }
.feed-row { display: flex; align-items: center; gap: 7px; }
.slot { display: inline-flex; align-items: center; justify-content: center;
        flex: 0 0 auto; min-width: 34px; height: 26px; border-radius: 7px;
        background: var(--a); color: var(--orca-accent-fg, #fff); font-weight: 700; }
.empty { color: var(--muted); font-size: 12px; }
.mismatch { margin: 8px 0 2px; padding: 8px 10px; border: 1px solid #b77900;
            border-radius: 8px; background: rgba(183,121,0,.10); font-size: 12px; }
.mismatch strong { display: block; margin-bottom: 2px; }
.feed.missing-slot { border-color: #b77900; }
.price-box { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--line); }
.price-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 8px; }
.price-grid label { display: flex; flex-direction: column; gap: 3px; font-size: 11px; color: var(--muted); }
.price-grid input { padding: 4px 6px; }
.price-actions { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.price-btn-wrap { display: inline-flex; }
.price-error { color: #c0392b; font-size: 11px; margin-top: 5px; }
.price-note { color: var(--muted); font-size: 11px; margin-top: 5px; }
.feed-status { min-height: 15px; margin: 2px 0 0 41px; font-size: 11px; }
.feed-status.ok { color: #1b7f4b; }
.feed-status.err { color: #c0392b; }
.feed-status.warn { color: #a26200; }
.fields { margin: 8px 0 0 41px; padding-top: 8px; border-top: 1px dashed var(--line); }
.field-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 8px; }
.field-grid label { display: flex; flex-direction: column; gap: 3px; font-size: 11px; color: var(--muted); }
.field-grid input { padding: 4px 6px; }
details.settings { margin-top: 14px; border: 1px solid var(--line); border-radius: 8px;
                   padding: 0 10px; }
details.settings > summary { cursor: pointer; padding: 8px 0; color: var(--muted); font-weight: 600; }
.settings-body { display: grid; gap: 8px; padding: 0 0 10px; }
.settings-body label { display: flex; flex-direction: column; gap: 3px; font-size: 11px; color: var(--muted); }
.settings-body label.inline { flex-direction: row; align-items: center; gap: 6px; }
.settings-body .printer-line { font-size: 11px; color: var(--muted); }
.dim { color: var(--muted); font-size: 11px; }
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.42); display: flex;
           align-items: center; justify-content: center; z-index: 20; padding: 16px; }
.modal { background: var(--orca-bg, Canvas); color: var(--orca-fg, CanvasText);
         border: 1px solid var(--line); border-radius: 10px; width: min(760px, 100%);
         max-height: 86vh; display: flex; flex-direction: column; }
.modal-head { display: flex; align-items: center; gap: 8px; padding: 9px 11px;
              border-bottom: 1px solid var(--line); }
.modal-head h2 { flex: 1 1 auto; min-width: 0; margin: 0; font-size: 13px;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.modal-body { overflow: auto; padding: 7px 11px 11px; }
.modal-body table { width: 100%; border-collapse: collapse; }
.modal-body td { padding: 3px 5px; border-bottom: 1px solid var(--line);
                 vertical-align: top; font-size: 12px; word-break: break-word; }
.modal-body td:first-child { width: 40%; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.modal-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 9px 11px;
              border-top: 1px solid var(--line); }
.cand { display: flex; gap: 8px; align-items: flex-start; padding: 8px 2px;
        border-bottom: 1px solid var(--line); cursor: pointer; }
.cand:last-child { border-bottom: 0; }
.cand input { margin-top: 3px; flex: 0 0 auto; accent-color: var(--a); }
.cand-text { flex: 1 1 auto; min-width: 0; }
.cand-name { font-weight: 600; }
.cand-meta { color: var(--muted); font-size: 11px; margin-top: 2px; }
</style></head><body>

<div class="topbar">
  <span id="status"></span>
  <button class="primary" id="apply" title="Apply the selected presets to Orca" disabled>Apply</button>
  <button class="icon" id="rescan" title="Scan printer again" aria-label="Scan printer again">↻</button>
</div>

<div id="mismatch" class="mismatch" hidden></div>

<div id="rows"></div>

<details class="settings" id="settingsTile">
  <summary>Settings</summary>
  <div class="settings-body">
    <div id="printerInfo" class="printer-line"></div>
    <label>Spoolman URL <span class="dim">(optional)</span>
      <input type="text" id="spoolman" placeholder="http://spoolman.local:7912" autocomplete="off">
    </label>
    <label class="inline"><input type="checkbox" id="ofd"> Open Filament Database <span class="dim">(recommended)</span></label>
    <div class="dim">On by default: find presets for filaments you have not installed. Disable to match only installed Orca presets.</div>
    <label id="ofdIndexWrap">OFD index URL
      <input type="text" id="ofdindex" autocomplete="off">
    </label>
    <div class="row"><button id="save">Save settings</button></div>
    <div class="row">
      <button id="clearCache" title="Delete the local OFD index and preset JSON cache. History and settings are kept.">Clear OFD cache</button>
    </div>
    <div class="dim">Next scan downloads a fresh OFD index and preset files.</div>
  </div>
</details>

<div id="presetModal" class="overlay" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2 id="presetTitle">Preset</h2>
      <button class="icon" id="presetClose" title="Close" aria-label="Close">×</button>
    </div>
    <div class="modal-body" id="presetBody"></div>
  </div>
</div>

<div id="chooser" class="overlay" hidden>
  <div class="modal">
    <div class="modal-head">
      <h2 id="chooserTitle">Choose a preset</h2>
      <button class="icon" id="chooserClose" title="Close" aria-label="Close">×</button>
    </div>
    <div class="modal-body" id="chooserList"></div>
    <div class="modal-foot"><button id="chooserPick">Use selected</button></div>
  </div>
</div>

<script>
'use strict';
const $ = id => document.getElementById(id);
let state = { settings: {}, presets: [], rows: [], connection: {}, slots: [], busy: false, mismatch: null,
  spoolman: { available: false, message: 'Add a Spoolman URL in Settings to fetch prices.' } };
let nextRid = 1;
let detailSlot = 0;
let chooserSlot = 0;

const esc = value => String(value ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function status(text, kind) {
  const node = $('status');
  node.textContent = text || '';
  node.className = kind || '';
}
function readSettings() {
  return {
    spoolman_url: $('spoolman').value.trim(),
    ofd_enabled: $('ofd').checked,
    ofd_index_url: $('ofdindex').value.trim()
  };
}
function applyCount() {
  return state.rows.filter(row => row.selected && row.dirty && !row.pending &&
    row.slot_exists !== false).length;
}
function updateApplyButton() {
  const count = applyCount();
  $('apply').textContent = count ? 'Apply (' + count + ')' : 'Apply';
  $('apply').disabled = state.busy || !count;
}
function setBusy(value) {
  state.busy = value;
  $('rescan').disabled = value;
  $('save').disabled = value;
  $('clearCache').disabled = value;
  $('apply').disabled = value || !applyCount();
  document.querySelectorAll('#rows select, #rows button, #rows input').forEach(node => {
    node.disabled = value;
  });
}
function post(command, extra) {
  if (state.busy) return;
  const rid = nextRid++;
  setBusy(true);
  orca.postMessage(Object.assign({command: command, rid: rid, settings: readSettings()}, extra || {}));
}
function printerText() {
  const c = state.connection || {};
  if (!c.name && !c.host) return 'No printer selected.';
  let text = c.name || 'Printer';
  if (c.host) text += ' — ' + c.host;
  if (c.connected) text += ' — connected';
  else if (c.selected) text += ' — not connected';
  return text;
}
function renderPrinter() {
  $('printerInfo').textContent = printerText();
}
function rowBySlot(slot) {
  return state.rows.find(row => Number(row.slot) === Number(slot)) || null;
}
function observationForm(obs) {
  return {
    vendor: obs.vendor || '', material: obs.material || '', subtype: obs.subtype || '',
    product: obs.product || '', color: obs.color || '', filament_id: obs.filament_id || '',
    spool_id: obs.spool_id == null ? '' : String(obs.spool_id),
    tag_uid: obs.tag_uid || '',
    hint: obs.hint || ''
  };
}
function readForm(slot) {
  const row = rowBySlot(slot);
  if (!row) return {};
  const values = Object.assign({}, row.form, {
    slot: Number(slot), present: row.observation.present !== false,
    source: row.observation.source || ''
  });
  document.querySelectorAll('#rows [data-slot="' + Number(slot) + '"][data-field]').forEach(node => {
    values[node.dataset.field] = node.value;
  });
  return values;
}
function applyRowState(row) {
  row.form = observationForm(row.observation || {});
  row.selected = row.preset_name || '';
  if (!row.selected && !row.error && (row.candidates || []).length) {
    const candidate = row.candidates[0];
    row.selected = row.source === 'library'
      ? 'lib:' + (candidate.preset_name || '')
      : (candidate.preset_name || '');
  }
  row.expanded = !!row.expanded;
  row.detail = null;
  row.detailLoading = false;
  row.apply_error = row.apply_error || '';
  row.current_price = row.current_price == null ? '' : String(row.current_price);
  if (!row.current_price && row.selected) {
    const priceInfo = selectedInfo(row, row.selected);
    row.current_price = priceInfo.price || '';
  }
  row.new_price = row.new_price == null
    ? (row.spool_price == null || row.spool_price === '' ? '' : String(row.spool_price))
    : String(row.new_price);
  row.price_touched = !!row.price_touched;
  row.price_pending = !!row.price_pending;
  row.price_error = row.price_error || '';
  row.dirty = !!(row.selected && !row.user_applied);
  return row;
}
function statusFor(row) {
  if (row.apply_error) return {kind: 'err', text: row.apply_error};
  if (row.pending) return {kind: 'warn', text: 'Applying…'};
  if (row.slot_exists === false) {
    return {kind: 'err', text: 'No Orca slot — add filament slot(s) in Orca'};
  }
  if (row.error) return {kind: 'err', text: row.error};
  if (row.choose) return {kind: 'warn', text: 'Multiple matches — pick one, then Apply'};
  if (row.source === 'library' && (row.candidates || []).length) {
    return {kind: 'warn', text: row.dirty
      ? 'Library match — press Apply to install' : 'Library match'};
  }
  if (row.preset_name) {
    if (row.user_applied) return {kind: 'ok', text: 'Applied'};
    if (row.applied) return {kind: 'ok', text: 'Already in slot'};
    if (row.source === 'generic') {
      return {kind: 'warn', text: 'Generic fallback — no vendor preset matched'};
    }
    if (row.dirty) return {kind: 'warn', text: 'Selected — press Apply'};
    return {kind: 'ok', text: 'Matched'};
  }
  return {kind: 'err', text: 'No match'};
}
function presetOptions(row) {
  const groups = [['user', 'Your presets'], ['bundle', 'Local bundles'],
                  ['system', 'System presets'], ['preset', 'Other presets']];
  let html = '<option value="">— select preset —</option>';
  groups.forEach(([prov, label]) => {
    const items = state.presets.filter(preset => preset.prov === prov);
    if (!items.length) return;
    html += '<optgroup label="' + esc(label) + '">';
    items.forEach(preset => {
      const selected = row.selected === preset.name ? ' selected' : '';
      html += '<option value="' + esc(preset.name) + '"' + selected + '>' +
        esc(preset.select_name || preset.name) + '</option>';
    });
    html += '</optgroup>';
  });
  const installed = new Set(state.presets.map(preset => preset.name));
  const extras = (row.candidates || []).filter(cand =>
    cand.source === 'library' && cand.preset_name && !installed.has(cand.preset_name));
  if (extras.length) {
    html += '<optgroup label="Library (not installed)">';
    extras.forEach(cand => {
      const value = 'lib:' + cand.preset_name;
      const selected = row.selected === value ? ' selected' : '';
      html += '<option value="' + esc(value) + '"' + selected + '>' +
        esc(cand.select_name || cand.preset_name) + '</option>';
    });
    html += '</optgroup>';
  }
  return html;
}
function selectedInfo(row, value) {
  if (!value) return {preset_name: '', source: '', candidate: null, price: ''};
  if (value.startsWith('lib:')) {
    const name = value.slice(4);
    const candidate = (row.candidates || []).find(cand =>
      cand.source === 'library' && cand.preset_name === name) || null;
    return {preset_name: name, source: 'library', candidate: candidate,
            price: candidate ? (candidate.cost || '') : ''};
  }
  const preset = state.presets.find(item => item.name === value) || null;
  const vendor = preset ? String(preset.vendor || '').toLowerCase() : '';
  const base = preset ? String(preset.base || preset.name || '') : '';
  const generic = vendor === 'generic' || /^generic(?:\s|$)/i.test(base);
  return {preset_name: value, source: generic ? 'generic' : 'installed',
          candidate: null, price: preset ? (preset.cost || '') : ''};
}
function field(label, name, slot, value, type, extra) {
  return '<label>' + esc(label) + '<input type="' + (type || 'text') +
    '" data-slot="' + Number(slot) + '" data-field="' + esc(name) + '" value="' +
    esc(value ?? '') + '"' + (extra || '') + '></label>';
}
function renderMismatch() {
  const box = $('mismatch');
  const mismatch = state.mismatch;
  if (!mismatch || (!mismatch.missing_slots && !mismatch.extra_slots)) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  const title = mismatch.missing_slots
    ? 'Project has fewer filament slots than the printer'
    : 'Project has more filament slots than the printer';
  box.innerHTML = '<strong>' + esc(title) + '</strong>' + esc(mismatch.message);
  box.hidden = false;
}
function priceBox(row) {
  const spool = row.form ? String(row.form.spool_id || '').trim() : '';
  const spoolman = state.spoolman || {};
  const fetchTitle = !spoolman.available
    ? (spoolman.message || 'Add a Spoolman URL in Settings to fetch prices.')
    : (!spool ? 'This feed has no Spoolman spool.' :
       'Fetch this spool price from Spoolman.');
  const fetchDisabled = !spoolman.available || !spool;
  const saveDisabled = !row.selected || !String(row.new_price ?? '').trim();
  return '<div class="price-box">' +
    '<div class="price-grid">' +
      '<label>Current price (per kg)<input type="number" step="0.01" min="0"' +
        ' data-price="current" data-slot="' + Number(row.slot) + '" value="' +
        esc(row.current_price ?? '') + '" readonly></label>' +
      '<label>New price (per kg)<input type="number" step="0.01" min="0"' +
        ' data-price="new" data-slot="' + Number(row.slot) + '" value="' +
        esc(row.new_price ?? '') + '"></label>' +
    '</div>' +
    '<div class="price-actions">' +
      '<span class="price-btn-wrap" title="' + esc(fetchTitle) + '">' +
        '<button data-action="fetch-price" data-slot="' + Number(row.slot) + '"' +
          (fetchDisabled ? ' disabled' : '') + '>Fetch price</button>' +
      '</span>' +
      '<button data-action="save-price" data-slot="' + Number(row.slot) + '"' +
        (saveDisabled ? ' disabled' : '') + '>Save price</button>' +
    '</div>' +
    (row.price_error ? '<div class="price-error">' +
      esc(row.price_error) + '</div>' : '') +
  '</div>';
}
function renderFeed(row) {
  const slot = Number(row.slot);
  const present = !row.observation || row.observation.present !== false;
  let html = '<section class="feed' +
    (row.slot_exists === false ? ' missing-slot' : '') +
    '" data-slot="' + slot + '">';
  html += '<div class="feed-row"><span class="slot">T' + slot + '</span>';
  if (!present) {
    html += '<span class="empty">Empty</span></div></section>';
    return html;
  }
  html += '<select data-role="preset" data-slot="' + slot + '" aria-label="Preset for T' + slot + '">' +
    presetOptions(row) + '</select>';
  html += '<button class="icon" data-action="edit" data-slot="' + slot +
    '" title="Edit preset" aria-label="Edit preset"' + (row.selected ? '' : ' disabled') + '>✎</button>';
  html += '<button class="icon" data-action="expand" data-slot="' + slot +
    '" title="Matching fields" aria-label="Matching fields">' + (row.expanded ? '▾' : '▸') + '</button>';
  html += '</div>';
  const info = statusFor(row);
  html += '<div class="feed-status ' + info.kind + '">' + esc(info.text) + '</div>';
  if (row.expanded) {
    const form = row.form;
    html += '<div class="fields"><div class="field-grid">' +
      field('Vendor', 'vendor', slot, form.vendor) +
      field('Material', 'material', slot, form.material) +
      field('Subtype', 'subtype', slot, form.subtype) +
      field('Product', 'product', slot, form.product) +
      field('Color', 'color', slot, form.color, 'text', ' placeholder="#RRGGBB"') +
      field('Filament ID', 'filament_id', slot, form.filament_id) +
      field('Spool ID', 'spool_id', slot, form.spool_id, 'number', ' min="1" step="1"') +
      field('Tag UID', 'tag_uid', slot, form.tag_uid) +
      '<input type="hidden" data-slot="' + slot + '" data-field="hint" value="' +
      esc(form.hint) + '">' +
      '</div><button class="match" data-action="match" data-slot="' + slot + '">Match</button>' +
      priceBox(row) + '</div>';
  }
  html += '</section>';
  return html;
}
function renderRows() {
  renderMismatch();
  $('rows').innerHTML = state.rows.map(renderFeed).join('');
  renderPrinter();
  updateApplyButton();
  if (state.busy) setBusy(true);
}
function updateRow(row) {
  const index = state.rows.findIndex(item => Number(item.slot) === Number(row.slot));
  const previous = index >= 0 ? state.rows[index] : null;
  const prepared = applyRowState(row);
  if (previous) {
    prepared.expanded = !!previous.expanded;
    prepared.new_price = previous.new_price;
    prepared.price_touched = previous.price_touched;
    prepared.price_pending = previous.selected === prepared.selected
      ? previous.price_pending : false;
    prepared.price_error = previous.price_error;
  }
  if (index >= 0) state.rows[index] = prepared; else state.rows.push(prepared);
  state.rows.sort((a, b) => Number(a.slot) - Number(b.slot));
  renderRows();
}
function choosePreset(slot, value) {
  const row = rowBySlot(slot);
  if (!row) return;
  const info = selectedInfo(row, value);
  row.selected = value || '';
  row.apply_error = '';
  row.pending = false;
  row.applied = false;
  row.user_applied = false;
  row.choose = false;
  if (value) row.error = '';
  row.current_price = value ? (info.price || '') : '';
  row.price_pending = false;
  row.price_error = '';
  row.dirty = !!value;
  renderRows();
  if (!value) { status('No preset selected.', 'info'); return; }
  status('T' + slot + ': selected ' +
    (info.source === 'library' ? info.preset_name : (info.preset_name || value)) +
    '. Press Apply to set it in Orca.', 'warn');
}
function applyAll() {
  if (state.busy) return;
  const entries = state.rows.filter(row => row.selected && row.dirty &&
      row.slot_exists !== false)
    .map(row => {
      const info = selectedInfo(row, row.selected);
      return {
        slot: Number(row.slot), preset_name: info.preset_name, source: info.source,
        candidate: info.candidate, remember: info.source !== 'generic',
        price: row.price_pending ? row.new_price : '',
        observation: readForm(row.slot)
      };
    });
  if (!entries.length) {
    if (state.mismatch && state.mismatch.missing_slots) {
      status(state.mismatch.message, 'warn');
    } else {
      status('Nothing to apply — select at least one preset.', 'info');
    }
    return;
  }
  status('Applying ' + entries.length + ' feed(s)…', 'info');
  post('apply_all', {entries: entries, feeders: state.rows.length});
}
function configTable(pairs) {
  if (!pairs || !pairs.length) return '<p class="dim">No own settings.</p>';
  return '<table><tbody>' + pairs.map(pair =>
    '<tr><td>' + esc(pair[0]) + '</td><td>' + esc(pair[1]) + '</td></tr>').join('') +
    '</tbody></table>';
}
function libraryTable(cand) {
  const meta = (cand && cand.library) || {};
  const rows = [
    ['Brand', meta.brand || ''],
    ['Product', meta.filament || ''],
    ['Inherits', meta.inherits || cand.inherits || ''],
    ['Source', cand.library_url || meta.url || '']
  ];
  return '<table><tbody>' + rows.map(pair =>
    '<tr><td>' + esc(pair[0]) + '</td><td>' + esc(pair[1] || '—') + '</td></tr>').join('') +
    '</tbody></table>';
}
function openPreset(slot) {
  const row = rowBySlot(slot);
  if (!row || !row.selected) return;
  closeChooser();
  if (row.selected.startsWith('lib:')) {
    const info = selectedInfo(row, row.selected);
    $('presetTitle').textContent = info.preset_name;
    $('presetBody').innerHTML = libraryTable(info.candidate);
    $('presetModal').hidden = false;
    return;
  }
  const preset = state.presets.find(item => item.name === row.selected);
  detailSlot = Number(slot);
  $('presetTitle').textContent = preset ? (preset.select_name || preset.name) : row.selected;
  $('presetBody').innerHTML = '<p class="dim">Loading…</p>';
  $('presetModal').hidden = false;
  status('', '');
  post('detail', {slot: Number(slot), preset_name: row.selected});
}
function closePreset() {
  $('presetModal').hidden = true;
  detailSlot = 0;
}
function candidateValue(cand) {
  return cand.source === 'library' ? 'lib:' + cand.preset_name : cand.preset_name;
}
function openChooser(slot) {
  const row = rowBySlot(slot);
  if (!row) return;
  const candidates = (row.candidates || []).filter(cand => cand.preset_name);
  if (candidates.length <= 1) return;
  closePreset();
  chooserSlot = Number(slot);
  $('chooserTitle').textContent = 'Choose a preset for T' + slot;
  const current = row.selected || '';
  $('chooserList').innerHTML = candidates.map((cand, index) => {
    const value = candidateValue(cand);
    const meta = [cand.provenance || cand.source || '',
                  cand.score != null ? Number(cand.score) + '%' : '',
                  cand.vendor, cand.material, cand.subtype]
      .filter(Boolean).join(' · ');
    return '<label class="cand"><input type="radio" name="chooser" value="' + index + '"' +
      (current === value ? ' checked' : '') + '>' +
      '<div class="cand-text"><div class="cand-name">' + esc(cand.select_name || cand.preset_name) + '</div>' +
      '<div class="cand-meta">' + esc(meta || '—') + '</div></div></label>';
  }).join('');
  if (!$('chooserList').querySelector('input:checked')) {
    const first = $('chooserList').querySelector('input');
    if (first) first.checked = true;
  }
  $('chooser').hidden = false;
}
function closeChooser() {
  $('chooser').hidden = true;
  chooserSlot = 0;
}
function pickChooser() {
  const row = rowBySlot(chooserSlot);
  if (!row) { closeChooser(); return; }
  const checked = document.querySelector('#chooserList input[name="chooser"]:checked');
  if (!checked) { closeChooser(); return; }
  const candidates = (row.candidates || []).filter(cand => cand.preset_name);
  const cand = candidates[Number(checked.value)];
  if (!cand) { closeChooser(); return; }
  const value = candidateValue(cand);
  closeChooser();
  choosePreset(Number(row.slot), value);
}
function expandRow(slot) {
  const row = rowBySlot(slot);
  if (!row) return;
  row.expanded = !row.expanded;
  renderRows();
}
function matchRow(slot) {
  const row = rowBySlot(slot);
  if (!row) return;
  status('Matching…', 'info');
  post('match', {
    slot: Number(slot), observation: readForm(slot)
  });
}
function fetchPrice(slot) {
  const row = rowBySlot(slot);
  if (!row) return;
  const values = readForm(slot);
  if (!String(values.spool_id || '').trim()) return;
  status('Fetching price from Spoolman…', 'info');
  post('fetch_price', {
    slot: Number(slot), observation: values
  });
}
function savePrice(slot) {
  const row = rowBySlot(slot);
  if (!row) return;
  if (!row.selected) {
    row.price_error = 'Choose a preset before saving a price.';
    renderRows();
    return;
  }
  const value = String(row.new_price ?? '').trim();
  const number = Number(value);
  if (!value || !Number.isFinite(number) || number < 0) {
    row.price_error = 'Enter a valid non-negative price.';
    renderRows();
    return;
  }
  row.current_price = value;
  row.price_pending = true;
  row.price_touched = true;
  row.dirty = true;
  row.price_error = '';
  renderRows();
  status('T' + slot + ': price staged at ' + value +
    '/kg. Press Apply to write it into the preset.', 'warn');
}

$('rows').addEventListener('click', event => {
  const button = event.target.closest('[data-action]');
  if (!button || state.busy) return;
  const slot = Number(button.dataset.slot);
  const action = button.dataset.action;
  if (action === 'edit') openPreset(slot);
  else if (action === 'expand') expandRow(slot);
  else if (action === 'match') matchRow(slot);
  else if (action === 'fetch-price') fetchPrice(slot);
  else if (action === 'save-price') savePrice(slot);
});
$('rows').addEventListener('change', event => {
  const select = event.target.closest('select[data-role="preset"]');
  if (!select) return;
  choosePreset(Number(select.dataset.slot), select.value);
});
$('rows').addEventListener('input', event => {
  const priceInput = event.target.closest('[data-price="new"]');
  if (priceInput) {
    const row = rowBySlot(priceInput.dataset.slot);
    if (!row) return;
    row.new_price = priceInput.value;
    row.price_touched = true;
    row.price_pending = false;
    row.price_error = '';
    const save = document.querySelector(
      'button[data-action="save-price"][data-slot="' + priceInput.dataset.slot + '"]');
    if (save) save.disabled = !String(row.new_price ?? '').trim();
    return;
  }
  const input = event.target.closest('[data-field]');
  if (!input) return;
  const row = rowBySlot(input.dataset.slot);
  if (row) row.form[input.dataset.field] = input.value;
  if (input.dataset.field === 'spool_id') {
    const fetch = document.querySelector(
      'button[data-action="fetch-price"][data-slot="' + input.dataset.slot + '"]');
    const wrap = fetch ? fetch.closest('.price-btn-wrap') : null;
    const available = !!(state.spoolman && state.spoolman.available);
    const hasSpool = !!String(input.value || '').trim();
    if (fetch) fetch.disabled = !(available && hasSpool);
    if (wrap) {
      wrap.title = !available
        ? ((state.spoolman && state.spoolman.message) ||
           'Add a Spoolman URL in Settings to fetch prices.')
        : (!hasSpool ? 'This feed has no Spoolman spool.' :
           'Fetch this spool price from Spoolman.');
    }
  }
});
$('apply').onclick = applyAll;
$('rescan').onclick = () => {
  if (state.busy) return;
  status('Scanning printer…', 'info');
  post('scan');
};
$('save').onclick = () => {
  status('Saving…', 'info');
  post('save');
};
$('clearCache').onclick = () => {
  status('Clearing OFD cache…', 'info');
  post('clear_cache');
};
$('ofd').onchange = () => {
  const wrap = $('ofdIndexWrap');
  wrap.hidden = !$('ofd').checked;
};
$('presetClose').onclick = closePreset;
$('presetModal').addEventListener('click', event => {
  if (event.target === $('presetModal')) closePreset();
});
$('chooserClose').onclick = closeChooser;
$('chooserPick').onclick = pickChooser;
$('chooser').addEventListener('click', event => {
  if (event.target === $('chooser')) closeChooser();
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (!$('chooser').hidden) { closeChooser(); return; }
  if (!$('presetModal').hidden) closePreset();
});

function markRowApplied(applied) {
  const row = rowBySlot(applied.slot);
  if (!row) return;
  row.current = applied.select_name || applied.preset_name;
  row.current_name = applied.preset_name;
  row.preset_name = applied.preset_name;
  row.select_name = applied.select_name || applied.preset_name;
  row.selected = applied.preset_name;
  row.source = 'installed';
  row.candidates = [];
  row.applied = true;
  row.user_applied = true;
  row.pending = false;
  row.choose = false;
  row.error = '';
  row.apply_error = '';
  if (applied.price) {
    row.current_price = String(applied.price);
    row.new_price = String(applied.price);
    row.price_pending = false;
    row.price_touched = false;
    row.price_error = '';
  }
  row.dirty = false;
}

orca.onMessage(message => {
  if (!message) return;
  if (message.error) {
    setBusy(false);
    if (!$('presetModal').hidden && message.slot &&
        Number(message.slot) === detailSlot) {
      $('presetBody').innerHTML = '<p class="dim">' + esc(message.error) + '</p>';
    } else if (message.slot) {
      const row = rowBySlot(message.slot);
      if (row) {
        row.apply_error = message.error;
        row.pending = false;
        row.selected = row.current_name || row.preset_name || '';
        renderRows();
      }
    }
    status(message.error, 'err');
    return;
  }
  setBusy(false);
  if (message.price_error) {
    const item = message.price_error;
    const row = rowBySlot(item.slot);
    if (row) {
      row.price_error = item.error || 'Could not fetch the price.';
      row.pending = false;
      renderRows();
    }
    status(item.error || 'Could not fetch the price.', 'err');
    return;
  }
  if (message.init) {
    state.settings = message.init.settings || {};
    state.presets = message.init.presets || [];
    state.slots = message.init.slots || [];
    state.connection = message.init.connection || {};
    $('spoolman').value = state.settings.spoolman_url || '';
    $('ofd').checked = !!state.settings.ofd_enabled;
    $('ofdindex').value = state.settings.ofd_index_url || '';
    $('ofdIndexWrap').hidden = !$('ofd').checked;
    renderRows();
    status('Scanning printer…', 'info');
    post('scan');
  }
  if (message.created) {
    state.presets = message.created.presets || state.presets;
    state.slots = message.created.slots || state.slots;
    if (message.created.connection) state.connection = message.created.connection;
    renderRows();
  }
  if (message.cache_cleared) {
    status(message.message || 'OFD cache cleared.', 'ok');
  }
  if (message.settings) {
    state.settings = message.settings;
    if (message.settings.spoolman_url !== undefined) $('spoolman').value = message.settings.spoolman_url || '';
    if (message.settings.ofd_enabled !== undefined) $('ofd').checked = !!message.settings.ofd_enabled;
    if (message.settings.ofd_index_url !== undefined) $('ofdindex').value = message.settings.ofd_index_url || '';
    $('ofdIndexWrap').hidden = !$('ofd').checked;
    status('Settings saved.', 'ok');
  }
  if (message.scan) {
    state.mismatch = message.slot_mismatch || null;
    state.spoolman = message.spoolman || state.spoolman;
    const previousRows = new Map(
      state.rows.map(row => [Number(row.slot), row]));
    state.rows = (message.scan || []).map(raw => {
      const row = applyRowState(raw);
      const old = previousRows.get(Number(row.slot));
      if (old) {
        row.new_price = old.price_touched
          ? old.new_price
          : (row.spool_price !== '' ? String(row.spool_price) : old.new_price);
        row.price_touched = old.price_touched;
        row.price_pending = old.selected === row.selected
          ? old.price_pending : false;
        row.price_error = old.price_error;
      }
      return row;
    });
    state.rows.sort((a, b) => Number(a.slot) - Number(b.slot));
    renderRows();
    const present = state.rows.filter(row =>
      !row.observation || row.observation.present !== false).length;
    const issues = state.rows.filter(row => row.error || row.apply_error || row.choose).length;
    const pending = state.rows.filter(row => row.selected && row.dirty).length;
    let text = message.message || ('Matched ' + present + ' feed(s).');
    if (pending) text += ' Press Apply to set ' + pending + ' feed(s) in Orca.';
    const warnings = (message.warnings || []).join(' ');
    if (warnings) text += ' ' + warnings;
    status(text, (issues || !present || pending || warnings) ? 'warn' : 'ok');
  }
  if (message.price_fetched) {
    const fetched = message.price_fetched;
    const row = rowBySlot(fetched.slot);
    if (row) {
      row.new_price = String(fetched.price);
      row.price_touched = true;
      row.price_pending = false;
      row.price_error = '';
      renderRows();
    }
    const warnings = (message.warnings || []).join(' ');
    status([message.message || 'Price fetched.', warnings]
      .filter(Boolean).join(' '), warnings ? 'warn' : 'ok');
  }
  if (message.match) {
    updateRow(message.match);
    const problem = message.match.apply_error || message.match.error;
    if (message.match.choose && (message.match.candidates || []).length > 1) {
      openChooser(message.match.slot);
      status(message.message || 'Multiple matches — choose one, then press Apply.', 'warn');
    } else {
      const warnings = (message.warnings || []).join(' ');
      status([problem || message.message || '', warnings].filter(Boolean).join(' '),
             problem ? 'err' : 'warn');
    }
  }
  if (message.applied) {
    markRowApplied(message.applied);
    renderRows();
    if (message.message) status(message.message, 'ok');
    if (message.refresh_presets) post('refresh');
  }
  if (message.applied_all) {
    state.mismatch = message.slot_mismatch || state.mismatch;
    (message.applied_all || []).forEach(markRowApplied);
    (message.apply_skipped || []).forEach(item => {
      const row = rowBySlot(item.slot);
      if (row) {
        row.apply_error = '';
        row.pending = false;
      }
    });
    (message.apply_errors || []).forEach(item => {
      const row = rowBySlot(item.slot);
      if (!row) return;
      row.apply_error = item.error || 'Could not apply';
      row.dirty = true;
    });
    renderRows();
    const failed = (message.apply_errors || []).length;
    const warnings = (message.warnings || []).join(' ');
    status([message.message || 'Applied.', warnings].filter(Boolean).join(' '),
           failed ? 'err' : (warnings ? 'warn' : 'ok'));
    if (message.refresh_presets) post('refresh');
  }
  if (message.detail) {
    if (!$('presetModal').hidden && Number(message.detail.slot) === detailSlot) {
      const detail = message.detail.preset || {};
      $('presetTitle').textContent = detail.select_name || detail.name || 'Preset';
      $('presetBody').innerHTML = configTable(detail.config || []);
    }
  }
});
orca.postMessage({command: 'init'});
</script></body></html>
"""


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------

if orca is not None:
    class FilamentMatch(orca.script.ScriptPluginCapabilityBase):
        window = None
        _commands = None
        _cancelled = False
        _storage = None
        _session = None

        def get_name(self):
            return "Filament Match"

        # ---- lifecycle -----------------------------------------------------

        def execute(self):
            if not _DEPS:
                return orca.ExecutionResult.failure(
                    orca.PluginResult.RecoverableError, _deps_error())
            if self.window is not None and self.window.is_open():
                try:
                    show = getattr(self.window, "show", None)
                    if callable(show):
                        show()
                    self.window.post({"message": "Filament Match is already open."})
                except Exception:
                    pass
                return orca.ExecutionResult.success("Filament Match is already open.")
            commands = queue.Queue()
            self._commands = commands
            self._cancelled = False
            # plugin.storage() is only valid inside the capability's own host
            # callbacks. execute() is the one callback guaranteed to run before
            # the panel exists, so resolve and cache the folder here and never
            # call storage() from the UI message handler or from the worker.
            try:
                self._storage = os.fspath(orca.host.plugin.storage())
                os.makedirs(self._storage, exist_ok=True)
            except Exception as error:
                return orca.ExecutionResult.failure(
                    orca.PluginResult.RecoverableError,
                    "Could not initialize the plugin storage: %s" % _error_text(error))
            self._session = {
                "spoolman_client": None,
                "spoolman_url": "",
                "spoolman_cache": {},
                "ofd_library": None,
                "ofd_key": None,
            }
            try:
                if self._cancelled:
                    return orca.ExecutionResult.success("Closed.")
                # Script execute() runs on a dedicated background Script worker
                # in Orca 2.5.x; the dock panel is created on the UI thread and
                # returns immediately. The loop below handles work the page
                # asks for.
                self.window = self._open_ui()
                while True:
                    message = commands.get()
                    if self._cancelled or message.get("command") == "close":
                        return orca.ExecutionResult.success("Closed.")
                    try:
                        reply = self._run(message)
                    except Exception as error:
                        print("[Filament Match] %s" % _error_text(error), file=sys.stderr)
                        reply = {
                            "rid": message.get("rid"),
                            "slot": message.get("slot"),
                            "error": _error_text(error),
                        }
                    if self.window is not None and self.window.is_open():
                        self.window.post(reply)
            except Exception as error:
                return orca.ExecutionResult.failure(
                    orca.PluginResult.RecoverableError,
                    "Filament Match failed: %s" % _error_text(error))
            finally:
                if self._commands is commands:
                    self._commands = None
                self.window = None

        def _open_ui(self):
            """A docked panel beside the sidebar; a window on older hosts."""
            ui = orca.host.ui
            factory = getattr(ui, "create_dock_panel", None)
            if callable(factory):
                try:
                    return factory(
                        PAGE, title="Filament Match", width=460, height=760,
                        on_message=self._on_message, on_close=self._on_close,
                        dock="right")
                except Exception as error:
                    print("[Filament Match] dock panel unavailable: %s"
                          % _error_text(error), file=sys.stderr)
            return ui.create_window(
                PAGE, title="Filament Match", width=1000, height=760,
                on_message=self._on_message, on_close=self._on_close)

        def _storage_path(self):
            if not self._storage:
                raise RuntimeError(
                    "The plugin storage is not ready. Run Filament Match again from "
                    "Plugins > Run.")
            return self._storage

        # ---- worker thread -------------------------------------------------

        def _run(self, message):
            _require_deps()
            command = _text(message.get("command"))
            settings = _normalize_settings(
                message.get("settings") or _load_settings(
                    message.get("storage") or self._storage_path()))
            storage = _text(message.get("storage")) or self._storage_path()
            snapshot = message.get("snapshot") or {}
            warnings = []
            rid = message.get("rid")
            session = self._session or {}

            if command == "scan":
                rows = _scan_rows(settings, snapshot, storage, session, warnings)
                present = sum(
                    1 for row in rows
                    if (row.get("observation") or {}).get("present") is not False)
                mismatch = _slot_mismatch(
                    len(rows), len(snapshot.get("slots") or []))
                message_text = (
                    "Matched %d feed(s)." % present if present
                    else "No filament feeds detected.")
                if mismatch["message"]:
                    message_text = "%s %s" % (message_text, mismatch["message"])
                return {
                    "rid": rid,
                    "scan": rows,
                    "slot_mismatch": mismatch,
                    "spoolman": session.get("spoolman_status") or {
                        "available": False,
                        "message": "Add a Spoolman URL in Settings to fetch prices.",
                    },
                    "message": message_text,
                    "warnings": warnings,
                }

            if command == "fetch_price":
                slot = int(message.get("slot") or 0)
                try:
                    price, spool_id = _fetch_spoolman_price(
                        settings, session, warnings,
                        message.get("observation") or {})
                except Exception as error:
                    text = _permission_hint(error) or _error_text(error)
                    return {
                        "rid": rid,
                        "price_error": {"slot": slot, "error": text},
                        "message": text,
                        "warnings": warnings,
                    }
                return {
                    "rid": rid,
                    "price_fetched": {
                        "slot": slot,
                        "spool_id": spool_id,
                        "price": price,
                    },
                    "message": "Fetched %s/kg for spool %s from Spoolman." % (
                        price, spool_id),
                    "warnings": warnings,
                }

            if command == "match":
                history = _history_store(storage, warnings)
                observation_values = message.get("observation") or {}
                observation = _observation_from_form(
                    message.get("slot") or 0, observation_values)
                row = _build_row(observation, snapshot, history, settings, storage, session, warnings,
                                 use_history=False)
                if row["preset_name"]:
                    message_text = "Matched T%s: %s. Press Apply to set it in Orca." % (
                        row.get("slot"), row.get("select_name") or row["preset_name"])
                elif row.get("choose"):
                    message_text = (
                        "T%s has multiple matches. Choose one, then press Apply." %
                        row.get("slot"))
                elif row.get("source") == "library" and row.get("candidates"):
                    candidate = row["candidates"][0]
                    label = _text(candidate.get("select_name")) or _text(
                        candidate.get("preset_name"))
                    message_text = (
                        "T%s: library match %s. Press Apply to install it." %
                        (row.get("slot"), label))
                else:
                    message_text = row["error"] or "No match."
                return {
                    "rid": rid,
                    "match": row,
                    "choose": row.get("choose"),
                    "applied": None,
                    "refresh_presets": False,
                    "message": message_text,
                    "warnings": warnings,
                }

            if command == "apply_all":
                entries = message.get("entries") or []
                if not isinstance(entries, list):
                    raise ValueError("No feeds were supplied to apply")
                slots = len(snapshot.get("slots") or [])
                applied_rows = []
                errors = []
                skipped = []
                refresh_presets = False
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    slot = entry.get("slot")
                    try:
                        slot_number = int(slot or 0)
                    except (TypeError, ValueError):
                        slot_number = 0
                    if slot_number < 1 or slot_number > slots:
                        skipped.append({
                            "slot": slot,
                            "reason": (
                                "This project has no filament slot %s; add more "
                                "slots in Orca's Filament tab." % slot),
                        })
                        continue
                    try:
                        reply = _apply_selection(
                            entry, snapshot, settings, storage, session, warnings)
                    except Exception as error:
                        errors.append({"slot": slot, "error": _error_text(error)})
                        _warn(warnings, "T%s: %s" % (slot, _error_text(error)))
                        continue
                    applied = reply.get("applied")
                    if isinstance(applied, dict):
                        applied_rows.append(applied)
                    refresh_presets = refresh_presets or bool(
                        reply.get("refresh_presets"))
                try:
                    feeders = int(message.get("feeders") or 0)
                except (TypeError, ValueError):
                    feeders = 0
                if feeders < 1:
                    feeders = max(len(entries), slots + len(skipped))
                mismatch = _slot_mismatch(feeders, slots)
                if skipped:
                    missing = len(skipped)
                    if applied_rows:
                        message_text = (
                            "Applied %d feed(s). %d feed(s) have no Orca slot; "
                            "add %d filament slot(s) in Orca's Filament tab, "
                            "then press ↻." %
                            (len(applied_rows), missing,
                             max(missing, mismatch["missing_slots"])))
                    else:
                        message_text = (
                            "No feeds were applied. This project has %d "
                            "filament slot(s); add %d slot(s) in Orca's "
                            "Filament tab, then press ↻." %
                            (slots, max(missing, mismatch["missing_slots"])))
                    if errors:
                        message_text += " %d preset error(s) also occurred." % len(errors)
                elif errors and not applied_rows:
                    message_text = "Could not apply: %s" % errors[0]["error"]
                elif errors:
                    message_text = "Applied %d feed(s); %d failed." % (
                        len(applied_rows), len(errors))
                elif mismatch["extra_slots"]:
                    message_text = (
                        "Applied %d feed(s). The project has %d extra filament "
                        "slot(s); remove them in Orca's Filament tab for an "
                        "exact match." %
                        (len(applied_rows), mismatch["extra_slots"]))
                else:
                    message_text = "Applied %d feed(s)." % len(applied_rows)
                return {
                    "rid": rid,
                    "applied_all": applied_rows,
                    "apply_errors": errors,
                    "apply_skipped": skipped,
                    "slot_mismatch": mismatch,
                    "refresh_presets": refresh_presets,
                    "message": message_text,
                    "warnings": warnings,
                }

            if command in ("apply", "select"):
                if command == "select":
                    message = dict(message)
                    message["remember"] = False
                reply = _apply_selection(message, snapshot, settings, storage, session, warnings)
                reply["rid"] = rid
                return reply

            raise ValueError("Unknown command: %s" % command)

        # ---- UI thread -----------------------------------------------------

        def _on_message(self, message):
            message = dict(message) if isinstance(message, dict) else {}
            command = _text(message.get("command"))
            rid = message.get("rid")
            try:
                storage = self._storage_path()
                if command == "clear_cache":
                    removed = _clear_ofd_cache(storage, self._session)
                    pieces = []
                    if removed["index_removed"]:
                        pieces.append("index")
                    if removed["preset_files_removed"]:
                        pieces.append("%d preset file(s)" %
                                      removed["preset_files_removed"])
                    detail = " and ".join(pieces) if pieces else "nothing (already empty)"
                    self.window.post({
                        "rid": rid,
                        "cache_cleared": removed,
                        "message": ("OFD cache cleared: %s. Press ↻ to download "
                                    "fresh data." % detail),
                    })
                    return

                if command == "init":
                    snapshot = _preset_snapshot(orca.host.preset_bundle())
                    self.window.post({
                        "rid": rid,
                        "init": {
                            "settings": _load_settings(storage),
                            "presets": snapshot["ui_presets"],
                            "slots": snapshot["slots"],
                            "printer": snapshot["printer"],
                            "connection": _public_printer_info(snapshot["printer_info"]),
                        },
                    })
                    return

                if command == "refresh":
                    snapshot = _preset_snapshot(orca.host.preset_bundle())
                    self.window.post({
                        "rid": rid,
                        "created": {
                            "presets": snapshot["ui_presets"],
                            "slots": snapshot["slots"],
                            "printer": snapshot["printer"],
                            "connection": _public_printer_info(snapshot["printer_info"]),
                        },
                        "message": "Preset list refreshed.",
                    })
                    return

                if command == "save":
                    settings = _save_settings(
                        storage, _merge_settings(_load_settings(storage), message.get("settings")))
                    self.window.post({
                        "rid": rid,
                        "settings": settings,
                        "message": "Settings saved.",
                    })
                    return

                if command == "detail":
                    detail = _live_preset_detail(
                        orca.host.preset_bundle(), _text(message.get("preset_name")))
                    if detail is None:
                        raise RuntimeError(
                            'The preset "%s" is no longer loaded. Press Refresh presets.'
                            % _text(message.get("preset_name")))
                    self.window.post({
                        "rid": rid,
                        "detail": {
                            "slot": int(message.get("slot") or 0),
                            "preset": detail,
                        },
                    })
                    return

                if command in ("mute", "unmute"):
                    settings = _load_settings(storage)
                    identity = _text(message.get("identity"))
                    if not identity:
                        raise ValueError("This slot has no filament identity to mute")
                    ignored = list(settings.get("ignored") or [])
                    if command == "mute" and identity not in ignored:
                        ignored.append(identity)
                    elif command == "unmute":
                        ignored = [item for item in ignored if item != identity]
                    settings["ignored"] = ignored[:500]
                    settings = _save_settings(storage, settings)
                    self.window.post({
                        "rid": rid,
                        "muted": settings["ignored"],
                        "message": "Muted for this filament identity." if command == "mute"
                                   else "Unmuted.",
                    })
                    return

                if command in ("scan", "match", "apply", "select",
                               "apply_all", "fetch_price"):
                    if command == "fetch_price":
                        snapshot = {}
                    else:
                        snapshot = _preset_snapshot(orca.host.preset_bundle())
                        if command == "scan":
                            problem = _printer_problem(
                                snapshot.get("printer_info") or {})
                            if problem:
                                self._show_message(problem)
                                self.window.post({"rid": rid, "error": problem})
                                return
                    message["snapshot"] = snapshot
                    message["storage"] = storage
                    message["settings"] = _merge_settings(
                        _load_settings(storage), message.get("settings"))
                    self._enqueue(message)
                    return

                raise ValueError("Unknown command: %s" % command)
            except Exception as error:
                print("[Filament Match] %s" % _error_text(error), file=sys.stderr)
                if self.window is not None and self.window.is_open():
                    self.window.post({
                        "rid": rid,
                        "slot": message.get("slot"),
                        "error": _error_text(error),
                    })

        def _show_message(self, text):
            try:
                orca.host.ui.message(
                    text, title="Filament Match", buttons="ok", icon="warning")
            except Exception:
                pass

        def _on_close(self):
            self._enqueue({"command": "close"})

        def on_cancelled(self):
            self._cancelled = True
            self._enqueue({"command": "close"})

        def _enqueue(self, message):
            commands = self._commands
            if commands is not None:
                commands.put(message)

    @orca.plugin
    class FilamentMatchPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(FilamentMatch)
