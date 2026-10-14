"""
Headless scenario test: drives the simulator through every scenario and valve
position, through the real Processor and AlarmManager, and asserts the expected
status band, the fault diagnosis and the alarm states.
Run:  python -m tools.scenario_check [-v]   (from the project root)
"""
import sys

from backend.alarms import AlarmManager
from backend.config import CONFIG, SENSORS
from backend.processor import Processor
from backend.scorer import DemoScorer
from backend.simulator import Simulator

SCRIPT = [
    # (label, seconds, action, expected final status, expected diagnosis or None)
    ("normal", 40, lambda s: s.set_scenario("normal"), "HEALTHY", None),
    ("valve 20%", 30, lambda s: s.set_valve(20), "HEALTHY", None),
    ("valve 40%", 30, lambda s: s.set_valve(40), "WARNING", "restriction"),
    ("valve 70%", 30, lambda s: s.set_valve(70), "CRITICAL", "restriction"),
    ("valve 0%", 30, lambda s: s.set_valve(0), "HEALTHY", None),
    ("blockage", 40, lambda s: s.set_scenario("blockage"), "CRITICAL", "restriction"),
    ("normal", 40, lambda s: s.set_scenario("normal"), "HEALTHY", None),
    ("imbalance", 40, lambda s: s.set_scenario("imbalance"), "CRITICAL", "imbalance"),
    ("normal", 40, lambda s: s.set_scenario("normal"), "HEALTHY", None),
    ("overload", 40, lambda s: s.set_scenario("overload"), "CRITICAL", "overload"),
    ("normal", 240, lambda s: s.set_scenario("normal"), "HEALTHY", None),
    ("pump off", 20, lambda s: s.set_pump(False), "STANDBY", None),
    ("pump on", 40, lambda s: s.set_pump(True), "HEALTHY", None),
    ("dry run", 20, lambda s: s.set_scenario("dry_run"), "DRY-RUN PROTECTION", "dry_run"),
    ("float ok, latched", 10, lambda s: s.set_scenario("normal"), "STANDBY", None),
    ("restart", 40, lambda s: s.set_pump(True), "HEALTHY", None),
]


def run(verbose=False, seed=1):
    sim = Simulator(CONFIG, seed=seed)
    p = Processor(CONFIG, DemoScorer(CONFIG), SENSORS)
    am = AlarmManager(CONFIG, SENSORS)
    t, failures = 1_700_000_000.0, []
    for label, secs, action, expect, expect_diag in SCRIPT:
        action(sim)
        scores = []
        for _ in range(secs):
            t += 1
            res = p.ingest(sim.line(), t)
            assert res["ok"], res
            f = res["frame"]
            am.evaluate(am.frame_conditions(f), t)
            if f["score"] is not None:
                scores.append(f["score"])
            for a in res["alerts"]:
                if verbose:
                    print(f"    [{a['sev']:4}] {a['msg']}")
        r, e, d = f["reading"], f["eng"], f["diagnosis"]
        diag = d["fault"] if d else None
        ok = f["status"] == expect and (expect_diag is None or diag == expect_diag)
        rng = f"{min(scores):.2f}-{max(scores):.2f}" if scores else "  -  "
        dtxt = f"{diag}({d['confidence']:.2f})" if d else "-"
        eff = f"{e['eff_pct']:4.1f}%" if e["eff_pct"] is not None else "  -  "
        print(f"{'OK ' if ok else 'BAD'} {label:18} -> {f['status']:19} score {rng:10} diag {dtxt:22} "
              f"Q {r['flow_lpm']:5.1f} P {r['pressure_bar']:4.2f} I {r['current_a']:4.2f} n {r['rpm']:5.0f} "
              f"vib {r['vib_rms']:.3f} eff {eff} alarms {[a['tag'] for a in am.listing() if a['active']]}")
        if not ok:
            failures.append((label, expect, f["status"], expect_diag, diag))
        am.ack()                       # operator acknowledges after each step
    # Alarm state machine: everything acknowledged and healthy -> list must be empty.
    if am.listing():
        failures.append(("alarms not cleared after ack + recovery", am.listing()))
    print("counters", p.counters, "totals", p.totals)
    return failures


def alarm_unit_test():
    am = AlarmManager(CONFIG, SENSORS)
    c = {"X.HI": (True, "test", "MED", 1)}
    assert am.evaluate(c, 1)[0][0] == "raise" and am.summary() == {"active": 1, "unacked": 1, "high_unacked": 0}
    am.ack("X.HI")
    assert am.summary()["unacked"] == 0 and am.listing()[0]["active"]        # active + acked stays listed
    am.evaluate({"X.HI": (False, "test", "MED", 0)}, 2)
    assert am.listing() == []                                                # cleared + acked -> gone
    am.evaluate(c, 3)
    am.evaluate({"X.HI": (False, "test", "MED", 0)}, 4)
    assert am.listing()[0]["active"] is False and not am.listing()[0]["acked"]  # RTN unacked stays
    am.ack()
    assert am.listing() == []
    print("alarm state machine OK")


if __name__ == "__main__":
    alarm_unit_test()
    fails = run(verbose="-v" in sys.argv)
    if fails:
        print("FAILED:", fails)
        sys.exit(1)
    print("all scenario checks passed")
