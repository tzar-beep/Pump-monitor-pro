"""
Pi-side server: owns the active data source, runs the Processor, logs to CSV and
pushes everything to browsers over a single WebSocket (/ws).

Browser <- {"type": "hello" | "frame" | "state" | "alert" | "ack"}
Browser -> {"cmd": "set_source" | "set_scenario" | "set_valve" | "set_pump" |
            "set_paused" | "set_logging" | "set_replay_file" | "reset_counters", "value": ...}
"""
import asyncio
import contextlib
import glob
import json
import os
import platform
import time
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .alarms import AlarmManager
from .config import SENSORS
from .csvlog import CsvLogger
from .processor import Processor, make_alert
from .scorer import DemoScorer, load_scorer
from .simulator import Simulator
from .sources import AutoSource, MqttSource, ReplaySource, SerialSource, SimSource

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web", "index.html")
SOURCES = ("auto", "sim", "serial", "mqtt", "replay")
SOURCE_WORD = {"auto": "auto (ESP32 when plugged in)", "sim": "simulator", "serial": "ESP32 over USB",
               "mqtt": "ESP32 over Wi-Fi", "replay": "replay"}
LINK_LABEL = {"sim": "SIMULATED", "replay": "REPLAY", "live": "LIVE", "stale": "STALE",
              "disconnected": "DISCONNECTED", "paused": "PAUSED"}


def rel(path):
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        if cfg["scorer"].get("model_path"):
            cfg["scorer"]["model_path"] = rel(cfg["scorer"]["model_path"])
        self.scorer = load_scorer(cfg)
        self.processor = Processor(cfg, self.scorer, SENSORS)
        self.alarm_mgr = AlarmManager(cfg, SENSORS)
        self.proc_ms = deque(maxlen=300)
        self.started = time.time()
        try:
            import psutil
            psutil.cpu_percent(interval=None)        # prime the CPU counter
            self.psutil = psutil
        except ImportError:
            self.psutil = None
        self.sim = Simulator(cfg)
        self.logger = CsvLogger(rel(cfg["logging"]["dir"]))
        self.clients = set()
        self.history = deque(maxlen=cfg["history_keep"])
        self.alerts = deque(maxlen=cfg["alerts_keep"])
        self.paused = False
        self.source = None
        self.mode = cfg["source"]
        self.task = None
        self.last_valid = None
        self.link = None
        self.was_fresh = False
        self.rx_times = deque(maxlen=50)
        self.replay_file = cfg["replay"]["file"]

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        if self.cfg["logging"]["enabled"]:
            self.logger.start(self.cfg["source"])
        await self.set_source(self.cfg["source"], announce=False)
        self.ticker = asyncio.create_task(self._tick())

    async def stop(self):
        for t in (self.task, getattr(self, "ticker", None)):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self.logger.stop()

    def _make_source(self, kind):
        if kind == "auto":
            return AutoSource(self.cfg, self.sim, lambda: self.paused, self._went_live,
                              fallback_sim=self.cfg["auto"]["fallback_sim"])
        if kind == "sim":
            return SimSource(self.cfg, self.sim, lambda: self.paused)
        if kind == "serial":
            return SerialSource(self.cfg)
        if kind == "mqtt":
            return MqttSource(self.cfg)
        if kind == "replay":
            return ReplaySource(self.cfg, rel(self.replay_file), lambda: self.paused, self.processor.reset)
        raise ValueError(f"unknown source {kind!r}")

    async def set_source(self, kind, announce=True):
        if kind not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        new = self._make_source(kind)
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.source = new
        self.mode = kind
        self.cfg["source"] = kind
        self.processor.reset()
        self.alarm_mgr.clear()
        self.last_valid = None
        self.was_fresh = False
        self.rx_times.clear()
        self.history.clear()
        self.task = asyncio.create_task(self._run_source(new))
        if announce:
            await self._alert(make_alert(time.time(), "info", f"Data source: {SOURCE_WORD[kind]}"))
            await self.broadcast(self.hello())

    async def _went_live(self, ser):
        """Auto mode: the first valid ESP32 message arrived. Drop the simulated history."""
        self.processor.reset()
        self.alarm_mgr.clear()
        self.history.clear()
        self.rx_times.clear()
        self.last_valid = None
        self.paused = False
        if self.logger.enabled:
            self.logger.start("serial")          # keep real data in its own CSV
        a = make_alert(time.time(), "info", f"ESP32 connected on {ser.port} — showing live data")
        a["toast"] = "good"
        await self._alert(a)
        await self.broadcast(self.hello())

    async def _run_source(self, src):
        try:
            await src.run(self.on_line)
        except asyncio.CancelledError:
            raise
        except Exception as e:      # never let a source bug kill the server
            src.connected, src.error = False, f"source crashed: {e!r}"

    # ------------------------------------------------------------------ data path
    async def on_line(self, line):
        if self.paused and self.source.live:
            self.processor.counters["skipped"] += 1
            return
        now = time.time()
        t0 = time.perf_counter()
        res = self.processor.ingest(line, now)
        if not res["ok"]:
            return
        frame = res["frame"]
        events = self.alarm_mgr.evaluate(self.alarm_mgr.frame_conditions(frame), now)
        self.proc_ms.append((time.perf_counter() - t0) * 1000.0)
        frame["proc_ms"] = round(self.proc_ms[-1], 3)
        self.last_valid = now
        self.rx_times.append(now)
        self.history.append(frame)
        self.logger.write(frame, self.source.kind)
        await self.broadcast(frame)
        for a in res["alerts"]:
            await self._alert(a)
        if events:
            await self.broadcast(self.state(now))

    async def _alert(self, a):
        self.alerts.append(a)
        await self.broadcast({"type": "alert", "alert": a})

    def _link_state(self, now):
        src = self.source
        age = None if self.last_valid is None else now - self.last_valid
        fresh = age is not None and age <= self.cfg["link"]["stale_after_s"]
        if self.paused:
            return "paused"
        if fresh:
            return {"sim": "sim", "replay": "replay"}.get(src.kind, "live")
        return "stale" if src.connected else "disconnected"

    async def _tick(self):
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            link = self._link_state(now)
            fresh = link in ("sim", "replay", "live")
            if link != self.link:
                a = None
                if link in ("stale", "disconnected") and self.link in ("sim", "replay", "live"):
                    a = make_alert(now, "warn", "Lost connection to the ESP32" if self.source.live else "Data stopped")
                    a["toast"] = "crit" if self.source.live else None
                elif fresh and self.link in ("stale", "disconnected") and self.was_fresh:
                    a = make_alert(now, "info", "Connection restored")
                    a["toast"] = "good" if self.source.live else None
                if a:
                    await self._alert(a)
            self.was_fresh = self.was_fresh or fresh
            self.link = link
            lost = self.source.live and link in ("stale", "disconnected") and self.was_fresh
            self.alarm_mgr.evaluate({"COMM-101.FAIL": (lost, "ESP32 communication lost", "HIGH", None)}, now)
            await self.broadcast(self.state(now))

    # ------------------------------------------------------------------ messages
    def state(self, now=None):
        now = now or time.time()
        src = self.source
        rate = 0.0
        if len(self.rx_times) >= 2 and now - self.rx_times[-1] < 5:
            rate = (len(self.rx_times) - 1) / max(1e-6, self.rx_times[-1] - self.rx_times[0])
        return {
            "type": "state", "t": now,
            "mode": self.mode,
            "source": src.kind, "source_detail": src.detail, "source_live": src.live,
            "serial": self._serial_info(),
            "source_connected": src.connected, "source_error": src.error,
            "link": self._link_state(now),
            "last_valid_age_s": None if self.last_valid is None else round(now - self.last_valid, 1),
            "msg_rate_hz": round(rate, 2),
            "paused": self.paused,
            "sim": self.sim.state() if src.kind == "sim" else None,
            "replay": {"file": self.replay_file, "files": self.replay_files(),
                       "position": getattr(src, "position", None)},
            "logging": {"enabled": self.logger.enabled, "path": self.logger.path,
                        "dir": self.logger.dir, "rows": self.logger.rows},
            "counters": self.processor.counters,
            "scorer": {"name": self.scorer.name, "kind": self.scorer.kind},
            "clients": len(self.clients),
            "alarms": self.alarm_mgr.listing(),
            "alarm_summary": self.alarm_mgr.summary(),
            "edge": self.edge_stats(now),
        }

    def edge_stats(self, now):
        """Health of the edge node itself (the Pi): load, memory, SoC temperature, processing latency."""
        ms = list(self.proc_ms)
        c = self.processor.counters
        d = {"host": platform.node(), "os": f"{platform.system()} {platform.machine()}",
             "python": platform.python_version(), "uptime_s": round(now - self.started),
             "proc_ms_avg": round(sum(ms) / len(ms), 3) if ms else None,
             "proc_ms_max": round(max(ms), 3) if ms else None,
             "quality_pct": round(100.0 * c["valid"] / c["received"], 1) if c["received"] else None,
             "cpu_pct": None, "mem_pct": None, "cpu_temp_c": None, "load1": None}
        if hasattr(os, "getloadavg"):
            d["load1"] = round(os.getloadavg()[0], 2)
        if self.psutil:
            d["cpu_pct"] = self.psutil.cpu_percent(interval=None)
            d["mem_pct"] = self.psutil.virtual_memory().percent
            temps = getattr(self.psutil, "sensors_temperatures", lambda: {})() or {}
            for key in ("cpu_thermal", "coretemp", "k10temp", "soc_thermal"):
                if temps.get(key):
                    d["cpu_temp_c"] = temps[key][0].current
                    break
        if d["cpu_temp_c"] is None:
            try:                                   # Raspberry Pi without psutil
                with open("/sys/class/thermal/thermal_zone0/temp") as fh:
                    d["cpu_temp_c"] = int(fh.read()) / 1000.0
            except OSError:
                pass
        return d

    def _serial_info(self):
        ser = getattr(self.source, "serial", self.source)
        if not isinstance(ser, SerialSource):
            return None
        return {"auto": ser.auto, "port": ser.port, "device": ser.device, "scan": ser.scan,
                "connected": ser.connected, "error": ser.error, "baud": ser.baud}

    def hello(self):
        c = self.cfg
        return {
            "type": "hello",
            "config": {
                "features": c["features"], "sensors": SENSORS, "window": c["window"],
                "status": c["status"], "scorer": c["scorer"], "baseline": c["baseline"],
                "ranges": c["ranges"], "sources": SOURCES,
                "scenarios": {k: {"label": v["label"], "signature": v["signature"]}
                              for k, v in c["simulator"]["scenarios"].items()},
                "link": c["link"], "history_keep": c["history_keep"],
                "engineering": c["engineering"],
                "faults": {k: v["label"] for k, v in c["diagnosis"]["faults"].items()},
            },
            "history": list(self.history),
            "alerts": list(self.alerts),
            "state": self.state(),
        }

    def replay_files(self):
        files = []
        for pat in ("data/*.csv", os.path.join(self.cfg["logging"]["dir"], "*.csv")):
            files += sorted(os.path.relpath(p, ROOT).replace("\\", "/") for p in glob.glob(rel(pat)))
        return files

    async def broadcast(self, obj):
        if not self.clients:
            return
        text = json.dumps(obj, separators=(",", ":"))
        dead = []
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=2)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # ------------------------------------------------------------------ commands
    async def command(self, msg):
        cmd, val = msg.get("cmd"), msg.get("value")
        sim_only = {"set_scenario", "set_valve", "set_pump"}
        if cmd in sim_only and self.source.kind != "sim":
            raise ValueError("scenario, valve and pump controls drive the simulator; switch source to SIM")
        if cmd == "set_source":
            await self.set_source(str(val))
        elif cmd == "set_scenario":
            self.sim.set_scenario(str(val))
            label = self.cfg["simulator"]["scenarios"][val]["label"]
            await self._alert(make_alert(time.time(), "info", f"Simulating: {label}"))
        elif cmd == "set_valve":
            self.sim.set_valve(float(val))
        elif cmd == "set_pump":
            self.sim.set_pump(bool(val))
            await self._alert(make_alert(time.time(), "info", f"Pump {'start' if val else 'stop'} pressed"))
        elif cmd == "set_paused":
            self.paused = bool(val)
            await self._alert(make_alert(time.time(), "info", "Stream paused" if self.paused else "Stream resumed"))
        elif cmd == "set_logging":
            if val:
                self.logger.start(self.source.kind)
                await self._alert(make_alert(time.time(), "info", "Saving readings to CSV"))
            else:
                self.logger.stop()
                await self._alert(make_alert(time.time(), "info", "Stopped saving readings"))
        elif cmd == "set_replay_file":
            if val not in self.replay_files():
                raise ValueError("unknown replay file")
            self.replay_file = val
            if self.source.kind == "replay":
                await self.set_source("replay")
        elif cmd == "reset_counters":
            self.processor.reset_counters()
        elif cmd in ("ack_alarm", "ack_all"):
            done = self.alarm_mgr.ack(None if cmd == "ack_all" else str(val))
            if done:
                await self._alert(make_alert(time.time(), "info", f"Acknowledged: {', '.join(done)}"))
        else:
            raise ValueError(f"unknown command {cmd!r}")
        await self.broadcast(self.state())


def create_app(cfg):
    hub = Hub(cfg)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await hub.start()
        print(f"[pump-console] source={cfg['source']} scorer={hub.scorer.name}")
        yield
        await hub.stop()

    app = FastAPI(title="Pump Health Console", lifespan=lifespan)
    app.state.hub = hub

    @app.get("/")
    async def index():
        return FileResponse(WEB, headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    async def api_state():
        return JSONResponse(hub.state())

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        await ws.send_text(json.dumps(hub.hello(), separators=(",", ":")))
        hub.clients.add(ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    await hub.command(msg)
                    await ws.send_text(json.dumps({"type": "ack", "cmd": msg.get("cmd"), "ok": True}))
                except Exception as e:
                    await ws.send_text(json.dumps({"type": "ack", "ok": False, "error": str(e)}))
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)

    return app
