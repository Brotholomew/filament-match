# Mock printer for Filament Match

`dev/mock_printer.py` is a standalone HTTP server that mimics the two printer
sources the plugin reads:

```text
GET /printer/objects/query?print_task_config&filament_detect   (Snapmaker)
GET /server/database/item?namespace=lane_data                  (Moonraker)
```

It has its own feeder editor, so you can change the feeder count and filament
data without touching a real printer.

## Start

```sh
python3 plugin/dev/mock_printer.py --port 7125
```

Optional persistent state:

```sh
python3 plugin/dev/mock_printer.py --port 7125 --config /tmp/filament-match-mock.json
```

Open the editor:

```text
http://127.0.0.1:7125/
```

## Point Orca at the mock

The plugin reads the printer host from the active Orca printer preset, not from
a plugin URL setting.

1. In Orca, duplicate the printer preset you want to test (or temporarily edit
   the existing one).
2. Set its **print host** (`print_host`) to:

   ```text
   127.0.0.1:7125
   ```

3. Select that printer preset.
4. In Filament Match, press **↻** to rescan.

Use `127.0.0.1:7125` with the port included. A bare hostname makes the plugin
try port 80 before 7125.

To test **other printer compatibility**, select a different Orca printer preset
while leaving the mock running. The mock only supplies the filament data; Orca
still decides which presets are compatible.

## Editor

- **Add feeder / Remove last** changes the number of slots.
- **Present** controls whether a slot is loaded.
- Vendor, material, subtype, product, colour, spool ID, filament ID, bed and
  nozzle temperatures are all editable.
- **Save** applies the state; press **↻** in Filament Match to rescan.
- **Reset defaults** restores the four-slot U1-style example.

The default example mirrors a U1 with:

```text
T1 empty
T2 empty
T3 3DJake ABS, #4DB6AC, spool 7
T4 Snapmaker PLA SnapSpeed, #000000, spool 3
```

## Four-spool Spoolman test state

`dev/mock_states/four_spools.json` is a ready-made four-feeder state for a
project with exactly four filament slots:

```text
T1 3DJake ABS Pastel Pink          spool 6
T2 CC3D PETG Pink                  spool 8
T3 eSUN PETG Basic Transparent Green spool 9
T4 The Filament PLA Lite White      no Spoolman spool
```

The fourth feeder is deliberately **not** in Spoolman. Its `spool_id` is `0`, so
the plugin does not request a Spoolman spool for it; the printer-reported
vendor, material, subtype, product, colour and temperatures are used directly.

Use a copy when starting the mock, because the server writes state back to its
`--config` file:

```sh
cp plugin/dev/mock_states/four_spools.json /tmp/four_spools.json
python3 plugin/dev/mock_printer.py --port 7125 --config /tmp/four_spools.json
```

The Filament values used:

```text
Vendor:   The Filament
Material: PLA
Subtype:  Lite
Product:  PLA Lite White
Colour:   #FFFFFF
Nozzle:   210 °C   (product range 195-220 °C)
Bed:      50 °C    (product range 40-50 °C)
Spool ID: 0        (not in Spoolman)
```

The product is The Filament PLA Lite White. The Open Filament Database entry is
`The Filament PLA Lite (OFD)`, inheriting `Generic PLA @System`; its preset JSON
is cached by the plugin, so Apply works without an OFD network request.

## Modes

`both` (default) serves both endpoints. `Snapmaker only` returns 404 for the
Moonraker endpoint; `Moonraker only` returns 404 for the Snapmaker endpoint. The
plugin logs the disabled source as a warning but still uses the other one.

This is useful for checking:

- a printer that only exposes vendor/material, with no product or spool ID;
- a printer that only exposes a product through Moonraker;
- feeder counts from 1 to 64;
- a slot that is present but has blank vendor/subtype.

## API

```sh
curl http://127.0.0.1:7125/__state
curl http://127.0.0.1:7125/__health
curl -X POST http://127.0.0.1:7125/__state \
  -H 'Content-Type: application/json' \
  --data @state.json
curl -X POST http://127.0.0.1:7125/__reset
```

State shape:

```json
{
  "mode": "both",
  "printer": "Mock Snapmaker U1",
  "feeders": [
    {
      "present": true,
      "vendor": "3DJake",
      "material": "ABS",
      "subtype": "",
      "product": "ABS",
      "color": "#4DB6AC",
      "spool_id": 7,
      "filament_id": "",
      "bed_temp": 100,
      "nozzle_temp": 260
    }
  ]
}
```

`color` accepts `#RGB`, `#RRGGBB` or `#RRGGBBAA` and is stored as `#RRGGBB`.
`product` is only visible to the plugin through the Moonraker source.

## Spoolman

For a controlled test, leave **Spoolman URL** blank in Filament Match settings
so the mock printer data is used as-is. If Spoolman is configured, the plugin
will try to enrich the mock spool IDs and may add warnings when it is
unreachable.

## Notes

- The server binds to `127.0.0.1` by default. Do not expose it on a public
  interface; it has no authentication.
- It does not control a real printer and does not touch the U1 at
  `10.0.0.176`.
- Orca may still ask for network permission for `127.0.0.1`; approve it once.
- The server is a development tool, not a general Moonraker implementation.
