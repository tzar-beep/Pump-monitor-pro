"""Parse and validate one ESP32 JSON line. Pure functions, no I/O."""
import json
import math

NUMERIC = ("rpm", "temp_c", "current_a", "flow_lpm", "pressure_bar", "vib_x", "vib_y", "vib_z", "vib_peak")


class Invalid(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason      # short counter key, e.g. "range", "missing", "parse"
        self.detail = detail


def _num(v):
    # bool is a subclass of int in Python; reject it explicitly.
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_line(line, cfg):
    """Return a clean reading dict or raise Invalid."""
    if isinstance(line, (bytes, bytearray)):
        if len(line) > cfg["serial"]["max_line_bytes"]:
            raise Invalid("too_long", f"{len(line)} bytes")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            raise Invalid("encoding")
    line = line.strip()
    if not line:
        raise Invalid("empty")
    if not line.startswith("{"):
        # Firmware debug prints are expected on the same UART; count but don't alarm.
        raise Invalid("not_json", line[:40])
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise Invalid("parse", str(e)[:60])
    if not isinstance(msg, dict):
        raise Invalid("parse", "not an object")
    return validate(msg, cfg)


def validate(msg, cfg):
    missing = [f for f in cfg["required_fields"] if f not in msg]
    if missing:
        raise Invalid("missing", ",".join(missing))

    out = {}
    ts = msg["ts"]
    if not _num(ts) or ts < 0:
        raise Invalid("type", "ts")
    out["ts"] = int(ts)

    if "seq" in msg:
        if not _num(msg["seq"]):
            raise Invalid("type", "seq")
        out["seq"] = int(msg["seq"])

    ranges = cfg["ranges"]
    for f in NUMERIC:
        if f not in msg:
            continue
        v = msg[f]
        if not _num(v):
            raise Invalid("type", f)
        lo, hi = ranges[f]
        if not lo <= v <= hi:
            raise Invalid("range", f"{f}={v}")
        out[f] = float(v)

    fl = msg["float"]
    if isinstance(fl, bool) or fl not in (0, 1):
        raise Invalid("range", f"float={fl}")
    out["float"] = int(fl)

    # Derived per-sample quantities used downstream.
    x, y, z = out["vib_x"], out["vib_y"], out["vib_z"]
    out["vib_rms"] = math.sqrt((x * x + y * y + z * z) / 3.0)
    if "vib_peak" not in out:
        out["vib_peak"] = max(x, y, z) * cfg["vib_peak_crest_fallback"]
    out["pressure_bar"] = max(0.0, out["pressure_bar"])
    return out
