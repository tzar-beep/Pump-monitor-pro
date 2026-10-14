"""Sliding-window feature extraction. Pure Python so it runs identically in the
server, in tools/replay_eval.py and in tools/train_iforest.py."""
import math
from collections import deque


def _mean(xs):
    return sum(xs) / len(xs)


def _std(xs):
    # Population STD (ddof=0): with a 5-sample window the sample/population
    # difference is a fixed factor that is absorbed by the feature scale anyway.
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _rms(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs))


def _slope_per_min(ts_ms, ys):
    """Least-squares slope of ys against time, in units per minute."""
    t = [(v - ts_ms[0]) / 60000.0 for v in ts_ms]
    tm, ym = _mean(t), _mean(ys)
    den = sum((a - tm) ** 2 for a in t)
    if den == 0:
        return 0.0
    return sum((a - tm) * (b - ym) for a, b in zip(t, ys)) / den


def extract(window):
    """window: sequence of validated readings (oldest first) -> ordered feature dict."""
    ts = [r["ts"] for r in window]
    rpm = [r["rpm"] for r in window]
    cur = [r["current_a"] for r in window]
    tmp = [r["temp_c"] for r in window]
    flo = [r["flow_lpm"] for r in window]
    pre = [r["pressure_bar"] for r in window]
    vib = [r["vib_rms"] for r in window]
    return {
        "rpm_mean": _mean(rpm),
        "current_rms": _rms(cur),
        "current_std": _std(cur),
        "temp_mean": _mean(tmp),
        "temp_slope": _slope_per_min(ts, tmp),
        "flow_mean": _mean(flo),
        "flow_std": _std(flo),
        "pressure_mean": _mean(pre),
        "pressure_std": _std(pre),
        "vib_rms": _rms(vib),
        "vib_peak": max(r["vib_peak"] for r in window),
        "vib_std": _std(vib),
    }


class Window:
    def __init__(self, size):
        self.size = size
        self.buf = deque(maxlen=size)

    def push(self, reading):
        self.buf.append(reading)

    def clear(self):
        self.buf.clear()

    @property
    def full(self):
        return len(self.buf) == self.size

    def features(self):
        return extract(list(self.buf)) if self.buf else None
