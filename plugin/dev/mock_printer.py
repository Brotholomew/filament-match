#!/usr/bin/env python3
"""Mock Snapmaker/Moonraker printer for Filament Match development.

The server exposes the two endpoints the plugin reads:

    GET /printer/objects/query?print_task_config&filament_detect   (Snapmaker)
    GET /server/database/item?namespace=lane_data                  (Moonraker)

The feeder list is editable at http://<host>:<port>/ and through
GET/POST /__state.  It is deliberately printer-agnostic: select a different
Orca printer preset to test compatibility against that printer's presets.

Run:
    python3 plugin/dev/mock_printer.py --port 7125
    python3 plugin/dev/mock_printer.py --port 7125 --config /tmp/mock.json
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_FEEDERS = 64
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

DEFAULT_FEEDER = {
    "present": True,
    "vendor": "",
    "material": "PLA",
    "subtype": "",
    "product": "",
    "color": "#FFFFFF",
    "spool_id": 0,
    "filament_id": "",
    "bed_temp": 60,
    "nozzle_temp": 220,
}

DEFAULT_STATE = {
    "mode": "both",
    "printer": "Mock Snapmaker U1",
    "feeders": [
        {**DEFAULT_FEEDER, "present": False, "material": "", "color": ""},
        {**DEFAULT_FEEDER, "present": False, "material": "", "color": ""},
        {
            **DEFAULT_FEEDER,
            "present": True,
            "vendor": "3DJake",
            "material": "ABS",
            "color": "#4DB6AC",
            "spool_id": 7,
            "bed_temp": 100,
            "nozzle_temp": 260,
        },
        {
            **DEFAULT_FEEDER,
            "present": True,
            "vendor": "Snapmaker",
            "material": "PLA",
            "subtype": "SnapSpeed",
            "color": "#000000",
            "spool_id": 3,
            "bed_temp": 60,
            "nozzle_temp": 230,
        },
    ],
}


def normalize_color(value):
    text = str(value or "").strip()
    match = _HEX_RE.fullmatch(text)
    if not match:
        return ""
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    elif len(digits) == 8:
        digits = digits[:6]
    return "#" + digits.upper()


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def clean_feeder(raw):
    raw = raw if isinstance(raw, dict) else {}
    present = _bool(raw.get("present", True))
    return {
        "present": present,
        "vendor": str(raw.get("vendor") or "").strip(),
        "material": str(raw.get("material") or "").strip(),
        "subtype": str(raw.get("subtype") or "").strip(),
        "product": str(raw.get("product") or "").strip(),
        "color": normalize_color(raw.get("color")) or ("#FFFFFF" if present else ""),
        "spool_id": max(0, _int(raw.get("spool_id"), 0)),
        "filament_id": str(raw.get("filament_id") or "").strip(),
        "bed_temp": max(0, _int(raw.get("bed_temp"), 0)),
        "nozzle_temp": max(0, _int(raw.get("nozzle_temp"), 0)),
    }


def clean_state(raw):
    if not isinstance(raw, dict):
        raise ValueError("state must be an object")
    feeders = raw.get("feeders")
    if not isinstance(feeders, list) or not feeders:
        raise ValueError("feeders must be a non-empty list")
    if len(feeders) > MAX_FEEDERS:
        raise ValueError("at most %d feeders are supported" % MAX_FEEDERS)
    mode = str(raw.get("mode") or "both").strip().lower()
    if mode not in ("both", "snapmaker", "moonraker"):
        raise ValueError("mode must be both, snapmaker or moonraker")
    return {
        "mode": mode,
        "printer": str(raw.get("printer") or DEFAULT_STATE["printer"]).strip(),
        "feeders": [clean_feeder(feeder) for feeder in feeders],
    }


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._state = copy.deepcopy(DEFAULT_STATE)
        if self.path and self.path.exists():
            try:
                self._state = clean_state(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as error:
                print("Could not read %s: %s; using defaults" % (self.path, error))

    def get(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def replace(self, raw):
        state = clean_state(raw)
        with self._lock:
            self._state = state
            self._save_locked()
        return copy.deepcopy(state)

    def reset(self):
        with self._lock:
            self._state = copy.deepcopy(DEFAULT_STATE)
            self._save_locked()
        return copy.deepcopy(self._state)

    def _save_locked(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def _rgba(color):
    color = normalize_color(color) or "#FFFFFF"
    return color + "FF"


def snapmaker_payload(state):
    feeders = state["feeders"]
    present = [bool(feeder["present"]) for feeder in feeders]
    material = [feeder["material"] if feeder["present"] else "" for feeder in feeders]
    subtype = [feeder["subtype"] if feeder["present"] else "" for feeder in feeders]
    vendor = [feeder["vendor"] if feeder["present"] else "" for feeder in feeders]
    color = [_rgba(feeder["color"]) if feeder["present"] else "FFFFFFFF"
             for feeder in feeders]
    spool = [feeder["spool_id"] if feeder["present"] else 0 for feeder in feeders]
    filament_id = [feeder["filament_id"] if feeder["present"] else ""
                   for feeder in feeders]
    info = []
    for feeder in feeders:
        if feeder["present"] and (feeder["bed_temp"] or feeder["nozzle_temp"]):
            info.append({
                "BED_TEMP": feeder["bed_temp"],
                "HOTEND_MAX_TEMP": feeder["nozzle_temp"],
            })
        else:
            info.append({})
    return {
        "result": {
            "status": {
                "print_task_config": {
                    "filament_exist": present,
                    "filament_type": material,
                    "filament_sub_type": subtype,
                    "filament_color_rgba": color,
                    "filament_vendor": vendor,
                    "filament_spool_id": spool,
                    "filament_id": filament_id,
                },
                "filament_detect": {"info": info},
            },
            "eventtime": time.time(),
        }
    }


def moonraker_payload(state):
    lanes = {}
    for index, feeder in enumerate(state["feeders"]):
        lane = {"lane": str(index)}
        if feeder["present"]:
            if feeder["material"]:
                lane["material"] = feeder["material"]
            if feeder["vendor"]:
                lane["vendor_name"] = feeder["vendor"]
            if feeder["subtype"]:
                lane["subtype"] = feeder["subtype"]
            if feeder["product"]:
                lane["product"] = feeder["product"]
            if feeder["color"]:
                lane["color"] = feeder["color"]
            if feeder["filament_id"]:
                lane["filament_id"] = feeder["filament_id"]
            if feeder["spool_id"]:
                lane["spool_id"] = feeder["spool_id"]
            if feeder["bed_temp"]:
                lane["bed_temp"] = feeder["bed_temp"]
            if feeder["nozzle_temp"]:
                lane["nozzle_temp"] = feeder["nozzle_temp"]
        lanes["lane%d" % (index + 1)] = lane
    return {"result": {"namespace": "lane_data", "value": lanes}}


UI_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Filament Match mock printer</title>
<style>
:root { color-scheme: light dark; --line: #aaa6; --soft: #8881; }
* { box-sizing: border-box; }
body { margin: 0; padding: 18px 22px 30px; font: 13px/1.4 system-ui, sans-serif; }
h1 { font-size: 18px; margin: 0 0 4px; }
p { margin: 4px 0 10px; color: #888; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 5px; text-align: left; }
th { font-size: 11px; color: #888; font-weight: 600; }
input[type=text], input[type=number], select {
  font: inherit; color: inherit; background: transparent; border: 1px solid var(--line);
  border-radius: 5px; padding: 4px 6px; width: 100%; min-width: 0; }
input[type=checkbox] { accent-color: #009688; }
input[type=text].color { width: 92px; }
input[type=number] { width: 78px; }
button { font: inherit; border: 1px solid #009688; background: #009688; color: #fff;
         border-radius: 6px; padding: 6px 11px; cursor: pointer; }
button.quiet { background: transparent; color: inherit; border-color: var(--line); }
button:disabled { opacity: .4; cursor: default; }
.bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 10px 0; }
.note { color: #888; font-size: 12px; }
#status { min-height: 18px; color: #1b7f4b; }
#status.err { color: #c0392b; }
tr.off { opacity: .55; }
</style></head><body>
<h1>Filament Match mock printer</h1>
<p>Changes apply to the next <b>Scan</b> in Filament Match. Set the selected Orca
printer preset's print host to <b id="host"></b>.</p>
<div class="bar">
  <label>Mode <select id="mode"><option value="both">both</option>
  <option value="snapmaker">Snapmaker only</option>
  <option value="moonraker">Moonraker only</option></select></label>
  <button id="add" class="quiet">Add feeder</button>
  <button id="remove" class="quiet">Remove last</button>
  <button id="save">Save</button>
  <button id="reset" class="quiet">Reset defaults</button>
</div>
<div id="status"></div>
<table><thead><tr>
  <th>#</th><th>Present</th><th>Vendor</th><th>Material</th><th>Subtype</th>
  <th>Product</th><th>Color</th><th>Spool ID</th><th>Filament ID</th>
  <th>Bed °C</th><th>Nozzle °C</th>
</tr></thead><tbody id="feeders"></tbody></table>
<p class="note">Snapmaker payload: <code>/printer/objects/query?print_task_config&amp;filament_detect</code><br>
Moonraker payload: <code>/server/database/item?namespace=lane_data</code></p>
<script>
'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = { mode: 'both', printer: 'Mock Snapmaker U1', feeders: [] };

function field(index, key, value, type, extra) {
  return '<input type="' + (type || 'text') + '" data-index="' + index +
    '" data-key="' + key + '" value="' + esc(value ?? '') + '"' + (extra || '') + '>';
}
function render() {
  $('mode').value = state.mode || 'both';
  $('feeders').innerHTML = state.feeders.map((f, i) =>
    '<tr class="' + (f.present ? '' : 'off') + '"><td>' + (i + 1) + '</td>' +
    '<td><input type="checkbox" data-index="' + i + '" data-key="present"' +
      (f.present ? ' checked' : '') + '></td>' +
    '<td>' + field(i, 'vendor', f.vendor) + '</td>' +
    '<td>' + field(i, 'material', f.material) + '</td>' +
    '<td>' + field(i, 'subtype', f.subtype) + '</td>' +
    '<td>' + field(i, 'product', f.product) + '</td>' +
    '<td>' + field(i, 'color', f.color, 'text', ' class="color"') + '</td>' +
    '<td>' + field(i, 'spool_id', f.spool_id, 'number', ' min="0" step="1"') + '</td>' +
    '<td>' + field(i, 'filament_id', f.filament_id) + '</td>' +
    '<td>' + field(i, 'bed_temp', f.bed_temp, 'number', ' min="0" step="1"') + '</td>' +
    '<td>' + field(i, 'nozzle_temp', f.nozzle_temp, 'number', ' min="0" step="1"') + '</td>' +
    '</tr>').join('');
  $('remove').disabled = state.feeders.length <= 1;
}
function collect() {
  state.mode = $('mode').value;
  document.querySelectorAll('[data-index][data-key]').forEach(node => {
    const index = Number(node.dataset.index);
    const key = node.dataset.key;
    if (!state.feeders[index]) return;
    state.feeders[index][key] = node.type === 'checkbox' ? node.checked : node.value;
  });
  return state;
}
async function load() {
  try {
    const response = await fetch('/__state', {cache: 'no-store'});
    const data = await response.json();
    state = data;
    render();
    $('status').textContent = '';
  } catch (error) {
    $('status').textContent = String(error);
    $('status').className = 'err';
  }
}
async function save() {
  collect();
  try {
    const response = await fetch('/__state', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(state)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    state = data; render();
    $('status').textContent = 'Saved. Press ↻ in Filament Match to rescan.';
    $('status').className = '';
  } catch (error) {
    $('status').textContent = String(error);
    $('status').className = 'err';
  }
}
$('add').onclick = () => {
  if (state.feeders.length >= 64) return;
  state.feeders.push({present: true, vendor: '', material: 'PLA', subtype: '',
    product: '', color: '#FFFFFF', spool_id: 0, filament_id: '',
    bed_temp: 60, nozzle_temp: 220});
  render();
};
$('remove').onclick = () => {
  if (state.feeders.length <= 1) return;
  state.feeders.pop();
  render();
};
$('save').onclick = save;
$('reset').onclick = async () => {
  const response = await fetch('/__reset', {method: 'POST'});
  state = await response.json();
  render();
  $('status').textContent = 'Reset.';
  $('status').className = '';
};
load();
$('host').textContent = location.host;
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "FilamentMatchMock/1.0"
    protocol_version = "HTTP/1.1"

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self, message="not found"):
        self._json({"error": message}, 404)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2_000_000:
            raise ValueError("missing or oversized request body")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        state = self.server.store.get()
        if path in ("/", "/index.html"):
            self._html(UI_HTML)
            return
        if path == "/__state":
            self._json(state)
            return
        if path == "/__health":
            self._json({
                "ok": True,
                "mode": state["mode"],
                "feeders": len(state["feeders"]),
                "present": sum(1 for feeder in state["feeders"] if feeder["present"]),
            })
            return
        if path == "/server/info":
            self._json({"result": {
                "klippy_connected": True,
                "klippy_state": "ready",
                "components": ["mock"],
                "moonraker_version": "mock",
                "hostname": "filament-match-mock",
            }})
            return
        if path == "/printer/info":
            self._json({"result": {"state": "ready", "hostname": "filament-match-mock"}})
            return
        if path == "/printer/objects/query":
            if state["mode"] == "moonraker":
                self._not_found("snapmaker endpoint disabled by mode")
                return
            self._json(snapmaker_payload(state))
            return
        if path == "/server/database/item":
            query = parse_qs(parsed.query)
            if query.get("namespace", [""])[0] != "lane_data":
                self._json({"result": {"namespace": "lane_data", "value": {}}})
                return
            if state["mode"] == "snapmaker":
                self._not_found("moonraker endpoint disabled by mode")
                return
            self._json(moonraker_payload(state))
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._not_found()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/__state":
            try:
                state = self.server.store.replace(self._read_json())
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, 400)
                return
            self._json(state)
            return
        if path == "/__reset":
            self._json(self.server.store.reset())
            return
        self._not_found()

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


class MockPrinterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store):
        super().__init__(address, Handler)
        self.store = store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7125)
    parser.add_argument("--config", default="", help="JSON state file to load/save")
    args = parser.parse_args()

    store = Store(args.config or None)
    server = MockPrinterServer((args.host, args.port), store)
    url = "http://%s:%d/" % (args.host, server.server_address[1])
    print("Mock printer: %s" % url)
    print("Editor:       %s" % url)
    print("State:        %s__state" % url)
    print("Set the Orca printer preset print_host to: %s" % url.rstrip("/"))
    if args.config:
        print("State file:   %s" % args.config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
