"""
Anomaly scorers. Every scorer maps an ordered feature dict to a score in [0, 1].

  DemoScorer            - standardised distance from the healthy baseline.
                          A transparent stand-in for Isolation Forest.
  IsolationForestScorer - pure-Python inference of a scikit-learn IsolationForest
                          exported to JSON by tools/train_iforest.py.

>>> TRAINED MODEL HOOK: load_scorer() below is the single place where the model is chosen.
"""
import json
import math
import os


def standardise(features, feat_cfg):
    """z_i = (f_i - baseline_i) / scale_i for every configured feature."""
    return {f["key"]: (features[f["key"]] - f["mean"]) / f["scale"] for f in feat_cfg}


class DemoScorer:
    """
    D     = sqrt( (1/N) * sum_i clip(z_i, -z_clip, +z_clip)^2 )
    score = 1 - exp( -(D / d0)^2 )

    D is the RMS standardised distance of the feature vector from the healthy
    baseline. With d0 = 2, a healthy rig (D ~ 0.5) scores ~0.06; D = 1.19 crosses
    the WARNING band (0.3) and D = 1.91 crosses CRITICAL (0.6).
    """
    name = "Demo scorer (standardised distance)"
    kind = "demo"

    def __init__(self, cfg):
        self.feat_cfg = cfg["features"]
        self.d0 = cfg["scorer"]["d0"]
        self.z_clip = cfg["scorer"]["z_clip"]

    def score(self, features):
        z = standardise(features, self.feat_cfg)
        zc = [max(-self.z_clip, min(self.z_clip, v)) for v in z.values()]
        d = math.sqrt(sum(v * v for v in zc) / len(zc))
        return 1.0 - math.exp(-(d / self.d0) ** 2), {"distance": d}


def _c(n):
    """Average path length of an unsuccessful BST search (Liu et al. 2008)."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1.0) + 0.5772156649) - 2.0 * (n - 1.0) / n


class IsolationForestScorer:
    """
    Inference for a forest exported by tools/train_iforest.py. Format:
      {"type": "isolation_forest", "features": [...12 keys...],
       "max_samples": int,
       "trees": [{"left": [...], "right": [...], "feature": [...],
                  "threshold": [...], "n": [...]}, ...],
       "calibration": {"lo": float, "warn_raw": float, "crit_raw": float,
                       "warn": 0.3, "crit": 0.6}}
    raw   = 2 ** (-E[h(x)] / c(max_samples))      (~0.4 normal, -> 1 anomalous)
    score = piecewise-linear map of raw through three anchors:
              lo -> 0,  warn_raw -> warn,  crit_raw -> crit  (slope continues above, clipped to 1)
    The anchors are fitted by tools/train_iforest.py (see its docstring).
    """
    kind = "iforest"

    def __init__(self, model, path):
        if model.get("type") != "isolation_forest":
            raise ValueError("not an isolation_forest export")
        self.keys = model["features"]
        self.trees = model["trees"]
        self.cn = _c(model["max_samples"])
        cal = model["calibration"]
        self.anchors = [(cal["lo"], 0.0), (cal["warn_raw"], cal["warn"]), (cal["crit_raw"], cal["crit"])]
        self.name = f"Isolation Forest ({os.path.basename(path)}, {len(self.trees)} trees)"

    def raw(self, x):
        total = 0.0
        for t in self.trees:
            node, depth = 0, 0
            left, right = t["left"], t["right"]
            while left[node] != -1:
                node = left[node] if x[t["feature"][node]] <= t["threshold"][node] else right[node]
                depth += 1
            total += depth + _c(t["n"][node])
        return 2.0 ** (-(total / len(self.trees)) / self.cn)

    def score(self, features):
        r = self.raw([features[k] for k in self.keys])
        return self.calibrate(r), {"raw": r}

    def calibrate(self, r):
        (r0, s0), (r1, s1), (r2, s2) = self.anchors
        if r <= r1:
            s = s0 + (s1 - s0) * (r - r0) / (r1 - r0)
        else:
            s = s1 + (s2 - s1) * (r - r1) / (r2 - r1)
        return max(0.0, min(1.0, s))


def load_scorer(cfg, log=print):
    # ------------------------------------------------------------------------
    # TRAINED MODEL HOOK
    # Set CONFIG["scorer"]["model_path"] (or pass --model) to a JSON file produced
    # by tools/train_iforest.py and the Isolation Forest replaces the demo scorer.
    # To use another runtime (e.g. onnxruntime), add a class with
    # .name, .kind and .score(features) -> (score_0_1, extras) and return it here.
    # ------------------------------------------------------------------------
    path = cfg["scorer"].get("model_path")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return IsolationForestScorer(json.load(fh), path)
        except (OSError, ValueError, KeyError) as e:
            log(f"[scorer] could not load {path!r} ({e}); falling back to demo scorer")
    return DemoScorer(cfg)
