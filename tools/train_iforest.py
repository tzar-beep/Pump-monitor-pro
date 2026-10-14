"""
Train a scikit-learn IsolationForest on HEALTHY logged data and export it to the
JSON format that backend/scorer.py:IsolationForestScorer runs in pure Python on the Pi.

  pip install scikit-learn numpy        (training machine only; the Pi needs neither)
  python tools/train_iforest.py logs/pump_*.csv --out models/iforest.json
  python -m backend --model models/iforest.json

Training rows: only STEADY healthy running - logged status HEALTHY with a logged score
below warn/2, and at least STEADY_ROWS consecutive such rows before it (this drops
start-ups and recoveries, which are labelled HEALTHY but are still drifting back).
Use --all-running to take every running row of a log you know is fault-free.
Features are computed with backend/features.py over the same 5-sample window, so
training and inference see identical vectors.

Calibration. Raw IF scores s = 2^(-E[h]/c(psi)) are compressed (healthy ~0.45,
clear faults only ~0.6-0.7), so a single linear rescale cannot reach CRITICAL.
Three anchors are mapped piecewise-linearly:
  lo       = median s of the healthy training vectors        -> score 0
  warn_raw = 99.5th percentile s of the healthy vectors       -> WARNING threshold
  crit_raw = lo + CRIT_FACTOR * (warn_raw - lo)               -> CRITICAL threshold
CRIT_FACTOR = 1.8 was chosen on simulator fault data. Re-check it with
tools/replay_eval.py once real fault runs have been logged on the rig.
"""
import argparse
import copy
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.config import CONFIG  # noqa: E402
from backend.features import Window  # noqa: E402
from backend.schema import Invalid, parse_line  # noqa: E402
from backend.scorer import IsolationForestScorer  # noqa: E402
from backend.sources import csv_row_to_line  # noqa: E402

CRIT_FACTOR = 1.8
STEADY_ROWS = 30


def vectors(paths, cfg, all_running):
    keys = [f["key"] for f in cfg["features"]]
    X = []
    for path in paths:
        win = Window(cfg["window"])
        run_len = 0
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    r = parse_line(csv_row_to_line(row), cfg)
                except Invalid:
                    continue
                if r["rpm"] < cfg["status"]["standby_rpm"] or r["float"] == 0:
                    win.clear()
                    continue
                win.push(r)
                steady_healthy = row.get("status") == "HEALTHY" and row.get("score") not in (None, "")                     and float(row["score"]) < cfg["status"]["warn"] / 2
                run_len = run_len + 1 if steady_healthy else 0
                ok = all_running or run_len > STEADY_ROWS
                if win.full and ok:
                    f = win.features()
                    X.append([f[k] for k in keys])
    return keys, X


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--out", default="models/iforest.json")
    ap.add_argument("--trees", type=int, default=100)
    ap.add_argument("--max-samples", type=int, default=256)
    ap.add_argument("--all-running", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    import numpy as np
    from sklearn.ensemble import IsolationForest

    cfg = copy.deepcopy(CONFIG)
    keys, X = vectors(a.csv, cfg, a.all_running)
    if len(X) < 50:
        sys.exit(f"only {len(X)} healthy feature vectors; log at least a few minutes of normal running")
    X = np.asarray(X)
    ms = min(a.max_samples, len(X))
    model = IsolationForest(n_estimators=a.trees, max_samples=ms, random_state=a.seed).fit(X)

    trees = []
    for est, feats in zip(model.estimators_, model.estimators_features_):
        t = est.tree_
        # Map the tree's local feature index back to the full feature vector.
        feature = [int(feats[f]) if f >= 0 else -1 for f in t.feature]
        trees.append({"left": t.children_left.tolist(), "right": t.children_right.tolist(),
                      "feature": feature, "threshold": [float(v) for v in t.threshold],
                      "n": t.n_node_samples.tolist()})
    raw = -model.score_samples(X)               # sklearn returns -s
    st = cfg["status"]
    lo, warn_raw = float(np.median(raw)), float(np.percentile(raw, 99.5))
    cal = {"lo": lo, "warn_raw": warn_raw, "crit_raw": lo + CRIT_FACTOR * (warn_raw - lo),
           "warn": st["warn"], "crit": st["crit"]}
    if not cal["lo"] < cal["warn_raw"] < cal["crit_raw"]:
        sys.exit(f"degenerate calibration {cal}; need more / more varied healthy data")
    export = {"type": "isolation_forest", "features": keys, "max_samples": int(model.max_samples_),
              "trees": trees, "calibration": cal,
              "trained_on": [os.path.basename(p) for p in a.csv], "n_train": len(X)}

    # Parity check: pure-Python inference must match scikit-learn.
    py = IsolationForestScorer(export, a.out)
    diffs = [abs(py.raw(list(x)) - r) for x, r in zip(X[:200], raw[:200])]
    if max(diffs) > 1e-6:
        sys.exit(f"parity check failed: max |python - sklearn| = {max(diffs):.2e}")

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(export, fh)
    print(f"trained on {len(X)} healthy vectors, {a.trees} trees, max_samples={model.max_samples_}")
    print(f"calibration raw anchors lo={cal['lo']:.4f} warn={cal['warn_raw']:.4f} crit={cal['crit_raw']:.4f}; "
          f"parity with sklearn max diff {max(diffs):.1e}")
    print(f"wrote {a.out} ({os.path.getsize(a.out) / 1024:.0f} KiB). Run: python -m backend --model {a.out}")


if __name__ == "__main__":
    main()
