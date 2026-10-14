"""
Engineering values and rule-based fault diagnosis. Pure functions, no I/O.

engineering() turns the window features into the quantities a pump engineer reads:
hydraulic power, estimated electrical input, wire-to-water efficiency, head, specific
energy, motor slip, vibration crest factor and the deviation from the pump curve.

diagnose() explains an anomaly: it compares the standardised feature deviation
vector z with a signature vector per known fault (cosine similarity). It is a
transparent rule base, not a trained classifier, and is labelled that way in the UI.
"""
import math

G = 9.80665


def pump_curve_bar(q_lpm, rpm, pc):
    """H(Q, n) = h0 (n/n0)^2 - k Q^2  (affinity law for the shut-off head)."""
    return pc["h0_bar"] * (rpm / pc["n0_rpm"]) ** 2 - pc["k_bar_per_lpm2"] * q_lpm ** 2


def engineering(reading, features, running, cfg):
    e = cfg["engineering"]
    f = features
    q = f["flow_mean"] if f else reading["flow_lpm"]
    p = f["pressure_mean"] if f else reading["pressure_bar"]
    i = f["current_rms"] if f else reading["current_a"]
    n = f["rpm_mean"] if f else reading["rpm"]
    vr = f["vib_rms"] if f else reading["vib_rms"]
    vp = f["vib_peak"] if f else reading["vib_peak"]

    p_hyd = p * 1e5 * q / 60000.0                       # W   (outlet gauge pressure taken as pump Δp)
    p_in = e["supply_v"] * i * e["power_factor"]          # W   (estimate: V·I·PF)
    n_sync = 120.0 * e["line_hz"] / e["motor_poles"]
    q_m3h = q * 0.06
    curve = pump_curve_bar(q, n, e["pump_curve"]) if running else None
    out = {
        "p_hyd_w": p_hyd,
        "p_in_w": p_in,
        "eff_pct": (100.0 * p_hyd / p_in) if running and p_in > 5 else None,
        "head_m": p * 1e5 / (e["density_kg_m3"] * G),
        "spec_energy_wh_m3": (p_in / q_m3h) if running and q_m3h > 0.05 else None,
        "slip_pct": (100.0 * (n_sync - n) / n_sync) if running else None,
        "n_sync_rpm": n_sync,
        "crest": (vp / vr) if vr > 1e-3 else None,
        "curve_bar": curve,
        "curve_dev_bar": (p - curve) if curve is not None else None,
        "q_lpm": q, "p_bar": p,
    }
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def diagnose(z, reading, status, cfg):
    """Returns None when there is nothing to explain, else the ranked fault hypotheses."""
    d = cfg["diagnosis"]
    if reading["float"] == 0:
        fault = d["faults"]["dry_run"]
        return {"fault": "dry_run", "label": fault["label"], "confidence": 1.0,
                "action": fault["action"], "basis": "float switch LOW (interlock)",
                "ranking": [{"fault": "dry_run", "label": fault["label"], "similarity": 1.0}]}
    if z is None or status not in ("WARNING", "CRITICAL"):
        return None
    clip = cfg["scorer"]["z_clip"]
    zc = {k: max(-clip, min(clip, v)) for k, v in z.items()}
    zn = math.sqrt(sum(v * v for v in zc.values())) or 1.0
    ranking = []
    for key, fault in d["faults"].items():
        sig = fault.get("signature")
        if not sig:
            continue
        sn = math.sqrt(sum(v * v for v in sig.values()))
        sim = sum(zc.get(k, 0.0) * w for k, w in sig.items()) / (zn * sn)
        ranking.append({"fault": key, "label": fault["label"], "similarity": round(max(0.0, sim), 3)})
    ranking.sort(key=lambda r: -r["similarity"])
    best = ranking[0]
    if best["similarity"] < d["min_similarity"]:
        return {"fault": "unclassified", "label": "Unclassified anomaly", "confidence": best["similarity"],
                "action": d["unclassified_action"], "basis": "no known signature matches well",
                "ranking": ranking[:4]}
    fault = d["faults"][best["fault"]]
    top = sorted(zc.items(), key=lambda kv: -abs(kv[1]))[:3]
    basis = ", ".join(f"{k} z={v:+.1f}" for k, v in top)
    return {"fault": best["fault"], "label": fault["label"], "confidence": best["similarity"],
            "action": fault["action"], "basis": basis, "ranking": ranking[:4]}
