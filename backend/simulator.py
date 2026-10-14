"""
Rig simulator. Emits the exact JSON line the ESP32 firmware prints, so it is fed
through the same parse -> validate -> feature -> score path as real hardware.

Operating points are blended with first-order lags (per-channel time constants),
so scenario and valve changes ramp in instead of jumping.
"""
import json
import math
import random


class Simulator:
    def __init__(self, cfg, seed=None):
        self.cfg = cfg
        self.sc = cfg["simulator"]
        self.base = cfg["baseline"]
        self.rng = random.Random(seed)
        self.scenario = "normal"
        self.valve = 0.0            # 0..1 closed
        self.pump_on = True         # commanded state
        self.tripped = False        # dry-run interlock latched
        self.float_low_for = 0.0
        self.t_ms = 0
        self.seq = 0
        b = self.base
        self.x = {"rpm": b["rpm"], "current_a": b["current_a"], "flow_lpm": b["flow_lpm"],
                  "pressure_bar": b["pressure_bar"], "vib": b["vib_rms"], "temp_c": b["temp_c"],
                  "crest": self.sc["vib_crest"]}

    # ------------------------------------------------------------ commands
    def set_scenario(self, name):
        if name not in self.sc["scenarios"]:
            raise ValueError(f"unknown scenario {name}")
        self.scenario = name

    def set_valve(self, pct):
        self.valve = max(0.0, min(1.0, float(pct) / 100.0))

    def set_pump(self, on):
        self.pump_on = bool(on)
        if on:
            self.tripped = False    # manual restart resets the latched interlock

    @property
    def running(self):
        return self.pump_on and not self.tripped

    @property
    def float_ok(self):
        return int(self.sc["scenarios"][self.scenario].get("float", 1))

    # ------------------------------------------------------------ model
    def _targets(self):
        b, s = self.base, self.sc["scenarios"][self.scenario]
        if not self.running:
            return {"rpm": 0.0, "current_a": 0.0, "flow_lpm": 0.0, "pressure_bar": 0.0,
                    "vib": self.sc["vib_off_g"], "temp_c": b["ambient_c"], "crest": self.sc["vib_crest"]}

        def apply(base_val, key):
            return base_val * s.get(f"{key}_mul", 1.0) + s.get(f"{key}_add", 0.0)

        t = {
            "rpm": apply(b["rpm"], "rpm"),
            "current_a": apply(b["current_a"], "current"),
            "flow_lpm": apply(b["flow_lpm"], "flow"),
            "pressure_bar": apply(b["pressure_bar"], "pressure"),
            "vib": apply(b["vib_rms"], "vib"),
            "temp_c": apply(b["temp_c"], "temp"),
            "crest": s.get("crest", self.sc["vib_crest"]),
        }
        # Valve restriction stacks on top of any scenario. A dry pump has no
        # liquid to restrict, so flow/pressure terms vanish naturally (flow_mul=0).
        v, vk = self.valve, self.sc["valve"]
        vp = v ** vk["exp"]
        q_before = t["flow_lpm"]
        t["flow_lpm"] *= max(0.0, 1.0 - vk["flow_k"] * vp)
        if t["flow_lpm"] > 0.5:
            # Throttling moves the duty point up the pump curve H = h0 - k Q^2.
            k = self.cfg["engineering"]["pump_curve"]["k_bar_per_lpm2"]
            t["pressure_bar"] += k * (q_before ** 2 - t["flow_lpm"] ** 2)
            t["current_a"] += vk["cur_k"] * v
            t["temp_c"] += vk["temp_k"] * v
        t["vib"] += vk["vib_k"] * v
        t["pressure_bar"] = min(t["pressure_bar"], 4.8)
        return t

    def step(self, dt=None):
        dt = dt or 1.0 / self.sc["rate_hz"]
        # Dry-run interlock emulation (what the ESP32/relay would do on the rig).
        if self.float_ok == 0 and self.running:
            self.float_low_for += dt
            if self.float_low_for >= self.sc["interlock_delay_s"]:
                self.tripped = True
        else:
            self.float_low_for = 0.0

        tgt = self._targets()
        tau = self.sc["tau_s"]
        taus = {"rpm": tau["rpm"], "current_a": tau["current_a"], "flow_lpm": tau["flow_lpm"],
                "pressure_bar": tau["pressure_bar"], "vib": tau["vib"], "crest": tau["vib"],
                "temp_c": tau["temp_running"] if self.running else tau["temp_stopped"]}
        if not self.running:
            for k in ("rpm", "current_a", "flow_lpm", "pressure_bar", "vib"):
                taus[k] = tau["coast_down"]
        for k, target in tgt.items():
            a = 1.0 - math.exp(-dt / taus[k])
            self.x[k] += (target - self.x[k]) * a

        self.t_ms += int(dt * 1000)
        self.seq += 1
        return self._measure()

    def _measure(self):
        n, g, x = self.sc["noise"], self.rng.gauss, self.x
        on = x["rpm"] > 50
        rpm = max(0.0, x["rpm"] + (g(0, n["rpm"]) if on else 0))
        cur = max(0.0, x["current_a"] + g(0, n["current_a"]) * (1 if on else 0.3))
        flow = max(0.0, x["flow_lpm"] + (g(0, n["flow_lpm"]) if x["flow_lpm"] > 0.5 else 0))
        pres = max(0.0, x["pressure_bar"] + g(0, n["pressure_bar"]))
        q = self.sc["temp_quantum_c"]
        temp = round((x["temp_c"] + g(0, n["temp_c"])) / q) * q

        ratio = self.sc["vib_axis_ratio"]
        norm = math.sqrt(sum(r * r for r in ratio) / 3.0)
        axes = [max(0.0, x["vib"] * r / norm * (1 + g(0, n["vib_axis_rel"]))) for r in ratio]
        vrms = math.sqrt(sum(a * a for a in axes) / 3.0)
        peak = vrms * x["crest"] * (1 + abs(g(0, n["vib_peak_rel"])))

        return {
            "ts": self.t_ms, "seq": self.seq,
            "rpm": round(rpm, 1), "temp_c": round(temp, 4), "current_a": round(cur, 3),
            "flow_lpm": round(flow, 2), "pressure_bar": round(pres, 3),
            "vib_x": round(axes[0], 4), "vib_y": round(axes[1], 4), "vib_z": round(axes[2], 4),
            "vib_peak": round(peak, 4), "float": self.float_ok,
        }

    def line(self, dt=None):
        return json.dumps(self.step(dt), separators=(",", ":"))

    def state(self):
        return {"scenario": self.scenario, "valve_pct": round(self.valve * 100),
                "pump_cmd": self.pump_on, "tripped": self.tripped}
