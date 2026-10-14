"""
End-to-end test of the LIVE path, runnable on any OS (no rig, no virtual serial driver):

  fake_esp32 (TCP) -> pyserial socket:// URL -> SerialSource -> Processor -> CSV + WebSocket -> this client

It injects malformed lines, sequence gaps and an 8 s stall, then asserts that:
  * frames arrive with source=serial and link=LIVE, carrying features/score/health/status
  * invalid lines and dropped messages are counted
  * the stall is reported as STALE and the source reconnects back to LIVE
  * the CSV log is written, and replaying it through the same pipeline works

  python tools/test_e2e.py            (from the project root, venv active)

On the Pi with a real pty pair use tools/test_live_path.sh instead (socat).
"""
import asyncio
import csv
import json
import os
import subprocess
import sys
import time

import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TCP, HTTP = 7777, 8765


def spawn(*args):
    return subprocess.Popen([PY, *args], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


async def main():
    fake = spawn("tools/fake_esp32.py", "--tcp", str(TCP), "--quiet", "--seed", "3",
                 "--bad-every", "6", "--drop-every", "9", "--stall-after", "12", "--stall-s", "9")
    time.sleep(1.0)
    server = spawn("-m", "backend", "--source", "serial", "--serial-port", f"socket://127.0.0.1:{TCP}",
                   "--port", str(HTTP), "--host", "127.0.0.1", "--log")
    ok = True

    def check(cond, what):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
        ok &= bool(cond)

    try:
        ws = None
        for _ in range(40):
            try:
                ws = await websockets.connect(f"ws://127.0.0.1:{HTTP}/ws")
                break
            except OSError:
                await asyncio.sleep(0.25)
        assert ws, "backend did not start"
        hello = json.loads(await ws.recv())
        check(hello["type"] == "hello" and hello["state"]["source"] == "serial", "hello received, source = serial")

        frames, links, state = [], [], None
        t_end = time.monotonic() + 34
        while time.monotonic() < t_end:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.5))
            except asyncio.TimeoutError:
                continue
            if m["type"] == "frame":
                frames.append(m)
            elif m["type"] == "state":
                state = m
                if not links or links[-1] != m["link"]:
                    links.append(m["link"])
                    print(f"    link -> {m['link']}  (age {m['last_valid_age_s']} s, err {m['source_error']})")

        c = state["counters"]
        print(f"    frames={len(frames)} counters={ {k: c[k] for k in ('received', 'valid', 'invalid', 'dropped')} }"
              f" reasons={c['invalid_by_reason']}")
        check(len(frames) >= 15, f"received >= 15 validated frames ({len(frames)})")
        scored = [f for f in frames if f["score"] is not None]
        check(scored and all(0 <= f["score"] <= 1 and f["health"] is not None for f in scored),
              "frames carry score in [0,1] and health index")
        check(any(f["status"] == "HEALTHY" for f in frames), "status reaches HEALTHY on normal data")
        check(c["invalid"] > 0, "malformed / out-of-range lines rejected and counted")
        check(c["dropped"] > 0, "sequence gaps counted as dropped")
        check("live" in links, "link reported LIVE")
        check("stale" in links or "disconnected" in links, "stall reported as STALE/DISCONNECTED")
        if "stale" in links or "disconnected" in links:
            i = max(links.index("stale") if "stale" in links else 99, 0)
            i = min(i, links.index("disconnected") if "disconnected" in links else 99)
            check("live" in links[i:], "stream reconnected back to LIVE after the stall")

        log_path = state["logging"]["path"]
        with open(log_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        check(len(rows) >= 15 and rows[0]["status"], f"CSV log written ({len(rows)} rows) -> {log_path}")

        # Replay the CSV we just logged through the same pipeline.
        rel = os.path.relpath(log_path, ROOT).replace("\\", "/")
        await ws.send(json.dumps({"cmd": "set_replay_file", "value": rel}))
        await ws.send(json.dumps({"cmd": "set_source", "value": "replay"}))
        replay_frames, replay_link = 0, None
        t_end = time.monotonic() + 9
        while time.monotonic() < t_end:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.5))
            except asyncio.TimeoutError:
                continue
            if m["type"] == "frame":
                replay_frames += 1
            elif m["type"] == "state" and m["source"] == "replay":
                replay_link = m["link"]
        check(replay_frames >= 5 and replay_link == "replay", f"replay mode streams the logged CSV ({replay_frames} frames)")

        await ws.send(json.dumps({"cmd": "set_logging", "value": False}))
        await ws.close()
    finally:
        for p in (server, fake):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    print("E2E PASSED" if ok else "E2E FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
