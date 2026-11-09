# AC43 Simulator

A self-contained Raspberry Pi web app that drives a Verkada AC43's door inputs
through an I2C relay board, with a live visual of all 4 doors.

- Visual AC43 panel: AUX, Doors 1–4, FAI
- Each door: animated open/closed door, lock icon, REX1 / REX2 / Open-Close (DPI)
- Click the door itself to toggle DPI
- REX: click = pulse (`rex_pulse_ms`); **press and hold** = held until you let go
  (mouse, touch, or Space/Enter). Released after `rex_max_hold_s` (default 30 s)
  if the browser never lets go
- REX **Latch** (toggle per door): click REX once to turn it on, click again to
  release. No time limit by default (`rex_max_latch_s = 0`); **All relays off**
  also releases it
- DPI = latching
- AUX IN 1/2 are driven by relays; AUX OUT 1/2 are **read** from the AC43 by the MCP23017
- Optional MCP23017 reads the AC43 lock relays → "✓ Confirmed by lock sensor"
- Live updates to every open browser (Server-Sent Events)
- All relays released on startup and on shutdown

No build step; Python standard library + `smbus2`.

## Hardware

| Relay ch | Point     | | Relay ch | Point     |
|---------:|-----------|-|---------:|-----------|
| 0  | Door 1 DPI  | | 8  | Door 3 REX2 |
| 1  | Door 1 REX1 | | 9  | Door 4 DPI  |
| 2  | Door 1 REX2 | | 10 | Door 4 REX1 |
| 3  | Door 2 DPI  | | 11 | Door 4 REX2 |
| 4  | Door 2 REX1 | | 12 | AUX IN 1    |
| 5  | Door 2 REX2 | | 13 | AUX IN 2    |
| 6  | Door 3 DPI  | | 14 | *free*      |
| 7  | Door 3 REX1 | | 15 | *free*      |

Channels 0–7 = relays A0–A7, 8–15 = B0–B7 on the XL9535-K16V5.

**MCP23017 (`sense`) pins**

| Pin | Reads | | Pin | Reads |
|---|---|-|---|---|
| GPA0 | Door 1 lock relay | | GPA4 | AC43 FAI output NO |
| GPA1 | Door 2 lock relay | | GPA5 | AC43 FAI output NC |
| GPA2 | Door 3 lock relay | | GPA6 | AC43 AUX OUT 1 |
| GPA3 | Door 4 lock relay | | GPA7 | AC43 AUX OUT 2 |

For every sensed relay, wire COM → MCP23017 GND and NO → the pin. Internal
pull-ups are on, so LOW = relay on. Check that each AC43 output is dry
(0 V on COM) before connecting.

**DPI wiring:** with `dpi_invert = false`, relay ON = door OPEN. Wire the relay
**COM/NC** across the AC43 DPI/GND terminals so a released relay reads "closed".

**Lock confirmation (MCP23017):** pins GPA0–GPA3 = doors 1–4. Wire each AC43
lock relay COM to GND and NO to the pin; internal pull-ups are enabled, so a
LOW pin = unlocked (`lock_active_low = true`). Set `enabled = true` on the
`sense` board.

### Fire Alarm Input (second XL9535 board, `fai`)

| FAI ch | Relay contacts | Wired across | Energised = |
|---:|---|---|---|
| 0 | COM / NO | FAI+ ↔ FAIP | jumper fitted → **NC mode** |
| 1 | COM / NO | FAI+ ↔ FAI− | contact closes → alarm (NO mode) |
| 2 | COM / NC | FAI− ↔ FAIP | loop breaks → alarm (NC mode) |
| 3 | COM / NC | L ↔ L | latch loop opens |

The app interlocks these:
- only the trigger for the selected mode fires
- the mode can't change while the alarm is active
- relays switch in an order that never presents a false alarm

In NO mode, ch 2 is held energised so FAIP isn't tied to FAI−.

**Confirmation:** the AC43's own FAI output relay (NC/COM/NO beside the FAI
input) goes to the MCP23017 as follows:
- COM → GND
- NO → GPA4
- NC → GPA5

The UI then shows ✓ Triggered or Normal. If the two inputs disagree, it shows
**Wiring fault**. Check that the output is dry (0 V on COM) before connecting.

**Power-off state:** with the default wiring, a powered-down Pi leaves the AC43
with no jumper and FAI−↔FAIP closed. If you'd rather power-off be a clean
"NC mode, normal", wire ch 0 on COM/NC and set `mode_invert = true`.

**I2C address clash:** both boards default to `0x20`. Change one with its
address jumpers and update `address` in `config.toml`. Check with
`i2cdetect -y 1`.

## Install on the Pi

```bash
cd ac43-simulator
sudo ./install.sh
```

Enables I2C, creates a venv, installs a `systemd` service that starts on boot,
and prints the URL (default port 8080).

## Run manually / develop on a laptop

```bash
python3 run.py            # real I2C if available, otherwise mock
python3 run.py --mock     # all boards simulated (incl. the lock sensor)
python3 -m unittest discover -s tests -v
```

The header LEDs show **Link** (browser ↔ Pi) and one per board:
green = live, amber = simulated, red = fault, grey = disabled.

## Adding things

Everything is in `config.toml`:

- **New relay/trigger:** add an `[[outputs]]` entry (`board`, `channel`,
  `group`, `mode = "toggle" | "pulse"`). `group = "aux"` / `"fai"` place it in
  those sections; any other group gets its own panel section automatically.
- **New board:** add a `[[boards]]` entry (`xl9535` or `mcp23017`).
- **FAI:** configured under `[fai]`; set `enabled = false` to hide it.
- **Auto-close doors:** `auto_close_s` under `[door_defaults]` or per door.

## API

| Method | Path | Body |
|---|---|---|
| GET  | `/api/state` · `/api/layout` · `/api/events` (SSE) | |
| POST | `/api/doors/<n>/open` · `/close` | |
| POST | `/api/doors/<n>/rex/<1\|2>` | `{"action": "pulse\|hold\|latch\|release"}` (default pulse) |
| POST | `/api/outputs/<id>` | `{"action": "on\|off\|toggle\|pulse\|hold\|latch\|release"}` |
| POST | `/api/fai/trigger` · `/api/fai/clear` | |
| POST | `/api/fai/mode` | `{"mode": "no\|nc"}` |
| POST | `/api/fai/latch` | `{"open": true}` |
| POST | `/api/all-off` (also clears the fire alarm) | |
| POST | `/api/sim/inputs/<id>` | `{"active": true}` (mock boards only) |

No authentication — keep it on a trusted network.
