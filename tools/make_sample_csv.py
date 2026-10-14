"""
Generate data/sample_session.csv: a scripted simulator session in the CSV log
format (normal running, valve sweep, every fault scenario, stop/start, dry run),
scored by the Pi pipeline. Used by replay mode and as training data for the demo.

  python tools/make_sample_csv.py [--minutes-normal 10]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.config import CONFIG, SENSORS  # noqa: E402
from backend.csvlog import CsvLogger  # noqa: E402
from backend.processor import Processor  # noqa: E402
from backend.scorer import DemoScorer  # noqa: E402
from backend.simulator import Simulator  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes-normal", type=float, default=10)
    ap.add_argument("--out", default="data/sample_session.csv")
    a = ap.parse_args()

    sim = Simulator(CONFIG, seed=42)
    p = Processor(CONFIG, DemoScorer(CONFIG), SENSORS)
    script = [
        (int(a.minutes_normal * 60), lambda: sim.set_scenario("normal")),
        (30, lambda: sim.set_valve(30)), (30, lambda: sim.set_valve(50)), (30, lambda: sim.set_valve(75)),
        (40, lambda: sim.set_valve(0)),
        (60, lambda: sim.set_scenario("blockage")), (60, lambda: sim.set_scenario("normal")),
        (60, lambda: sim.set_scenario("imbalance")), (60, lambda: sim.set_scenario("normal")),
        (60, lambda: sim.set_scenario("overload")), (240, lambda: sim.set_scenario("normal")),
        (20, lambda: sim.set_pump(False)), (60, lambda: sim.set_pump(True)),
        (20, lambda: sim.set_scenario("dry_run")), (10, lambda: sim.set_scenario("normal")),
        (60, lambda: sim.set_pump(True)),
    ]
    log = CsvLogger(os.path.dirname(os.path.abspath(a.out)))
    log.start("sim")
    t = time.time()
    for secs, action in script:
        action()
        for _ in range(secs):
            t += 1
            res = p.ingest(sim.line(), t)
            log.write(res["frame"], "sim")
    log.stop()
    os.replace(log.path, a.out)
    print(f"wrote {a.out}: {p.counters['valid']} rows")


if __name__ == "__main__":
    main()
