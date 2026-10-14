"""CSV session logger: one row per valid message (all raw fields + derived + score + status)."""
import csv
import os
import time

COLUMNS = ["wall_time", "source", "ts", "seq", "rpm", "temp_c", "current_a", "flow_lpm",
           "pressure_bar", "vib_x", "vib_y", "vib_z", "vib_peak", "float", "vib_rms",
           "score", "health", "status"]


class CsvLogger:
    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        self.fh = self.writer = None
        self.path = None
        self.rows = 0

    @property
    def enabled(self):
        return self.fh is not None

    def start(self, source):
        self.stop()
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, time.strftime(f"pump_%Y%m%d_%H%M%S_{source}.csv"))
        self.fh = open(self.path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.fh)
        self.writer.writerow(COLUMNS)
        self.rows = 0

    def stop(self):
        if self.fh:
            self.fh.close()
        self.fh = self.writer = None

    def write(self, frame, source):
        if not self.fh:
            return
        r = frame["reading"]
        score = frame["score"]
        self.writer.writerow([
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(frame["t"])), source,
            r["ts"], r.get("seq", ""), r["rpm"], r["temp_c"], r["current_a"], r["flow_lpm"],
            r["pressure_bar"], r["vib_x"], r["vib_y"], r["vib_z"], round(r["vib_peak"], 4), r["float"],
            round(r["vib_rms"], 4), "" if score is None else round(score, 4),
            "" if frame["health"] is None else frame["health"], frame["status"],
        ])
        self.fh.flush()
        self.rows += 1
