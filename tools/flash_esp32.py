"""
One-command ESP32 flashing: compile + upload firmware/esp32_pump_node and verify it.

  python tools/flash_esp32.py                 # auto-detect the board, flash, verify
  python tools/flash_esp32.py --port COM5     # explicit port
  python tools/flash_esp32.py --compile-only  # just build (no board needed)
  (Windows: double-click flash.bat)

Steps:
  1. find arduino-cli; install the esp32 core and the OneWire / DallasTemperature libraries if missing
  2. compile the sketch
  3. find the ESP32 on USB (CP210x / CH340 / FTDI / native USB)
  4. if the dashboard backend is running on this machine it holds the port in auto mode,
     so ask it to switch to the simulator for a moment, which releases the port
  5. upload (retrying once with a slower baud if the first attempt fails)
  6. read the board's serial output for a few seconds and validate the JSON with the
     same schema the Pi uses; report sensor faults in plain words
  7. give the port back to the dashboard (auto mode), which then goes LIVE by itself
"""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.config import CONFIG  # noqa: E402
from backend.schema import Invalid, parse_line  # noqa: E402
from backend.sources import USB_BRIDGES  # noqa: E402

SKETCH = os.path.join(ROOT, "firmware", "esp32_pump_node")
BUILD = os.path.join(ROOT, "build", "esp32")
CORE_URL = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"


def say(msg):
    print(f"[flash] {msg}", flush=True)


def find_cli():
    for c in (shutil.which("arduino-cli"), r"C:\Program Files\Arduino CLI\arduino-cli.exe",
              os.path.expanduser("~/bin/arduino-cli"), "/usr/local/bin/arduino-cli"):
        if c and os.path.exists(c):
            return c
    sys.exit("arduino-cli not found. Windows: winget install ArduinoSA.CLI   "
             "Linux/Pi: curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh")


def run(cli, *args, check=True, quiet=False):
    p = subprocess.run([cli, *args], capture_output=True, text=True)
    if not quiet and p.stdout.strip():
        print("\n".join(p.stdout.strip().splitlines()[-15:]))
    if check and p.returncode:
        print(p.stderr.strip()[-2000:])
        sys.exit(f"arduino-cli {' '.join(args[:2])} failed")
    return p


def ensure_toolchain(cli):
    cores = run(cli, "core", "list", quiet=True).stdout
    if "esp32:esp32" not in cores:
        say("installing the ESP32 board package (one time, ~400 MB)…")
        run(cli, "config", "add", "board_manager.additional_urls", CORE_URL, check=False, quiet=True)
        run(cli, "config", "set", "network.connection_timeout", "900s", check=False, quiet=True)
        run(cli, "core", "update-index", quiet=True)
        run(cli, "core", "install", "esp32:esp32")
    libs = run(cli, "lib", "list", quiet=True).stdout
    missing = [lib for lib in ("OneWire", "DallasTemperature") if lib not in libs]
    if missing:
        say(f"installing libraries: {', '.join(missing)}")
        run(cli, "lib", "install", *missing)


def find_port():
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports() if p.vid is not None]
    ports.sort(key=lambda p: p.vid not in USB_BRIDGES)
    for p in ports:
        say(f"found {p.device}: {USB_BRIDGES.get(p.vid, p.description)}")
    return ports[0].device if ports else None


async def backend_cmd(cmd, value):
    """Send one command to a dashboard backend on this machine; returns its previous mode or None."""
    try:
        import websockets
        url = f"ws://127.0.0.1:{CONFIG['server']['port']}/ws"
        async with websockets.connect(url, open_timeout=2) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 3))
            await ws.send(json.dumps({"cmd": cmd, "value": value}))
            await asyncio.sleep(1.5)            # serial reader thread closes the port
            return hello["state"].get("mode")
    except Exception:
        return None


def verify(port, seconds=10):
    import serial
    say(f"reading {port} for up to {seconds} s to verify…")
    try:
        ser = serial.Serial(port, 115200, timeout=0.5)
    except Exception as e:
        say(f"could not open {port} to verify: {e}")
        return False
    good = faults = 0
    end = time.monotonic() + seconds
    with ser:
        while time.monotonic() < end and good < 3:
            line = ser.readline()
            if not line.strip():
                continue
            text = line.decode("utf-8", "replace").strip()
            print("   ", text[:140])
            if text.startswith("# sensor fault"):
                faults += 1
            try:
                parse_line(line, CONFIG)
                good += 1
            except Invalid:
                pass
    if good:
        say(f"OK: {good} valid pump messages received — firmware is working")
        return True
    if faults:
        say("board runs, but reports a SENSOR FAULT and sends no data.")
        say("check the DS18B20 (GPIO4 + 4.7 kΩ pull-up) and ADXL345 (SDA 21 / SCL 22, 3.3 V) wiring")
    else:
        say("no valid messages seen. Press the board's EN/RST button and re-run with --verify-only")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--fqbn", default="esp32:esp32:esp32", help="e.g. esp32:esp32:esp32s3 for an ESP32-S3")
    ap.add_argument("--compile-only", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()

    if a.verify_only:
        port = a.port or find_port()
        sys.exit(0 if port and verify(port) else 1)

    cli = find_cli()
    ensure_toolchain(cli)
    say(f"compiling {os.path.relpath(SKETCH, ROOT)} for {a.fqbn}…")
    run(cli, "compile", "--fqbn", a.fqbn, "--output-dir", BUILD, SKETCH)
    if a.compile_only:
        say(f"build OK -> {os.path.relpath(BUILD, ROOT)}")
        return

    port = a.port or find_port()
    if not port:
        sys.exit("no ESP32 found on USB. Plug it in (data-capable cable), install the CP210x/CH340 driver if "
                 "Windows shows no COM port, then run this again.")

    prev_mode = asyncio.run(backend_cmd("set_source", "sim"))
    if prev_mode:
        say(f"dashboard backend found (mode {prev_mode}); switched it to simulator to free {port}")
    try:
        say(f"uploading to {port}…")
        p = run(cli, "upload", "-p", port, "--fqbn", a.fqbn, "--input-dir", BUILD, check=False)
        if p.returncode:
            say("first attempt failed; retrying at 115200 baud. If it fails again, hold BOOT while it says 'Connecting…'")
            p = run(cli, "upload", "-p", port, "--fqbn", f"{a.fqbn}:UploadSpeed=115200", "--input-dir", BUILD, check=False)
        if p.returncode:
            print(p.stderr.strip()[-1500:])
            sys.exit("upload failed")
        say("upload complete")
        time.sleep(1.5)
        ok = verify(port)
    finally:
        if prev_mode:
            asyncio.run(backend_cmd("set_source", "auto" if prev_mode in ("auto", "sim") else prev_mode))
            say("dashboard switched back; it will detect the ESP32 and go LIVE within a few seconds")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
