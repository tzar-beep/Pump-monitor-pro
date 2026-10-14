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
import time
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .config import SENSORS
from .csvlog import CsvLogger
from .processor import Processor, make_alert
from .scorer import DemoScorer, load_scorer
from .simulator import Simulator
from .sources import MqttSource, ReplaySource, SerialSource, SimSource

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web", "index.html")
SOURCES = ("sim", "serial", "mqtt", "replay")
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
        self.sim = Simulator(cfg)
        self.logger = CsvLogger(rel(cfg["logging"]["dir"]))
        self.clients = set()
        self.history = deque(maxlen=cfg["history_keep"])
        self.alerts = deque(maxlen=cfg["alerts_keep"])
        self.paused = False
        self.source = None
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
        self.cfg["source"] = kind
        self.processor.reset()
        self.last_valid = None
        self.was_fresh = False
        self.rx_times.clear()
        self.history.clear()
        self.task = asyncio.create_task(self._run_source(new))
        if announce:
            await self._alert(make_alert(time.time(), "info", f"Source switched to {kind.upper()} ({new.detail})"))
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
        res = self.processor.ingest(line, now)
        if not res["ok"]:
            return
        frame = res["frame"]
        self.last_valid = now
        self.rx_times.append(now)
        self.history.append(frame)
        self.logger.write(frame, self.source.kind)
        await self.broadcast(frame)
        for a in res["alerts"]:
            await self._alert(a)

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
                if link in ("stale", "disconnected") and self.link in ("sim", "replay", "live"):
                    await self._alert(make_alert(now, "warn", f"Data stream {LINK_LABEL[link]} ({self.source.detail})"))
                elif fresh and self.link in ("stale", "disconnected") and self.was_fresh:
                    await self._alert(make_alert(now, "info", f"Data stream restored ({LINK_LABEL[link]})"))
            self.was_fresh = self.was_fresh or fresh
            self.link = link
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
            "source": src.kind, "source_detail": src.detail, "source_live": src.live,
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
        }

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
            await self._alert(make_alert(time.time(), "info", f"Simulator scenario → {label}"))
        elif cmd == "set_valve":
            self.sim.set_valve(float(val))
        elif cmd == "set_pump":
            self.sim.set_pump(bool(val))
            await self._alert(make_alert(time.time(), "info", f"Pump {'START' if val else 'STOP'} command (simulator)"))
        elif cmd == "set_paused":
            self.paused = bool(val)
            await self._alert(make_alert(time.time(), "info", "Stream paused" if self.paused else "Stream resumed"))
        elif cmd == "set_logging":
            if val:
                self.logger.start(self.source.kind)
                await self._alert(make_alert(time.time(), "info", f"CSV logging → {self.logger.path}"))
            else:
                self.logger.stop()
                await self._alert(make_alert(time.time(), "info", "CSV logging stopped"))
        elif cmd == "set_replay_file":
            if val not in self.replay_files():
                raise ValueError("unknown replay file")
            self.replay_file = val
            if self.source.kind == "replay":
                await self.set_source("replay")
        elif cmd == "reset_counters":
            self.processor.reset_counters()
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
