# OrcaSlicer 2.5.x printer filament data: exact paths

Reference for Filament Match. Source reviewed from OrcaSlicer `main`:

- `src/slic3r/Utils/SnapmakerPrinterAgent.cpp`
- `src/slic3r/Utils/MoonrakerPrinterAgent.cpp`
- `src/slic3r/Utils/MoonrakerPrinterAgent.hpp`
- `src/slic3r/Utils/CrealityCfs.cpp`
- `src/slic3r/plugin/host/PluginHostPresets.cpp`

This documents what Orca's native **Synchronize filament list from AMS** actually reads. It is the authoritative schema for the plugin until Orca exposes a read-only Python API.

## Why the plugin currently needs HTTP

Orca's native sync runs in C++ and fills `DevFilaSystem` / `AmsTrayData`. The Python plugin API exposes only:

- `PresetBundle.current_filament_preset_names()`
- `PresetBundle.current_filament_presets()`
- `PresetBundle.full_config_value("filament_ids")`

It does **not** expose raw tray data, a sync trigger, or a sync-completion event. The plugin therefore has to perform the same HTTP requests if it wants vendor/product/colour/spool data for matching.

The `AmsTrayData` shape Orca builds internally is:

```text
slot_index      0-based slot index
has_filament    bool
tray_type       material type, e.g. PLA, PETG, PLA-CF
tray_color      raw colour string
tray_colors     optional multi-colour list
colors_gradient optional
tray_info_idx   resolved Orca filament_id, optional
bed_temp        optional
nozzle_temp     optional
```

## Base URL and port

Orca uses:

```cpp
device_info.base_url = normalize_base_url(use_ssl, dev_ip, port);
```

and:

```cpp
value = use_ssl ? "https://" : "http://";
value += host;
value += port.empty() ? "" : (":" + port);
```

There is **no built-in 80/7125 probing** in the native agents. Use the configured `print_host` and `printhost_port` from the printer preset. If no port is configured, do not blindly probe two ports; report the missing port or use the one Orca has already resolved.

## Snapmaker

Function: `SnapmakerPrinterAgent::fetch_filament_info`

```http
GET {base_url}/printer/objects/query?print_task_config&filament_detect
X-Api-Key: {api_key}   (if configured)
```

Expected JSON:

```json
{
  "result": {
    "status": {
      "print_task_config": {
        "filament_exist": [true, true, false, false],
        "filament_type": ["PLA", "PETG", "", ""],
        "filament_sub_type": ["", "HF", "", ""],
        "filament_color_rgba": ["RRGGBBAA", "RRGGBBAA", "", ""],
        "filament_vendor": ["Snapmaker", "CC3D", "", ""]
      },
      "filament_detect": {
        "info": [
          {"BED_TEMP": 60, "HOTEND_MAX_TEMP": 230},
          {"BED_TEMP": 70, "HOTEND_MAX_TEMP": 250}
        ]
      }
    }
  }
}
```

Extraction rules from the native agent:

- `slot_count = len(filament_exist)`. If it is empty, no slots are reported.
- Slot `i` is 0-based.
- `has_filament = filament_exist[i]`.
- `tray_type = combine_filament_type(filament_type[i], filament_sub_type[i])`.
  - Empty subtype or `NONE` → base type.
  - `CF`/`GF` subtype → `BASE-CF` / `BASE-GF` unless already suffixed.
  - Other subtypes are finishes/brands, not material types; the native agent keeps the base type.
- `tray_color = filament_color_rgba[i]` (raw; native `normalize_color_value` converts to `RRGGBBAA`).
- `tray_vendor = filament_vendor[i]` is used only for native preset selection.
- `BED_TEMP` and `HOTEND_MAX_TEMP` come from `filament_detect.info[i]` when present.
- The native agent does **not** read `filament_spool_id`, `filament_id`, tag UID, product, or subtype as separate tray fields. If those appear in `print_task_config`, they are vendor extensions; do not assume them.

## Moonraker

Function: `MoonrakerPrinterAgent::fetch_filament_info`

Order:

1. `fetch_moonraker_filament_data` (`lane_data`)
2. `fetch_hh_filament_info` (`mmu`)
3. `fetch_creality_cfs_data` (`box`)

The first successful source wins. It does not merge them.

### 1. Moonraker `lane_data`

```http
GET {base_url}/server/database/item?namespace=lane_data
X-Api-Key: {api_key}   (if configured)
```

Expected JSON:

```json
{
  "result": {
    "namespace": "lane_data",
    "value": {
      "lane1": {
        "lane": "0",
        "material": "PLA",
        "color": "#RRGGBB",
        "bed_temp": 60,
        "nozzle_temp": 210,
        "filament_id": "optional-string"
      },
      "lane2": {
        "lane": "1",
        "material": "PETG",
        "color": "#RRGGBB",
        "bed_temp": 70,
        "nozzle_temp": 240
      }
    }
  }
}
```

Extraction rules:

- Iterate `result.value` object values; the **object key is irrelevant**.
- Read `lane` as a **string**. Native `safe_json_string(lane_obj, "lane")` returns empty for a JSON number. Empty/invalid/non-numeric/negative `lane` → skip the lane.
- `slot_index = int(lane)` (0-based).
- `tray_type = material`.
- `tray_color = color`.
- `tray.bed_temp = bed_temp`.
- `tray.nozzle_temp = nozzle_temp`.
- `has_filament = not material.empty()`.
- `tray_info_idx = filament_id` only when it is a JSON string. Numeric `filament_id` is ignored by native.
- Native does **not** read `vendor`, `subtype`, `product`, `spool_id`, `tag_uid`, `uid`, or `card_uid` from `lane_data`. If present, they are producer extensions. Use them only as optional enrichment, not as the base schema.
- Native does not use the key name (`lane1`, `0`, etc.) for slot numbering.

### 2. Happy Hare `mmu`

```http
GET {base_url}/printer/objects/query?mmu
X-Api-Key: {api_key}   (if configured)
```

Expected JSON:

```json
{
  "result": {
    "status": {
      "mmu": {
        "num_gates": 4,
        "gate_status": [1, 1, 0, -1],
        "gate_material": ["PLA", "PETG", "", ""],
        "gate_color": ["#RRGGBB", "#RRGGBB", "", ""],
        "gate_temperature": [210, 240, 0, 0]
      }
    }
  }
}
```

Extraction rules:

- If `result.status.mmu` is missing or empty, HH is not installed.
- `num_gates` must be a number and > 0.
- Arrays must be arrays; missing arrays are treated as empty.
- For each gate index:
  - `gate_status`: `-1` unknown, `0` empty, `1` or `2` available. Skip `<= 0`.
  - `material = gate_material[i]`; skip empty.
  - `color = gate_color[i]`.
  - `nozzle_temp = gate_temperature[i]`.
  - `slot_index = gate index` (0-based).
  - `has_filament = true`.
  - `bed_temp = 0` (HH arrays do not provide it).
- Native does not read vendor, product, spool ID or tag UID here.

### 3. Creality CFS `box`

```http
GET {base_url}/printer/objects/query?box
X-Api-Key: {api_key}   (if configured)
```

Expected JSON:

```json
{
  "result": {
    "status": {
      "box": {
        "T1": {
          "state": "connect",
          "material_type": ["PLA", "PETG", "PLA", "PETG"],
          "color_value": ["RRGGBB", "RRGGBB", "RRGGBB", "RRGGBB"]
        },
        "T2": {"state": "connect", "...": "..."}
      },
      "same_material": [
        ["material_id", "color", [slots], "PLA"]
      ]
    }
  }
}
```

Extraction rules:

- Inspect `box.T1` … `box.T4`; require `state == "connect"`.
- Each tray has arrays `material_type` and `color_value` with four entries.
- `material_type` entries are material IDs; map with Orca's table first, then `same_material[0] -> same_material[3]` as fallback.
- `color_value[i]` is the colour. Creality may prefix six-digit RGB with a leading zero; native strips one leading zero when length is 7.
- Slot index = `(tray_idx - 1) * 4 + slot_i` (0-based).
- Native does not read vendor, product, spool ID or tag UID here.

## What native sync does with the data

`build_ams_payload()` converts `AmsTrayData` to Bambu-compatible AMS JSON:

```text
ams[].id, ams[].info, ams[].display_name
tray.id, tray.tag_uid ("0000000000000000")
tray.tray_info_idx, tray.tray_type, tray.tray_color (RRGGBBAA)
tray.cols, tray.ctype
tray.bed_temp, tray.nozzle_temp_max
tray_slot_placeholder when no preset resolves
ams_exist_bits, tray_exist_bits
```

Then `DevFilaSystemParser::ParseV1_0` populates Orca's `DevFilaSystem`. The plugin does not need to reproduce this BBL mapping; it only needs the raw `AmsTrayData` fields.

## Implementation implications

1. Do not invent slot numbering from object keys; follow native field semantics.
2. Do not probe port 80 and 7125 blindly; use Orca's configured host/port.
3. Query only the source Orca would query for the detected agent:
   - Snapmaker → `print_task_config&filament_detect`
   - Moonraker → `lane_data`, then `mmu`, then `box`
4. Keep native fields authoritative; treat vendor/product/spool/tag extras as optional producer extensions.
5. Add fixtures that mirror these exact shapes, including:
   - Snapmaker arrays with empty slots;
   - `lane_data` with string `lane` and numeric `lane` (numeric must skip natively);
   - HH `gate_status` 1/2/0/-1;
   - CFS `box` with T1/T2, `state` connect, `same_material` fallback.
6. Add the upstream Orca API proposal for raw tray access; until then HTTP is unavoidable for OFD matching.
