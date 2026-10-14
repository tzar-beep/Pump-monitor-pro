"""
Headless scenario test: drives the simulator through every scenario and valve
position, through the real Processor, and asserts the expected status bands
and alerts. Run:  python -m tools.scenario_check   (from the project root)
"""
import sys

from backend.config import CONFIG, SENSORS
from backend.processor import Processor
from backend.scorer import DemoScorer
from backend.simulator import Simulator

SCRIPT = [
    # (label, seconds, action, expected final status or None)
    ("normal", 40, lambda s: s.set_scenario("normal"), "HEALTHY"),
    ("valve 20%", 30, lambda s: s.set_valve(20), "HEALTHY"),
    ("valve 40%", 30, lambda s: s.set_valve(40), "WARNING"),
    ("valve 70%", 30, lambda s: s.set_valve(70), "CRITICAL"),
    ("valve 0%", 30, lambda s: s.set_valve(0), "HEALTHY"),
    ("blockage", 40, lambda s: s.set_scenario("blockage"), "CRITICAL"),
    ("normal", 40, lambda s: s.set_scenario("normal"), "HEALTHY"),
    ("imbalance", 40, lambda s: s.set_scenario("imbalance"), "CRITICAL"),
    ("normal", 40, lambda s: s.set_scenario("normal"), "HEALTHY"),
    ("overload", 40, lambda s: s.set_scenario("overload"), "CRITICAL"),
    ("normal", 240, lambda s: s.set_scenario("normal"), "HEALTHY"),
    ("pump off", 20, lambda s: s.set_pump(False), "STANDBY"),
    ("pump on", 40, lambda s: s.set_pump(True), "HEALTHY"),
    ("dry run", 20, lambda s: s.set_scenario("dry_run"), "DRY-RUN PROTECTION"),
    ("float ok, latched", 10, lambda s: s.set_scenario("normal"), "STANDBY"),
    ("restart", 40, lambda s: s.set_pump(True), "HEALTHY"),
]


def run(verbose=False, seed=1):
    sim = Simulator(CONFIG, seed=seed)
    p = Processor(CONFIG, DemoScorer(CONFIG), SENSORS)
    t, failures, rows = 1_700_000_000.0, [], []
    for label, secs, action, expect in SCRIPT:
        action(sim)
        scores = []
        for _ in range(secs):
            t += 1
            res = p.ingest(sim.line(), t)
            assert res["ok"], res
            f = res["frame"]
            rows.append((label, f))
            if f["score"] is not None:
                scores.append(f["score"])
            for a in res["alerts"]:
                if verbose:
                    print(f"    [{a['sev']:4}] {a['msg']}")
        last = rows[-1][1]
        ok = expect is None or last["status"] == expect
        rng = f"{min(scores):.2f}-{max(scores):.2f}" if scores else "  -  "
        print(f"{'OK ' if ok else 'BAD'} {label:18} -> {last['status']:20} score {rng:10} last "
              f"{(last['score'] or 0):.2f}  flow {last['reading']['flow_lpm']:5.1f} "
              f"P {last['reading']['pressure_bar']:4.2f} I {last['reading']['current_a']:4.2f} "
              f"rpm {last['reading']['rpm']:6.0f} T {last['reading']['temp_c']:5.1f} vib {last['reading']['vib_rms']:.3f}")
        if not ok:
            failures.append((label, expect, last["status"]))
    print("counters", p.counters)
    return failures


if __name__ == "__main__":
    fails = run(verbose="-v" in sys.argv)
    if fails:
        print("FAILED:", fails)
        sys.exit(1)
    print("all scenario checks passed")
