"""
Single source of truth for every tunable constant in the Pi-side pipeline.

Override at runtime without editing this file:
  * a JSON file passed with --config (deep-merged over CONFIG), e.g. config.local.json
  * CLI flags: --source, --serial-port, --baud, --mqtt-host, --replay-file, --port
  * env vars: PUMP_SOURCE, PUMP_SERIAL_PORT, PUMP_MQTT_HOST, PUMP_MQTT_USER, PUMP_MQTT_PASSWORD

The browser receives the relevant subset of this object in the WebSocket "hello"
message, so thresholds and baselines are never duplicated in the frontend.
"""

CONFIG = {
    # ------------------------------------------------------------------ server
    "server": {"host": "0.0.0.0", "port": 8000},

    # ------------------------------------------------------------------ source
    # One of: "auto" | "sim" | "serial" | "mqtt" | "replay". Switchable from the UI too.
    # "auto" = plug and play: simulator until an ESP32 is found on USB, then LIVE.
    "source": "auto",

    "auto": {
        # Show simulated data (clearly badged) until the first ESP32 is found. Once a real
        # device has been seen, losing it shows DISCONNECTED - never a silent fallback.
        "fallback_sim": True,
    },

    "serial": {
        # "auto" = scan USB serial ports and lock onto the one sending valid pump JSON.
        # Or a fixed port - Linux/Pi: /dev/ttyUSB0 or /dev/ttyACM0. Windows: COM5.
        # Any pyserial URL works too, e.g. "socket://127.0.0.1:7777" (used by tools/test_e2e.py).
        "port": "auto",
        "probe_timeout_s": 4.0,       # per port: ESP32 reboots on open, then sends within ~2 s
        "extra_candidates": [],       # extra ports/URLs the auto-scan also probes (tests, odd adapters)
        "baud": 115200,
        "read_timeout_s": 1.0,
        "reconnect_delay_s": 2.0,
        "max_line_bytes": 512,
    },

    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "topic": "pumprig/esp32/telemetry",
        "client_id": "pump-pi5-console",
        "username": None,
        "password": None,        # prefer env PUMP_MQTT_PASSWORD
        "tls": False,
        "keepalive_s": 30,
    },

    "replay": {
        "file": "data/sample_session.csv",
        "speed": 1.0,            # 1.0 = original timing from the 'ts' column
        "loop": True,
    },

    # ------------------------------------------------------------------ link
    "link": {
        "expected_interval_s": 1.0,   # ESP32 publish period
        "stale_after_s": 3.0,         # no valid message for this long -> STALE
        "ts_reset_back_ms": 10_000,   # ts going back more than this = ESP32 reboot, accept
    },

    # ------------------------------------------------------------------ CSV log
    "logging": {"enabled": False, "dir": "logs"},

    # ------------------------------------------------------------------ validation
    # Physical plausibility ranges [min, max] for each incoming field.
    "ranges": {
        "rpm":          [0, 3000],
        "temp_c":       [-10, 100],
        "current_a":    [0, 20],       # ACS712-20A
        "flow_lpm":     [0, 40],       # YF-S201 rated 1-30 L/min
        "pressure_bar": [-0.1, 5.2],   # 0-5 bar transducer, small tolerance for offset noise
        "vib_x":        [0, 16],       # ADXL345 +/-16 g full scale, per-axis AC RMS
        "vib_y":        [0, 16],
        "vib_z":        [0, 16],
        "vib_peak":     [0, 16],
    },
    "required_fields": ["ts", "rpm", "temp_c", "current_a", "flow_lpm",
                        "pressure_bar", "vib_x", "vib_y", "vib_z", "float"],
    "optional_fields": ["seq", "vib_peak"],
    # Used only when the firmware does not send vib_peak: peak ~= crest * max axis RMS.
    "vib_peak_crest_fallback": 2.8,

    # ------------------------------------------------------------------ healthy baseline (raw)
    "baseline": {
        "rpm": 1480, "current_a": 2.1, "flow_lpm": 18.4, "pressure_bar": 2.2,
        "temp_c": 38.0, "vib_rms": 0.12, "ambient_c": 27.0,
    },

    # ------------------------------------------------------------------ features
    "window": 5,   # samples in the sliding window (5 s at 1 Hz)
    # mean = healthy baseline of the feature, scale = tolerance used to standardise it.
    "features": [
        {"key": "rpm_mean",      "label": "RPM mean",          "unit": "rpm",   "mean": 1480,  "scale": 25,    "dp": 0, "sensor": "hall"},
        {"key": "current_rms",   "label": "Current RMS",       "unit": "A",     "mean": 2.10,  "scale": 0.15,  "dp": 2, "sensor": "current"},
        {"key": "current_std",   "label": "Current STD",       "unit": "A",     "mean": 0.03,  "scale": 0.04,  "dp": 3, "sensor": "current"},
        {"key": "temp_mean",     "label": "Temp mean",         "unit": "°C",    "mean": 38.0,  "scale": 2.5,   "dp": 1, "sensor": "temp"},
        {"key": "temp_slope",    "label": "Temp slope",        "unit": "°C/min","mean": 0.0,   "scale": 1.5,   "dp": 2, "sensor": "temp"},
        {"key": "flow_mean",     "label": "Flow mean",         "unit": "L/min", "mean": 18.4,  "scale": 1.3,   "dp": 2, "sensor": "flow"},
        {"key": "flow_std",      "label": "Flow STD",          "unit": "L/min", "mean": 0.22,  "scale": 0.30,  "dp": 3, "sensor": "flow"},
        {"key": "pressure_mean", "label": "Pressure mean",     "unit": "bar",   "mean": 2.20,  "scale": 0.16,  "dp": 3, "sensor": "pressure"},
        {"key": "pressure_std",  "label": "Pressure STD",      "unit": "bar",   "mean": 0.02,  "scale": 0.04,  "dp": 3, "sensor": "pressure"},
        {"key": "vib_rms",       "label": "Vibration RMS",     "unit": "g",     "mean": 0.120, "scale": 0.030, "dp": 3, "sensor": "vib"},
        {"key": "vib_peak",      "label": "Vibration peak",    "unit": "g",     "mean": 0.36,  "scale": 0.10,  "dp": 3, "sensor": "vib"},
        {"key": "vib_std",       "label": "Vibration STD",     "unit": "g",     "mean": 0.006, "scale": 0.015, "dp": 4, "sensor": "vib"},
    ],

    # ------------------------------------------------------------------ scorer
    "scorer": {
        # Demo scorer: D = sqrt(mean(clip(z, ±z_clip)^2)); score = 1 - exp(-(D / d0)^2)
        "d0": 2.0,
        "z_clip": 8.0,
        # Path to a trained Isolation Forest exported by tools/train_iforest.py.
        # None (or a missing file) -> the demo scorer is used.
        "model_path": None,
    },

    # ------------------------------------------------------------------ status logic
    "status": {
        "warn": 0.30,              # score >= warn -> WARNING
        "crit": 0.60,              # score > crit  -> CRITICAL
        "debounce_samples": 2,     # a new band must persist this many samples before it is latched
        "deadband": 0.08,          # de-escalate only once score is this far below the band threshold
        "standby_rpm": 200,        # rpm below this = pump not running -> STANDBY
        "startup_inhibit_s": 8,    # alarm inhibit after a start, while the window refills
        "channel_z_warn": 2.0,     # per-sensor tile colouring from |z| of its primary feature
        "channel_z_crit": 4.0,
    },

    # ------------------------------------------------------------------ engineering values
    "engineering": {
        "supply_v": 230.0,            # CAL: motor supply voltage (single-phase AC assumed)
        "power_factor": 0.80,         # CAL: motor power factor at the duty point
        "motor_poles": 4,             # 4-pole induction motor on 50 Hz -> 1500 rpm synchronous
        "line_hz": 50.0,
        "density_kg_m3": 998.0,
        # Pump characteristic at n0: H(Q) = h0 - k*Q^2 [bar, L/min], scaled by (n/n0)^2.
        # CAL: fit h0 (shut-off pressure) and k from two measured points on the rig.
        "pump_curve": {"h0_bar": 3.75, "k_bar_per_lpm2": 0.00458, "n0_rpm": 1480, "q_max_lpm": 26.0},
    },

    # ------------------------------------------------------------------ fault diagnosis
    # Rule-based: cosine similarity between the feature z-vector and each signature.
    "diagnosis": {
        "min_similarity": 0.55,
        "unclassified_action": "Compare the feature table with the baseline; log the run for model retraining.",
        "faults": {
            "restriction": {
                "label": "Outlet restriction / blockage",
                "signature": {"flow_mean": -1.0, "pressure_mean": 1.0, "current_rms": 0.4},
                "action": "Check discharge valve position, outlet line and strainer for blockage.",
            },
            "imbalance": {
                "label": "Mechanical imbalance / misalignment",
                "signature": {"vib_rms": 1.0, "vib_peak": 0.9, "vib_std": 0.3, "current_rms": 0.15},
                "action": "Inspect impeller for debris or damage; check coupling alignment and mounting bolts.",
            },
            "overload": {
                "label": "Motor overload / mechanical binding",
                "signature": {"rpm_mean": -1.0, "current_rms": 1.0, "temp_slope": 0.5, "temp_mean": 0.4,
                              "flow_mean": -0.3},
                "action": "Check bearings and shaft for binding, supply voltage and load; stop if temperature keeps rising.",
            },
            "cavitation": {
                "label": "Loss of prime / cavitation",
                "signature": {"flow_mean": -1.0, "pressure_mean": -1.0, "current_rms": -0.6,
                              "vib_rms": 0.5, "vib_peak": 0.6},
                "action": "Check inlet level, suction line for air leaks or blockage, and priming.",
            },
            "dry_run": {
                "label": "Dry run (inlet level LOW)",
                "action": "Refill the sump. Interlock is latched: restart the pump manually once the level is restored.",
            },
        },
    },

    # ------------------------------------------------------------------ alarms
    "alarms": {
        "channel_deadband_z": 1.0,    # channel deviation alarm clears at |z| < channel_z_crit - this
    },

    "alerts_keep": 200,
    "history_keep": 120,          # frames replayed to a newly connected browser

    # ------------------------------------------------------------------ simulator
    "simulator": {
        "rate_hz": 1.0,
        "interlock_delay_s": 2.0,   # dry-run: float LOW -> pump tripped after this delay
        "tau_s": {"rpm": 2.0, "current_a": 1.8, "flow_lpm": 2.5, "pressure_bar": 2.0,
                  "vib": 2.0, "temp_running": 90.0, "temp_stopped": 300.0,
                  "coast_down": 0.6},   # water-loaded impeller stops in ~1-2 s
        "noise": {"rpm": 4.0, "current_a": 0.03, "flow_lpm": 0.22, "pressure_bar": 0.02,
                  "temp_c": 0.03, "vib_axis_rel": 0.05, "vib_peak_rel": 0.08},
        "temp_quantum_c": 0.0625,   # DS18B20 12-bit resolution
        "vib_axis_ratio": [1.0, 0.75, 1.25],   # x, y, z share of combined RMS
        "vib_crest": 3.0,
        "vib_off_g": 0.004,         # residual floor noise with pump stopped
        # Valve: v in [0,1] closed. flow *= 1 - flow_k*v^exp; the pressure then moves along the
        # pump curve (engineering.pump_curve): dP = k*(Q_before^2 - Q_after^2). current += cur_k*v
        "valve": {"flow_k": 0.85, "exp": 1.3, "cur_k": 0.40, "temp_k": 3.0, "vib_k": 0.03},
        # Scenario targets relative to baseline. *_mul multiplies, *_add adds.
        "scenarios": {
            "normal": {
                "label": "Normal", "signature": "All channels at baseline",
            },
            "blockage": {
                "label": "Outlet restriction", "signature": "Flow ↓, Pressure ↑, Current ↑",
                "flow_mul": 0.45, "pressure_add": 1.1, "current_add": 0.35,
                "temp_add": 4.0, "vib_add": 0.04, "rpm_add": -15,
            },
            "imbalance": {
                "label": "Mechanical imbalance", "signature": "Vibration ↑↑, Current ↑, Temp ↑",
                "vib_mul": 3.6, "crest": 2.2, "current_add": 0.18, "temp_add": 3.0,
                "rpm_add": -10, "flow_mul": 0.98,
            },
            "overload": {
                "label": "Overload", "signature": "RPM ↓, Current ↑↑, Temp ↑, Flow ↓",
                "rpm_add": -180, "current_add": 1.2, "temp_add": 12.0, "flow_mul": 0.85,
                "pressure_add": -0.3, "vib_add": 0.05,
            },
            "dry_run": {
                "label": "Dry run", "signature": "Float LOW → Flow 0, Current ↓, pump trips",
                "float": 0, "flow_mul": 0.0, "pressure_mul": 0.03, "current_mul": 0.55,
                "rpm_add": 40, "vib_add": 0.15, "crest": 4.0,
            },
        },
    },
}

# Sensor metadata for the HMI sensor table. 'pin' must match firmware/esp32_pump_node.
SENSORS = [
    {"id": "vib",      "part": "ADXL345",  "name": "Vibration (3-axis)", "pin": "I2C 0x53 (SDA21/SCL22)",
     "field": "vib_rms", "unit": "g",     "range": [0, 16],   "dp": 3,
     "features": ["vib_rms", "vib_peak", "vib_std"]},
    {"id": "temp",     "part": "DS18B20",  "name": "Motor temperature",  "pin": "GPIO4 1-Wire",
     "field": "temp_c", "unit": "°C",     "range": [-10, 100], "dp": 1,
     "features": ["temp_mean", "temp_slope"]},
    {"id": "current",  "part": "ACS712",   "name": "Motor current",      "pin": "GPIO34 ADC1_CH6",
     "field": "current_a", "unit": "A",   "range": [0, 20],   "dp": 2,
     "features": ["current_rms", "current_std"]},
    {"id": "flow",     "part": "YF-S201",  "name": "Water flow",         "pin": "GPIO27 pulse",
     "field": "flow_lpm", "unit": "L/min", "range": [0, 40],  "dp": 2,
     "features": ["flow_mean", "flow_std"]},
    {"id": "pressure", "part": "0–5 bar",  "name": "Outlet pressure",    "pin": "GPIO35 ADC1_CH7",
     "field": "pressure_bar", "unit": "bar", "range": [0, 5], "dp": 2,
     "features": ["pressure_mean", "pressure_std"]},
    {"id": "hall",     "part": "A3144",    "name": "Shaft speed",        "pin": "GPIO26 interrupt",
     "field": "rpm", "unit": "rpm",       "range": [0, 3000], "dp": 0,
     "features": ["rpm_mean"]},
    {"id": "float",    "part": "Float sw.", "name": "Inlet water level", "pin": "GPIO25 DI pull-up",
     "field": "float", "unit": "0/1",     "range": [0, 1],    "dp": 0,
     "features": []},
]
