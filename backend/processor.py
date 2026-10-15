"""
Synchronous pipeline core: line -> validate -> window -> features -> score -> status/alerts.

No I/O and no asyncio, so the exact same object runs inside the server, inside
tools/replay_eval.py and in the headless scenario test.
"""
import itertools
from datetime import datetime, timezone

from .analytics import diagnose, engineering
from .features import Window
from .schema import Invalid, parse_line
from .scorer import standardise

HEALTHY, WARNING, CRITICAL = "HEALTHY", "WARNING", "CRITICAL"
STANDBY, STARTING, DRY_RUN = "STANDBY", "STARTING", "DRY-RUN PROTECTION"

SEVERITY = {HEALTHY: "info", STANDBY: "info", STARTING: "info", WARNING: "warn",
            CRITICAL: "crit", DRY_RUN: "crit"}

_alert_ids = itertools.count(1)
SENSOR_WORD = {"hall": "speed", "current": "current", "temp": "temperature", "flow": "flow",
               "pressure": "pressure", "vib": "vibration"}


def make_alert(now, sev, msg, status=None):
    return {"id": next(_alert_ids), "t": now, "iso": iso(now), "sev": sev, "msg": msg, "status": status}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).astimezone().isoformat(timespec="seconds")


class Processor:
    def __init__(self, cfg, scorer, sensors):
        self.cfg = cfg
        self.scorer = scorer
        self.sensors = sensors
        self.st = cfg["status"]
        self.feat_by_key = {f["key"]: f for f in cfg["features"]}
        self.window = Window(cfg["window"])
        self.reset_counters()
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self):
        """Forget stream state (on source switch or replay loop). Counters persist."""
        self.window.clear()
        self.last_ts = None
        self.last_seq = None
        self.running = False
        self.start_ts = None
        self.status = None
        self.band = None
        self.pending = None
        self.pending_n = 0
        self.prev_float = None
        self.frame_no = 0

    def reset_counters(self):
        self.counters = {"received": 0, "valid": 0, "invalid": 0, "dropped": 0,
                         "skipped": 0, "invalid_by_reason": {}, "last_invalid": None}
        # Session totals (reset with the counters, not on a source switch).
        self.totals = {"run_s": 0.0, "starts": 0, "energy_wh": 0.0, "pumped_l": 0.0}
        self._prev_run_ts = None

    # ------------------------------------------------------------------ ingest
    def ingest(self, line, now):
        """Returns {"ok": True, "frame": {...}, "alerts": [...]} or {"ok": False, ...}."""
        c = self.counters
        c["received"] += 1
        alerts = []
        try:
            r = parse_line(line, self.cfg)
            self._check_sequence(r, alerts, now)
        except Invalid as e:
            c["invalid"] += 1
            c["invalid_by_reason"][e.reason] = c["invalid_by_reason"].get(e.reason, 0) + 1
            c["last_invalid"] = {"t": now, "reason": e.reason, "detail": e.detail}
            return {"ok": False, "reason": e.reason, "detail": e.detail}
        c["valid"] += 1
        frame = self._process(r, now, alerts)
        return {"ok": True, "frame": frame, "alerts": alerts}

    def _check_sequence(self, r, alerts, now):
        ts, link = r["ts"], self.cfg["link"]
        if self.last_ts is not None and ts <= self.last_ts:
            if self.last_ts - ts < link["ts_reset_back_ms"]:
                raise Invalid("ts_order", f"ts {ts} <= {self.last_ts}")
            # Large backwards jump: the ESP32 rebooted (millis() restarted).
            alerts.append(make_alert(now, "warn", "ESP32 restarted"))
            self.window.clear()
            self.last_seq = None
        elif self.last_ts is not None:
            if "seq" in r and self.last_seq is not None and r["seq"] > self.last_seq:
                self.counters["dropped"] += r["seq"] - self.last_seq - 1
            elif "seq" not in r:
                period = link["expected_interval_s"] * 1000
                gap = ts - self.last_ts
                if gap > 1.5 * period:
                    self.counters["dropped"] += int(round(gap / period)) - 1
        self.last_ts = ts
        self.last_seq = r.get("seq")

    def _process(self, r, now, alerts):
        st = self.st
        running = r["rpm"] >= st["standby_rpm"]

        # Float switch is a safety interlock, never an ML input.
        if self.prev_float is not None and r["float"] != self.prev_float:
            if r["float"] == 0:
                alerts.append(make_alert(now, "crit", "Water level LOW — dry-run protection tripped", DRY_RUN))
            else:
                alerts.append(make_alert(now, "info", "Water level back to normal"))
        elif self.prev_float is None and r["float"] == 0:
            alerts.append(make_alert(now, "crit", "Water level LOW", DRY_RUN))
        self.prev_float = r["float"]

        if running and not self.running:
            self.start_ts = r["ts"]
            self.window.clear()
            self.totals["starts"] += 1
        if not running:
            self.window.clear()
            self.band = None
        self.running = running

        features = z = score = health = None
        extras = {}
        if running:
            self.window.push(r)
            if self.window.full:
                features = self.window.features()
                z = standardise(features, self.cfg["features"])
                score, extras = self.scorer.score(features)
                health = round((1.0 - score) * 100)

        eng = engineering(r, features, running, self.cfg)
        if running and self._prev_run_ts is not None and 0 < r["ts"] - self._prev_run_ts <= 5000:
            dt = (r["ts"] - self._prev_run_ts) / 1000.0
            self.totals["run_s"] += dt
            self.totals["energy_wh"] += eng["p_in_w"] * dt / 3600.0
            self.totals["pumped_l"] += r["flow_lpm"] * dt / 60.0
        self._prev_run_ts = r["ts"] if running else None

        inhibited = running and (r["ts"] - self.start_ts) < st["startup_inhibit_s"] * 1000
        if r["float"] == 0:
            status = DRY_RUN
        elif not running:
            status = STANDBY
        elif inhibited or score is None:
            status = STARTING
        else:
            status = self._debounced_band(score)

        if status != self.status:
            if self.status is not None or status != HEALTHY:
                alerts.append(self._transition_alert(self.status, status, score, now, z))
            self.status = status

        self.frame_no += 1
        diag = diagnose(z, r, status, self.cfg)
        return {
            "type": "frame", "n": self.frame_no, "t": now,
            "reading": r, "running": running,
            "features": features, "z": z,
            "score": score, "health": health, "status": status,
            "window_fill": len(self.window.buf),
            "inhibit_left_s": max(0.0, st["startup_inhibit_s"] - (r["ts"] - self.start_ts) / 1000) if inhibited else 0,
            "channels": self._channel_states(r, z, running),
            "scorer": extras,
            "eng": eng,
            "diagnosis": diag,
            "totals": {k: round(v, 3) for k, v in self.totals.items()},
        }

    def _band(self, score, latched=None):
        """Band for a score. Relative to the latched band, a lower band is only
        returned once the score is `deadband` below that band's threshold."""
        st, db = self.st, self.st.get("deadband", 0.0)
        crit_th = st["crit"] - db if latched == CRITICAL else st["crit"]
        warn_th = st["warn"] - db if latched in (WARNING, CRITICAL) else st["warn"]
        if score > crit_th:
            return CRITICAL
        if score >= warn_th:
            return WARNING
        return HEALTHY

    def _debounced_band(self, score):
        band = self._band(score, self.band)
        if self.band is None:          # first scored sample after a start: latch immediately
            self.band, self.pending, self.pending_n = band, None, 0
            return band
        if band == self.band:
            self.pending, self.pending_n = None, 0
        elif band == self.pending:
            self.pending_n += 1
            if self.pending_n >= self.st["debounce_samples"]:
                self.band, self.pending, self.pending_n = band, None, 0
        else:
            self.pending, self.pending_n = band, 1
            if self.st["debounce_samples"] <= 1:
                self.band, self.pending, self.pending_n = band, None, 0
        return self.band

    def _transition_alert(self, old, new, score, now, z):
        """Plain-language event text for the dashboard's event list."""
        sev = SEVERITY[new]
        text = {HEALTHY: "Healthy", WARNING: "Warning", CRITICAL: "Critical",
                STANDBY: "Pump stopped", STARTING: "Pump started",
                DRY_RUN: "Dry-run protection: pump stopped"}[new]
        if new == HEALTHY and old in (WARNING, CRITICAL):
            text = "Back to healthy"
        if score is not None and new in (WARNING, CRITICAL):
            text += f" — anomaly score {score:.2f}"
            if z:
                names = []
                for k, _ in sorted(z.items(), key=lambda kv: -abs(kv[1])):
                    name = SENSOR_WORD[self.feat_by_key[k]["sensor"]]
                    if name not in names:
                        names.append(name)
                    if len(names) == 2:
                        break
                text += f" ({' and '.join(names)} unusual)"
        return make_alert(now, sev, text, new)

    def _channel_states(self, r, z, running):
        out = {}
        for s in self.sensors:
            if s["id"] == "float":
                out["float"] = "good" if r["float"] == 1 else "crit"
                continue
            if not running or z is None:
                out[s["id"]] = "idle"
                continue
            m = max(abs(z[k]) for k in s["features"])
            out[s["id"]] = "crit" if m >= self.st["channel_z_crit"] else "warn" if m >= self.st["channel_z_warn"] else "good"
        return out
