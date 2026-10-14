"""
Alarm management in the ISA-18.2 style used by SCADA systems.

An alarm is a *condition* with a tag (e.g. "AI-101.HIHI"), not a log line. Its
states are:
    ACTIVE + UNACK   raised, operator has not acknowledged      (shown flashing)
    ACTIVE + ACK     acknowledged, condition still present
    CLEAR  + UNACK   returned to normal before anyone acknowledged it
and it leaves the list once it is both cleared and acknowledged.
The state lives on the Pi, so every connected HMI sees the same alarm list.
"""

PRIORITY = {"HIGH": 1, "MED": 2, "LOW": 3}

# Instrument tags (ISA-5.1 style) for the per-channel deviation alarms.
CHANNEL_TAG = {"hall": "ST-101", "current": "IT-101", "temp": "TT-101", "flow": "FT-101",
               "pressure": "PT-101", "vib": "VT-101"}
CHANNEL_NAME = {"hall": "Speed", "current": "Motor current", "temp": "Motor temperature",
                "flow": "Flow", "pressure": "Outlet pressure", "vib": "Vibration"}


class AlarmManager:
    def __init__(self, cfg, sensors):
        self.cfg = cfg
        self.sensors = [s for s in sensors if s["id"] in CHANNEL_TAG]
        self.alarms = {}

    # ------------------------------------------------------------------ conditions
    def frame_conditions(self, frame):
        """Map one processed frame to {tag: (active, description, priority, value)}."""
        st, r = frame["status"], frame["reading"]
        score = frame["score"]
        sval = None if score is None else round(score, 3)
        cond = {
            "AI-101.HIHI": (st == "CRITICAL", "Anomaly score high-high (CRITICAL)", "HIGH", sval),
            "AI-101.HI": (st in ("WARNING", "CRITICAL"), "Anomaly score high (WARNING)", "MED", sval),
            "LSL-101.LO": (r["float"] == 0, "Inlet level low — dry-run interlock", "HIGH", r["float"]),
        }
        z = frame["z"]
        zc, db = self.cfg["status"]["channel_z_crit"], self.cfg["alarms"]["channel_deadband_z"]
        for s in self.sensors:
            tag = f"{CHANNEL_TAG[s['id']]}.DEV"
            if z is None or not frame["running"]:
                active = False
                zmax = None
            else:
                zmax = max(abs(z[k]) for k in s["features"])
                was = tag in self.alarms and self.alarms[tag]["active"]
                active = zmax >= (zc - db if was else zc)        # deadband on clear
            cond[tag] = (active, f"{CHANNEL_NAME[s['id']]} deviation from baseline", "MED",
                         None if zmax is None else round(zmax, 1))
        return cond

    # ------------------------------------------------------------------ state machine
    def evaluate(self, conditions, now):
        """Apply conditions; returns event strings for the log (raises and returns to normal)."""
        events = []
        for tag, (active, desc, prio, value) in conditions.items():
            a = self.alarms.get(tag)
            if active and (a is None or not a["active"]):
                if a is None:
                    a = self.alarms[tag] = {"tag": tag, "desc": desc, "priority": prio, "count": 0}
                a.update(active=True, acked=False, t_in=now, t_out=None, value=value, count=a["count"] + 1)
                events.append(("raise", a))
            elif active:
                a["value"] = value
            elif a is not None and a["active"]:
                a.update(active=False, t_out=now)
                events.append(("rtn", a))
                if a["acked"]:
                    del self.alarms[tag]
        return events

    def ack(self, tag=None):
        tags = [tag] if tag else list(self.alarms)
        done = []
        for t in tags:
            a = self.alarms.get(t)
            if a is None or a["acked"]:
                continue
            a["acked"] = True
            done.append(t)
            if not a["active"]:
                del self.alarms[t]
        return done

    def clear(self):
        self.alarms.clear()

    def listing(self):
        items = sorted(self.alarms.values(),
                       key=lambda a: (a["acked"], not a["active"], PRIORITY[a["priority"]], -a["t_in"]))
        return [dict(a) for a in items]

    def summary(self):
        al = self.alarms.values()
        return {"active": sum(a["active"] for a in al), "unacked": sum(not a["acked"] for a in al),
                "high_unacked": sum(not a["acked"] and a["priority"] == "HIGH" for a in al)}
