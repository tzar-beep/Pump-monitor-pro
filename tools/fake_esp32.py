"""
Fake ESP32: writes the firmware's JSON lines to a serial port (or a TCP socket)
so the live path can be tested without the rig.

  # Linux / Pi, with a virtual port pair from socat (see tools/test_live_path.sh):
  python tools/fake_esp32.py --port /tmp/ttyESP32 --scenario blockage

  # Any OS, no virtual serial driver needed: serve on TCP and point the backend at
  # --serial-port socket://127.0.0.1:7777  (pyserial URL, same SerialSource code path)
  python tools/fake_esp32.py --tcp 7777

Options inject faults the Pi must catch: --bad-every N sends a malformed/out-of-range
line every N messages, --drop-every N skips a sequence number, --stall-after S stops
sending for --stall-s seconds (STALE + reconnect test).
"""
import argparse
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.config import CONFIG  # noqa: E402
from backend.simulator import Simulator  # noqa: E402

BAD_LINES = [
    '{"ts": 1, "rpm": "fast"}',                                     # wrong type + missing fields
    "garbage-without-json",                                         # debug print / noise
    '{"ts":%d,"rpm":9999,"temp_c":38,"current_a":2,"flow_lpm":18,'
    '"pressure_bar":2,"vib_x":0.1,"vib_y":0.1,"vib_z":0.1,"float":1}',  # rpm out of range
    '{"ts":%d,"rpm":1480,"temp_c":38,"current_a":2,"flow_lpm":18',     # truncated
]


def lines(args):
    sim = Simulator(CONFIG, seed=args.seed)
    sim.set_scenario(args.scenario)
    sim.set_valve(args.valve)
    n = 0
    while True:
        n += 1
        if args.sensor_fault:          # like the real firmware with an unwired sensor: no data, only this line
            yield f"# sensor fault: temp={0 if 'temp' in args.sensor_fault else 1} adxl={0 if 'adxl' in args.sensor_fault else 1}", True
            continue
        msg = sim.step(1.0 / args.rate)
        if args.drop_every and n % args.drop_every == 0:
            continue                                   # lost message -> seq gap
        if args.bad_every and n % args.bad_every == 0:
            bad = BAD_LINES[(n // args.bad_every) % len(BAD_LINES)]
            yield ((bad % msg["ts"]) if "%d" in bad else bad), False
        yield json.dumps(msg, separators=(",", ":")), True
        if args.count and n >= args.count:
            return


def run(write, args):
    t0 = time.monotonic()
    stalled = False
    for line, real in lines(args):
        if args.stall_after and not stalled and time.monotonic() - t0 > args.stall_after:
            print(f"[fake_esp32] stalling for {args.stall_s} s", flush=True)
            time.sleep(args.stall_s)
            stalled = True
        write((line + "\n").encode())
        if not args.quiet:
            print(line, flush=True)
        if real:
            time.sleep(1.0 / args.rate)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--port", help="serial device to write to, e.g. /tmp/ttyESP32 or COM6")
    g.add_argument("--tcp", type=int, help="serve the stream on this TCP port (socket:// URL)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--rate", type=float, default=1.0)
    ap.add_argument("--scenario", default="normal", choices=list(CONFIG["simulator"]["scenarios"]))
    ap.add_argument("--valve", type=float, default=0)
    ap.add_argument("--bad-every", type=int, default=0)
    ap.add_argument("--sensor-fault", choices=["temp", "adxl", "temp,adxl"], help="emulate an undetected sensor")
    ap.add_argument("--drop-every", type=int, default=0)
    ap.add_argument("--stall-after", type=float, default=0)
    ap.add_argument("--stall-s", type=float, default=8)
    ap.add_argument("--count", type=int, default=0, help="stop after N messages (0 = forever)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.port:
        import serial
        with serial.Serial(args.port, args.baud) as ser:
            print(f"[fake_esp32] writing to {args.port} @ {args.baud}", flush=True)
            run(ser.write, args)
        return

    srv = socket.create_server(("127.0.0.1", args.tcp))
    print(f"[fake_esp32] serving on socket://127.0.0.1:{args.tcp}", flush=True)
    while True:
        conn, addr = srv.accept()
        print(f"[fake_esp32] client {addr}", flush=True)
        try:
            run(conn.sendall, args)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            print("[fake_esp32] client went away", flush=True)
        finally:
            conn.close()
        if args.count:
            return


if __name__ == "__main__":
    main()
