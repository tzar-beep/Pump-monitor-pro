"""
Run a recorded CSV through the exact Pi pipeline offline (no server, no timing) and
summarise what the scorer does with it. Use it to compare scorers on logged data.

  python tools/replay_eval.py data/sample_session.csv
  python tools/replay_eval.py logs/pump_20261014_*.csv --model models/iforest.json --out scored.csv
"""
import argparse
import copy
import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.config import CONFIG, SENSORS  # noqa: E402
from backend.processor import Processor  # noqa: E402
from backend.scorer import load_scorer  # noqa: E402
from backend.sources import csv_row_to_line  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--model", help="trained Isolation Forest JSON; default = demo scorer")
    ap.add_argument("--out", help="write per-row recomputed score/status here")
    a = ap.parse_args()

    cfg = copy.deepcopy(CONFIG)
    cfg["scorer"]["model_path"] = a.model
    scorer = load_scorer(cfg)
    print(f"scorer: {scorer.name}")
    out = csv.writer(open(a.out, "w", newline="", encoding="utf-8")) if a.out else None
    if out:
        out.writerow(["file", "row", "ts", "logged_status", "status", "score", "health"])

    for path in a.csv:
        p = Processor(cfg, scorer, SENSORS)
        statuses, agree, compared, alerts = Counter(), 0, 0, []
        with open(path, newline="", encoding="utf-8") as fh:
            for i, row in enumerate(csv.DictReader(fh)):
                res = p.ingest(csv_row_to_line(row), float(i))
                if not res["ok"]:
                    continue
                f = res["frame"]
                statuses[f["status"]] += 1
                alerts += [a_["msg"] for a_ in res["alerts"]]
                if row.get("status"):
                    compared += 1
                    agree += row["status"] == f["status"]
                if out:
                    out.writerow([path, i, f["reading"]["ts"], row.get("status", ""), f["status"],
                                  "" if f["score"] is None else round(f["score"], 4), f["health"]])
        c = p.counters
        print(f"\n{path}\n  rows {c['received']}  valid {c['valid']}  invalid {c['invalid']}  dropped {c['dropped']}")
        print("  status mix: " + ", ".join(f"{k} {v}" for k, v in statuses.most_common()))
        if compared:
            print(f"  agreement with logged status: {agree}/{compared} ({100 * agree / compared:.1f} %)")
        print(f"  alerts ({len(alerts)}):")
        for m in alerts[:40]:
            print("    -", m)


if __name__ == "__main__":
    main()
