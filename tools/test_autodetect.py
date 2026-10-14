"""
Plug-and-play test for the default "auto" source, runnable on any OS without hardware.

A fake ESP32 on a TCP port stands in for a USB device (the auto-scan also probes
--probe URLs, through the same SerialSource code as real COM / ttyUSB ports):

  1. backend starts in auto mode, no device  -> simulator, SIMULATED, scan lists the port as unavailable
  2. "plug in" (start fake ESP32)            -> detected, switched to serial, link LIVE, toast alert
  3. "unplug"  (kill it)                      -> STALE / DISCONNECTED, no fallback to simulated data
  4. "replug"  (start it again)               -> reconnected, LIVE again, toast alert

  python tools/test_autodetect.py
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TCP, HTTP = 7781, 8767


def spawn(*args):
    return subprocess.Popen([PY, *args], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()


async def main():
    server = spawn("-m", "backend", "--source", "auto", "--probe", f"socket://127.0.0.1:{TCP}",
                   "--port", str(HTTP), "--host", "127.0.0.1")
    ok, fake = True, None

    def check(cond, what):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
        ok &= bool(cond)

    async def watch(ws, seconds, until=None):
        """Collect messages for up to `seconds`; stop early when until(state) is true."""
        state, alerts, frames = None, [], 0
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
            except asyncio.TimeoutError:
                continue
            if m["type"] == "state":
                state = m
                if until and until(m):
                    break
            elif m["type"] == "alert":
                alerts.append(m["alert"])
            elif m["type"] == "frame":
                frames += 1
        return state, alerts, frames

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
        check(hello["state"]["mode"] == "auto", "starts in AUTO mode")

        print("  1) nothing plugged in")
        st, _, frames = await watch(ws, 6)
        check(st["source"] == "sim" and st["link"] == "sim" and frames > 0, "simulated data flows while waiting")
        scan = st["serial"]["scan"]
        print(f"     scan: {scan}")
        check(any(r["port"].startswith("socket://") and "✓" not in r["result"] for r in scan),
              "scan lists the (unplugged) port with a reason")

        print("  2) plug in")
        t0 = time.monotonic()
        fake = spawn("tools/fake_esp32.py", "--tcp", str(TCP), "--quiet", "--seed", "5")
        st, alerts, _ = await watch(ws, 20, until=lambda s: s["link"] == "live")
        dt = time.monotonic() - t0
        check(st["source"] == "serial" and st["link"] == "live", f"switched to LIVE automatically ({dt:.1f} s after plug-in)")
        check(any(a.get("toast") == "good" and "ESP32 detected" in a["msg"] for a in alerts), "toast: 'ESP32 detected … LIVE data'")
        check(st["serial"]["connected"] and st["serial"]["port"].startswith("socket://"), f"device shown: {st['source_detail']}")
        st, _, frames = await watch(ws, 8)
        check(frames >= 5 and st["link"] == "live", f"live frames keep flowing ({frames} in 8 s)")

        print("  3) unplug")
        stop(fake)
        st, alerts, _ = await watch(ws, 15, until=lambda s: s["link"] in ("stale", "disconnected"))
        check(st["link"] in ("stale", "disconnected") and st["source"] == "serial",
              f"link {st['link'].upper()} — does not fall back to simulated data")
        st, alerts2, frames = await watch(ws, 5)
        check(frames == 0, "no frames while unplugged")
        check(any(a.get("toast") == "crit" for a in alerts + alerts2), "toast: data stream lost")

        print("  4) replug")
        t0 = time.monotonic()
        fake = spawn("tools/fake_esp32.py", "--tcp", str(TCP), "--quiet", "--seed", "6")
        st, alerts, _ = await watch(ws, 25, until=lambda s: s["link"] == "live")
        check(st["link"] == "live", f"reconnected to LIVE automatically ({time.monotonic() - t0:.1f} s after replug)")
        await ws.close()
    finally:
        if fake:
            stop(fake)
        stop(server)
    print("AUTODETECT PASSED" if ok else "AUTODETECT FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
