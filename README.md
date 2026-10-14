# Pump Health Console

An edge-AI predictive-maintenance HMI for a centrifugal water pump test rig.
An ESP32 reads the sensors and streams JSON lines to a Raspberry Pi 5. The Pi
validates each message, builds a sliding feature window, scores it for anomalies,
logs it to CSV and pushes everything to a browser dashboard over a WebSocket.

```
ESP32 ──USB serial / MQTT──▶ Pi 5: validate → 5-sample window → 12 features → scorer → status/alerts ──WebSocket──▶ browser HMI
  ▲                                     ▲                                                     └──▶ CSV log
  └ simulator / CSV replay (same JSON lines, same code path) ┘
```

The browser only reads the WebSocket. It never talks to serial, MQTT or the
simulator, so it cannot tell a simulated stream from a real one, apart from the
source badge that tells the operator.

## Quick start: plug and play

**Raspberry Pi (one-time setup, then it's automatic):**

```bash
git clone https://github.com/tzar-beep/Pump-monitor-pro.git && cd Pump-monitor-pro
./deploy/install_pi.sh --kiosk      # omit --kiosk if the Pi has no screen
```

This installs everything, gives your user serial-port access, and registers a systemd
service that starts the backend on every boot. With `--kiosk`, it also opens the dashboard
full-screen when the Pi logs in. After that, **plug the ESP32 into any USB port. That's it.**

**Windows:** double-click **`start.bat`**. The first run sets up Python packages, then the
dashboard opens in your browser.

**Manually, on any OS:**

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip ...
.venv/bin/python -m backend                    # http://localhost:8000
```

### What happens when you plug in (default `auto` source)

1. **Nothing plugged in.** The dashboard runs the simulator, with the amber
   **SIMULATED DATA** badge and *Device: searching USB…*. Every 2 s the backend lists the USB
   serial ports (CP210x, CH340, FTDI, ESP32 native USB, and so on). Bluetooth and legacy COM
   ports are skipped.
2. **ESP32 plugged in.** Each port is opened and read for up to 4 s, which allows for the ESP32
   rebooting when the port opens. The first port that sends a message passing full validation
   is locked in. The simulator stops, the history is cleared, a green banner says
   *ESP32 detected on COM5 (CP210x) — LIVE data*, and the badge turns to green **LIVE DATA**.
   This usually takes 2–5 s.
3. **Unplugged.** A red banner and the **DISCONNECTED** badge appear, and the last values are
   dimmed. The app **never falls back to simulated data** once a real device has been seen,
   because that would hide a real fault.
4. **Plugged back in.** The app reconnects on its own, even if the OS gave the board a new port name.

If a port is found but rejected, the **Controls → Data source** panel lists every port it
probed and why, for example *silent: no data (firmware flashed?)*,
*port busy: close Arduino Serial Monitor*, or *permission denied: add your user to dialout*.

### Other modes

To change mode, use the **Data source** dropdown, or start the backend with a flag:

| Mode | Command |
|---|---|
| Auto (default) | `python -m backend`. Add `--no-sim-fallback` to show nothing until hardware is found. |
| Simulator only | `python -m backend --source sim` |
| ESP32 on a fixed port | `python -m backend --source serial --serial-port /dev/ttyUSB0` (`--source serial` alone auto-detects, without the simulator) |
| ESP32 over MQTT | `PUMP_MQTT_PASSWORD=… python -m backend --source mqtt --mqtt-host 192.168.1.10 --mqtt-topic pumprig/esp32/telemetry` |
| Replay a recorded CSV | `python -m backend --source replay --replay-file data/sample_session.csv --replay-speed 4` |
| Use a trained model | add `--model models/iforest.json` |
| Log to CSV from start-up | add `--log` (it can also be switched on from the UI). Live data goes to its own file. |

Every tunable constant is in [`backend/config.py`](backend/config.py) (`CONFIG`): baselines,
feature scales, thresholds, window size, ranges, serial/MQTT settings and simulator physics.
To keep your edits out of git, put overrides in a JSON file and pass `--config config.local.json`.
It is deep-merged over `CONFIG`:

```json
{ "source": "serial", "serial": { "port": "/dev/ttyACM0" }, "logging": { "enabled": true } }
```

## The dashboard

| Section | What it shows |
|---|---|
| Top bar | Overall status (HEALTHY / WARNING / CRITICAL / DRY-RUN PROTECTION / STANDBY / STARTING), pump state, stream state and rate, time since the last valid message, clock. The **source badge is always visible**: `LIVE DATA` (green), `SIMULATED DATA` / `REPLAY DATA` (amber), `STALE n s` / `DISCONNECTED` (red). |
| KPI row | Health-index gauge, anomaly score and the six physical channels. Each tile has a value, unit, baseline delta and a 60-sample sparkline. The tile colour comes from the channel's state (the largest \|z\| among its features), not from the accent colour. |
| Pipeline | Sensors → ESP32 → Pi 5 → Isolation Forest → Health index → HMI. Each node shows its live state: channel health, link and rate, valid/invalid/dropped counts, window fill, score, alert count and CSV state. |
| Trend | Current, flow, pressure, temperature, vibration or anomaly score over a rolling 120-sample window, with WARN/CRIT threshold lines (score) or a baseline (physical channels), an endpoint marker and a hover crosshair. |
| Controls | Source selector, scenario cards with expected signatures, valve slider (0–100 % closed), pump start/stop, stream pause, CSV logging toggle with the file path, and message counters (received / valid / invalid / dropped / skipped, plus reject reasons). |
| Sensors | One row per sensor: ESP32 channel, live reading, unit, valid range, status, and the ML features it feeds. The float switch is marked as a safety interlock, not an ML input. |
| Feature vector | The 12 window features with value, healthy baseline, tolerance σ, z-score and a deviation bar. Expand the row below the table to see the scoring formula. |
| Alerts | Status transitions, float LOW/restored, link STALE/restored, source and command events. Newest first, coloured by severity, each with a text label. |

## What is simulated

When `source = sim`, `backend/simulator.py` prints exactly the JSON line the firmware
prints, and that line goes through the same parse → validate → feature → score path as real data.

* One reading per second. Every channel has realistic noise: DS18B20 temperatures are quantised
  to 0.0625 °C, and the ADXL345 output is three per-axis RMS values plus a peak.
* Operating points blend through first-order lags (about 2 s for hydraulics, 90 s for temperature,
  0.6 s coast-down when the pump stops), so changes ramp in instead of jumping.
* Scenarios (all deltas are in `CONFIG["simulator"]["scenarios"]`):

| Scenario | Signature |
|---|---|
| Normal | baseline: 1480 rpm, 2.1 A, 18.4 L/min, 2.2 bar, 38 °C, 0.12 g |
| Outlet restriction | Flow ↓ (×0.45), Pressure ↑ (+1.1 bar), Current ↑ (+0.35 A) |
| Mechanical imbalance | Vibration ↑↑ (×3.6), Current ↑, Temp ↑ |
| Overload | RPM ↓ (−180), Current ↑↑ (+1.2 A), Temp ↑ (+12 °C), Flow ↓ |
| Dry run | Float LOW → Flow 0, Current ↓, vibration ↑. The interlock trips the pump after 2 s and stays **latched** until you press *Start*. |

* The valve slider adds restriction on top of any scenario.
* Pump ON/OFF and stream pause/resume are simulator commands. On a live source, pause
  discards incoming lines and counts them as `skipped`. The pump button is disabled on live
  sources because the Pi has no actuator.

## Scoring (demo scorer, standing in for Isolation Forest)

```
z_i    = (f_i − baseline_i) / σ_i                       for the 12 features
D      = √( (1/12) · Σ clip(z_i, ±8)² )
score  = 1 − exp(−(D / 2)²)                             ∈ [0, 1]
health = round((1 − score) × 100)
```

HEALTHY is score < 0.3, WARNING is 0.3–0.6, CRITICAL is > 0.6. A new band must hold for
2 samples (debounce), and an alarm clears only once the score is 0.08 below its threshold
(deadband). Float = 0 overrides everything with DRY-RUN PROTECTION. RPM < 200 shows STANDBY.
Alarms are inhibited for 8 s after a start while the window refills.

Features are computed over the last 5 samples: RPM mean, current RMS and STD, temperature
mean and slope (°C/min, least squares), flow mean and STD, pressure mean and STD, and
vibration RMS, peak and STD. (The brief listed 11 features and asked for 12. Vibration STD is
the 12th, from the ADXL345 "RMS, peak, STD" summary.)

### Replacing the demo scorer with a trained model

The swap point is `load_scorer()` in [`backend/scorer.py`](backend/scorer.py). It is
already wired to a real Isolation Forest:

```bash
pip install -r requirements-train.txt                       # training machine only
python -m backend --log                                     # log a few minutes of NORMAL running on the rig
python tools/train_iforest.py logs/pump_*.csv --out models/iforest.json
python tools/replay_eval.py logs/pump_*.csv --model models/iforest.json   # check it offline
python -m backend --model models/iforest.json
```

`train_iforest.py` fits scikit-learn's `IsolationForest` on steady healthy windows. It exports
the trees to JSON and checks that the pure-Python inference on the Pi matches scikit-learn
(max difference about 1e-14). It then calibrates the compressed raw IF score with three
anchors so that the same 0.3 / 0.6 bands still apply. The Pi needs no numpy or sklearn.
`models/iforest.json` in this repo was trained on **simulated** data
(`data/sample_session.csv`) only to demonstrate the workflow. Retrain it on rig data.
To use another runtime (ONNX etc.), add a class with `.name`, `.kind` and
`.score(features) -> (score, extras)` and return it from `load_scorer()`.

## Hardware-side contract (ESP32 firmware ⇄ Pi)

**Transport:** USB CDC/UART, **115200 baud, 8N1**, no flow control, UTF-8.
One JSON object per line, terminated by `\n`, at most 512 bytes. Rate: **1 Hz** nominal (1–10 Hz accepted).
Lines that do not start with `{` (for example `# boot message`) are counted as `not_json` and ignored.
For MQTT, the payload is the same JSON object (one per message, or newline-separated), sent to `pumprig/esp32/telemetry`, QoS 0.

```json
{"ts":123456,"seq":42,"rpm":1482.0,"temp_c":38.06,"current_a":2.12,"flow_lpm":18.40,
 "pressure_bar":2.301,"vib_x":0.1200,"vib_y":0.0900,"vib_z":0.1500,"vib_peak":0.412,"float":1}
```

| Field | Type | Unit | Valid range | Meaning |
|---|---|---|---|---|
| `ts` | int, required | ms | ≥ 0, increasing | `millis()` at the end of the 1 s acquisition window. A jump backwards of more than 10 s is treated as a reboot. |
| `seq` | int, optional | – | increasing | Message counter, used to count dropped messages. Without it, gaps in `ts` are used. |
| `rpm` | number, required | rpm | 0–3000 | Shaft speed from the mean A3144 pulse period |
| `temp_c` | number, required | °C | −10–100 | DS18B20 motor/casing temperature |
| `current_a` | number, required | A (RMS) | 0–20 | ACS712 true RMS over the window |
| `flow_lpm` | number, required | L/min | 0–40 | YF-S201 pulse frequency / 7.5 |
| `pressure_bar` | number, required | bar (gauge) | −0.1–5.2 | Outlet pressure, 0–5 bar transducer. Values below 0 are clamped to 0. |
| `vib_x`, `vib_y`, `vib_z` | number, required | g | 0–16 | Per-axis **AC RMS** over the window, with gravity/DC removed |
| `vib_peak` | number, optional | g | 0–16 | Peak absolute deviation on any axis in the window. If missing, the Pi uses 2.8 × max axis RMS. |
| `float` | 0 / 1, required | – | 0 or 1 | Inlet level: 1 = water OK, 0 = LOW |

Messages that fail any check are rejected, not fed to the model, and counted by reason
(`missing`, `type`, `range`, `parse`, `not_json`, `ts_order`, `too_long`).
The per-sample vibration RMS is √((x² + y² + z²)/3).

**Firmware:** [`firmware/esp32_pump_node/esp32_pump_node.ino`](firmware/esp32_pump_node/esp32_pump_node.ino)
(Arduino framework, libraries OneWire and DallasTemperature). It samples the ADXL345, ACS712 and
pressure input at 400 Hz, counts flow and Hall pulses in interrupts, reads the DS18B20
without blocking, and prints one line per second. Pins:

| Sensor | ESP32 pin |
|---|---|
| ADXL345 | I²C SDA 21 / SCL 22, address 0x53 |
| DS18B20 | GPIO4, 4.7 kΩ pull-up |
| ACS712 | GPIO34 (ADC1) |
| Pressure | GPIO35 (ADC1) |
| YF-S201 | GPIO27 |
| A3144 | GPIO26, 10 kΩ pull-up |
| Float | GPIO25 to GND, `INPUT_PULLUP` |

> **Voltage conditioning is mandatory.** ESP32 GPIOs are 3.3 V and **not 5 V tolerant**.
> The ACS712 (5 V supply, 2.5 V ± sensitivity output) and the 0.5–4.5 V pressure transducer must
> go through a resistor divider (for example 10 kΩ / 20 kΩ, ×0.667) and ideally a buffer or RC filter
> before GPIO34/35. The YF-S201 output also needs a divider or level shifter. Use ADC1 pins only
> (ADC2 does not work while Wi-Fi is on). The firmware divides by the same ratio, so set
> `ACS_DIVIDER` / `P_DIVIDER` to the resistors you fit. If the pump is mains powered, the ACS712 is
> in the mains path: use a properly isolated module and enclosure, or a split-core CT instead.

## Testing

| Test | Command | What it checks |
|---|---|---|
| Scenario logic (headless) | `python -m tools.scenario_check -v` | Drives every scenario, the valve at 20/40/70 %, stop/start and dry run through the real `Processor`, and asserts the final status of each step plus the alerts |
| Plug-and-play, any OS | `python tools/test_autodetect.py` | Starts in auto mode with nothing attached, then plugs in a fake ESP32, unplugs it and plugs it back in. Asserts: simulator while waiting, scan reasons listed, automatic switch to LIVE (about 2 s), DISCONNECTED on unplug with no fallback to simulated data, banners, and automatic reconnect |
| Live path, any OS | `python tools/test_e2e.py` | Fake ESP32 on TCP → pyserial `socket://` URL → the real `SerialSource` → WebSocket client. Injects bad lines, sequence gaps and a 9 s stall, and asserts rejection counts, LIVE → STALE → reconnect → LIVE, the CSV log, and replay of that log |
| Live path over a real pty (Pi/Linux) | `sudo apt install socat && ./tools/test_live_path.sh` | `socat` creates a virtual serial pair. `fake_esp32.py` writes to `/tmp/ttyESP32` and the backend reads `/tmp/ttyPI`, as it would read `/dev/ttyUSB0`. Extra flags pass through, e.g. `--scenario imbalance --stall-after 30` |
| Offline replay of logged data | `python tools/replay_eval.py data/sample_session.csv [--model models/iforest.json]` | Recomputes every window with the chosen scorer and prints the status mix, agreement with the logged status, and alerts |
| Interactive replay | `python -m backend --source replay` | Streams a CSV through the live pipeline at the original timing |

On Windows without a Pi, the `socket://` test uses the same serial code. For a real virtual
COM pair, install com0com and run `fake_esp32.py --port COM6` with `--serial-port COM7`.

`data/sample_session.csv` (24 min, 1440 rows) is regenerated with `python tools/make_sample_csv.py`.

## Assumptions to calibrate on the real hardware

1. **Sample rate**: 1 JSON line per second. The window is 5 samples (5 s). The temperature slope
   is a least-squares fit over those 5 s, which is noisy. Its tolerance (1.5 °C/min) reflects that.
2. **Baselines and tolerances** (`CONFIG["features"]`, `mean` / `scale`) are the values from the brief,
   with tolerances I chose. On the rig, log 10+ minutes of normal running and set `mean` to the
   per-feature mean and `scale` to about 3 × its standard deviation. The demo scorer only means
   something once these are right.
3. **ACS712**: 20 A variant, 100 mV/A, zero offset measured at boot **with the pump off**, divider ×2/3.
   True RMS over 1 s at 400 Hz, which suits a 50 Hz AC motor (1480 rpm suggests a 4-pole induction
   motor) or a DC motor.
4. **Pressure transducer**: ratiometric 0.5–4.5 V = 0–5 bar on a 5 V supply, divider ×2/3. Check the datasheet.
5. **YF-S201**: F = 7.5 × Q (L/min) is nominal. Real units vary by ±10 %, so calibrate with a bucket and stopwatch.
6. **Hall sensor**: one magnet per revolution, RPM from the mean edge period, 2 ms debounce.
   The reading drops to 0 if there is no edge for 0.5 s.
7. **ADXL345**: ±16 g full-resolution mode (3.9 mg/LSB), 400 Hz ODR. `vib_x/y/z` are AC RMS per axis.
   The values depend strongly on mounting position. The brief's 0.12 g baseline is taken as the combined RMS.
8. **Float switch**: closed = water present (reads LOW with `INPUT_PULLUP` and is reported as `float:1`).
   Invert in firmware if your switch is normally open the other way. The real interlock should cut
   pump power in hardware, through a relay or contactor wired to the float. The Pi only displays it.
9. **Auto-detect**: any USB serial port is probed at 115200 baud. The board is accepted only if a
   line passes full validation, so other USB serial devices are opened briefly but never used.
   If you have other USB-serial gear on the Pi that must not be opened, set a fixed `serial.port`.
10. **Standby detection**: RPM < 200 means the pump is not running. The Pi has no pump-command input on a live source.
11. **Outlet restriction signature**: the brief specifies Current ↑. On a radial-flow centrifugal pump,
    throttling the discharge usually **reduces** shaft power and current. The simulator follows the brief
    (`valve.cur_k`, `blockage.current_add`). Flip the sign if the rig shows the opposite.
12. **Thresholds**: stale after 3 s without a valid message, the serial port is reopened after 6 s of silence,
    startup inhibit is 8 s, debounce 2 samples, deadband 0.08.
13. **Isolation Forest calibration**: `CRIT_FACTOR = 1.8` in `train_iforest.py` was chosen on simulated
    faults. Re-check it with `replay_eval.py` once real fault runs have been logged.

## Layout

```
backend/   config.py (all constants) · schema.py (validation) · simulator.py · features.py
           scorer.py (demo + Isolation Forest, load_scorer hook) · processor.py (window/score/status/alerts)
           sources.py (auto / sim / serial / mqtt / replay) · csvlog.py · server.py (FastAPI + WebSocket) · __main__.py
web/       index.html: the HMI. Plain HTML/CSS/JS, no build step, hand-drawn SVG charts
firmware/  esp32_pump_node/esp32_pump_node.ino
tools/     scenario_check.py · test_autodetect.py · test_e2e.py · test_live_path.sh · fake_esp32.py
           replay_eval.py · train_iforest.py · make_sample_csv.py
deploy/    install_pi.sh (Pi setup + boot service + kiosk) · pump-console.service · kiosk.sh
start.bat  Windows one-click launcher
data/      sample_session.csv  (simulated session for replay/training demos)
models/    iforest.json        (demo model trained on simulated data)
logs/      CSV session logs (git-ignored)
```

The only external request is the IBM Plex font from Google Fonts. Without internet access
(for example a Pi on an isolated network), the page falls back to system fonts.
