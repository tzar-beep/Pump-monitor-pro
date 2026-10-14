"""python -m backend [--source sim|serial|mqtt|replay] [--serial-port COM5] ..."""
import argparse
import copy
import json
import os

import uvicorn

from .config import CONFIG
from .server import create_app


def deep_merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def main():
    ap = argparse.ArgumentParser(description="Pump Health Console — edge backend")
    ap.add_argument("--config", help="JSON file deep-merged over backend/config.py")
    ap.add_argument("--source", choices=["auto", "sim", "serial", "mqtt", "replay"])
    ap.add_argument("--serial-port", help='"auto" (default) or e.g. /dev/ttyUSB0, COM5, socket://host:port')
    ap.add_argument("--probe", action="append", default=[], help="extra port/URL for the auto-scan to probe (repeatable)")
    ap.add_argument("--no-sim-fallback", action="store_true", help="auto mode: show nothing until an ESP32 is found")
    ap.add_argument("--baud", type=int)
    ap.add_argument("--mqtt-host")
    ap.add_argument("--mqtt-topic")
    ap.add_argument("--replay-file")
    ap.add_argument("--replay-speed", type=float)
    ap.add_argument("--model", help="trained Isolation Forest JSON (tools/train_iforest.py)")
    ap.add_argument("--log", action="store_true", help="start with CSV logging enabled")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()

    cfg = copy.deepcopy(CONFIG)
    if a.config:
        with open(a.config, encoding="utf-8") as fh:
            deep_merge(cfg, json.load(fh))
    env = os.environ.get
    cfg["source"] = a.source or env("PUMP_SOURCE") or cfg["source"]
    cfg["serial"]["port"] = a.serial_port or env("PUMP_SERIAL_PORT") or cfg["serial"]["port"]
    cfg["serial"]["baud"] = a.baud or cfg["serial"]["baud"]
    cfg["serial"]["extra_candidates"] = cfg["serial"]["extra_candidates"] + a.probe
    if a.no_sim_fallback:
        cfg["auto"]["fallback_sim"] = False
    cfg["mqtt"]["host"] = a.mqtt_host or env("PUMP_MQTT_HOST") or cfg["mqtt"]["host"]
    cfg["mqtt"]["topic"] = a.mqtt_topic or cfg["mqtt"]["topic"]
    cfg["replay"]["file"] = a.replay_file or cfg["replay"]["file"]
    cfg["replay"]["speed"] = a.replay_speed or cfg["replay"]["speed"]
    cfg["scorer"]["model_path"] = a.model or cfg["scorer"]["model_path"]
    cfg["logging"]["enabled"] = a.log or cfg["logging"]["enabled"]
    host = a.host or cfg["server"]["host"]
    port = a.port or cfg["server"]["port"]

    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
