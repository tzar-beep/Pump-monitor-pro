"""
Interchangeable data sources. Each one only produces raw text lines (one ESP32
JSON object per line) and hands them to `emit(line)`. Parsing, validation,
features and scoring all happen downstream in the shared Processor, so the
browser cannot tell the sources apart except by the reported source name.

Source interface:
    .kind       "sim" | "serial" | "mqtt" | "replay"
    .live       True for real hardware transports
    .detail     human-readable endpoint, e.g. "/dev/ttyUSB0 @ 115200"
    .connected  transport is up (independent of whether data is flowing)
    .error      last transport error or None
    async run(emit)   runs until cancelled
"""
import asyncio
import csv
import json
import os
import threading
import time

from .simulator import Simulator


class SimSource:
    kind, live = "sim", False

    def __init__(self, cfg, sim: Simulator, is_paused):
        self.sim = sim
        self.period = 1.0 / cfg["simulator"]["rate_hz"]
        self.is_paused = is_paused
        self.detail = f"built-in simulator @ {cfg['simulator']['rate_hz']:g} Hz"
        self.connected, self.error = True, None

    async def run(self, emit):
        nxt = time.monotonic()
        while True:
            nxt += self.period
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))
            if self.is_paused():
                nxt = time.monotonic()      # freeze simulated time while paused
                continue
            await emit(self.sim.line(self.period))


# USB-serial bridges found on ESP32 boards. Unknown USB VIDs are still probed, after these.
USB_BRIDGES = {0x10C4: "CP210x", 0x1A86: "CH340/CH9102", 0x0403: "FTDI", 0x303A: "ESP32 native USB",
               0x2341: "Arduino", 0x239A: "Adafruit"}


class SerialSource:
    """
    USB serial via pyserial, in a reader thread. Reconnects on error and on a stalled stream.

    port = "auto": each scan lists the USB serial ports (ports without a USB VID, such as
    Bluetooth or legacy UARTs, are skipped: opening them can hang or upset other devices),
    opens each one and keeps the first that sends a line passing full message validation.
    The last good port is tried first, so a replug reconnects in one probe, and a port the
    OS renamed (ttyUSB0 -> ttyUSB1, COM5 -> COM7) is found on the next scan.
    """
    kind, live = "serial", True

    def __init__(self, cfg):
        sc = cfg["serial"]
        self.cfg = cfg
        self.auto = str(sc["port"]).lower() == "auto"
        self.port = None if self.auto else sc["port"]
        self.baud = sc["baud"]
        self.timeout = sc["read_timeout_s"]
        self.reconnect = sc["reconnect_delay_s"]
        self.max_line = sc["max_line_bytes"]
        self.probe_s = sc.get("probe_timeout_s", 4.0)
        self.extra = list(sc.get("extra_candidates") or [])
        self.stall_s = cfg["link"]["stale_after_s"] * 2
        self.device = None          # bridge name when auto-detected, e.g. "CP210x"
        self.scan = []              # last scan results: [{"port", "desc", "result"}]
        self.connected, self.error = False, None
        self._stop = threading.Event()

    @property
    def detail(self):
        if self.auto and not self.port:
            return f"auto-detect · scanning USB serial ports @ {self.baud}"
        how = f" · auto-detected {self.device}" if self.auto else ""
        return f"{self.port} @ {self.baud} 8N1{how}"

    # ------------------------------------------------------------ auto-detection
    def candidates(self):
        try:
            from serial.tools import list_ports
            found = [(p.device, USB_BRIDGES.get(p.vid, p.description or "USB serial"), p.vid in USB_BRIDGES)
                     for p in list_ports.comports() if p.vid is not None]
        except Exception:
            found = []
        found.sort(key=lambda c: (c[0] != self.port, not c[2], c[0]))   # last good port, then known bridges
        return [(dev, desc) for dev, desc, _ in found] + [(url, "extra candidate") for url in self.extra]

    def _probe(self, dev):
        """Open dev and wait for one valid message. Returns (serial, first_line, rest) or (None, reason, None)."""
        import serial
        from .schema import Invalid, parse_line
        try:
            ser = serial.serial_for_url(dev, baudrate=self.baud, timeout=0.3)
        except Exception as e:
            msg = str(e)
            if "Permission denied" in msg or "Errno 13" in msg:
                return None, "permission denied: run  sudo usermod -aG dialout $USER  and log in again", None
            if "Access is denied" in msg or "PermissionError" in msg or "busy" in msg.lower():
                return None, "port busy: close Arduino Serial Monitor / other serial programs", None
            if "socket://" in dev:
                return None, "not reachable", None
            return None, f"cannot open: {msg[:70]}", None
        buf, got_bytes = b"", False
        deadline = time.monotonic() + self.probe_s    # the ESP32 resets when the port opens; allow for boot
        try:
            while time.monotonic() < deadline and not self._stop.is_set():
                chunk = ser.read(ser.in_waiting or 1)
                if not chunk:
                    continue
                got_bytes, buf = True, buf + chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        parse_line(line, self.cfg)
                    except Invalid:
                        continue                   # boot chatter, "# ready" lines, half lines
                    return ser, line, buf
                if len(buf) > self.max_line:
                    buf = b""
        except Exception as e:
            ser.close()
            return None, f"read error ({str(e)[:50]})", None
        ser.close()
        if not got_bytes:
            return None, "silent: no data (firmware flashed? sensors wired?)", None
        return None, "sends data, but not valid pump JSON (wrong firmware or baud?)", None

    def _open(self):
        """Returns (serial, already-read first line or None, leftover buffer)."""
        import serial
        if not self.auto:
            return serial.serial_for_url(self.port, baudrate=self.baud, timeout=self.timeout), None, b""
        results = []
        for dev, desc in self.candidates():
            if self._stop.is_set():
                break
            ser, first, rest = self._probe(dev)
            if ser:
                ser.timeout = self.timeout
                results.append({"port": dev, "desc": desc, "result": "ESP32 pump node ✓"})
                self.scan, self.port, self.device = results, dev, desc
                return ser, first, rest
            results.append({"port": dev, "desc": desc, "result": first})
        self.scan = results
        raise IOError("no ESP32 found on USB" if results else "no USB serial ports present: plug in the ESP32")

    # ------------------------------------------------------------ reader
    def _reader(self, loop, q):
        while not self._stop.is_set():
            try:
                ser, first, buf = self._open()
            except Exception as e:  # SerialException, OSError, ValueError for bad URLs
                self.connected = False
                self.error = str(e) if self.auto else f"open failed: {e}"
                self._stop.wait(self.reconnect)
                continue
            self.connected, self.error = True, None
            if first:
                loop.call_soon_threadsafe(q.put_nowait, first)
            last_rx = time.monotonic()
            try:
                while not self._stop.is_set():
                    chunk = ser.read(ser.in_waiting or 1)
                    if chunk:
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            last_rx = time.monotonic()
                            loop.call_soon_threadsafe(q.put_nowait, line)
                        if len(buf) > self.max_line:     # garbage without newlines
                            loop.call_soon_threadsafe(q.put_nowait, buf)
                            buf = b""
                    elif time.monotonic() - last_rx > self.stall_s:
                        raise IOError(f"no data for {self.stall_s:.0f} s, reopening port")
            except Exception as e:
                self.error = str(e)
            finally:
                self.connected = False
                try:
                    ser.close()
                except Exception:
                    pass
            self._stop.wait(self.reconnect)

    async def run(self, emit):
        q = asyncio.Queue()
        th = threading.Thread(target=self._reader, args=(asyncio.get_running_loop(), q), daemon=True)
        th.start()
        try:
            while True:
                await emit(await q.get())
        finally:
            self._stop.set()


class AutoSource:
    """
    Plug-and-play mode (the default). Runs the simulator as a clearly labelled fallback
    while SerialSource(port="auto") scans USB. The first valid ESP32 message stops the
    simulator for good and awaits on_live(serial_source); from then on only the real
    device feeds the pipeline. If the device is unplugged later the link goes
    DISCONNECTED - it never silently reverts to simulated data - and reconnects on replug.
    """

    def __init__(self, cfg, sim, is_paused, on_live, fallback_sim=True):
        self.serial = SerialSource(cfg)
        self.simsrc = SimSource(cfg, sim, is_paused) if fallback_sim else None
        self.on_live = on_live
        self.is_live = not fallback_sim

    @property
    def kind(self):
        return "serial" if self.is_live else "sim"

    @property
    def live(self):
        return self.is_live

    @property
    def connected(self):
        return self.serial.connected if self.is_live else True

    @property
    def error(self):
        return self.serial.error if self.is_live else None

    @property
    def detail(self):
        return self.serial.detail if self.is_live else "simulator · waiting for ESP32 on USB"

    async def run(self, emit):
        sim_task = asyncio.create_task(self.simsrc.run(emit)) if self.simsrc else None

        async def serial_emit(line):
            if not self.is_live:
                self.is_live = True
                sim_task.cancel()
                await self.on_live(self.serial)
            await emit(line)

        try:
            await self.serial.run(serial_emit)
        finally:
            if sim_task:
                sim_task.cancel()


class MqttSource:
    """ESP32 publishing over Wi-Fi. paho-mqtt runs its own network thread and reconnects itself."""
    kind, live = "mqtt", True

    def __init__(self, cfg):
        self.mc = dict(cfg["mqtt"])
        self.mc["username"] = os.environ.get("PUMP_MQTT_USER", self.mc["username"])
        self.mc["password"] = os.environ.get("PUMP_MQTT_PASSWORD", self.mc["password"])
        self.detail = f"mqtt://{self.mc['host']}:{self.mc['port']}  {self.mc['topic']}"
        self.connected, self.error = False, None

    async def run(self, emit):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.error = "paho-mqtt not installed (pip install paho-mqtt)"
            await asyncio.Event().wait()
        loop, q, m = asyncio.get_running_loop(), asyncio.Queue(), self.mc
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=m["client_id"])
        if m["username"]:
            client.username_pw_set(m["username"], m["password"])
        if m["tls"]:
            client.tls_set()
        client.reconnect_delay_set(1, 30)

        def on_connect(c, _u, _f, rc, _p):
            self.connected = not rc.is_failure
            self.error = None if self.connected else f"connect refused: {rc}"
            if self.connected:
                c.subscribe(m["topic"], qos=0)

        def on_disconnect(_c, _u, _f, rc, _p):
            self.connected = False
            self.error = f"disconnected: {rc}"

        def on_message(_c, _u, msg):
            for line in msg.payload.splitlines() or [b""]:
                loop.call_soon_threadsafe(q.put_nowait, line)

        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        try:
            client.connect_async(m["host"], m["port"], keepalive=m["keepalive_s"])
            client.loop_start()
            while True:
                await emit(await q.get())
        finally:
            client.loop_stop()
            client.disconnect()


REPLAY_FIELDS = ["ts", "seq", "rpm", "temp_c", "current_a", "flow_lpm", "pressure_bar",
                 "vib_x", "vib_y", "vib_z", "vib_peak", "float"]


def csv_row_to_line(row):
    """Rebuild the ESP32 JSON line from a logged CSV row (extra columns are ignored)."""
    msg = {}
    for k in REPLAY_FIELDS:
        v = row.get(k)
        if v in (None, ""):
            continue
        try:
            msg[k] = int(v) if k in ("ts", "seq", "float") else float(v)
        except ValueError:
            msg[k] = v          # keep it; the validator will reject and count it
    return json.dumps(msg, separators=(",", ":"))


class ReplaySource:
    """Feeds a recorded CSV through the same pipeline, paced by its 'ts' column."""
    kind, live = "replay", False

    def __init__(self, cfg, path, is_paused, on_restart):
        rc = cfg["replay"]
        self.path, self.speed, self.loop = path, rc["speed"], rc["loop"]
        self.is_paused, self.on_restart = is_paused, on_restart
        self.detail = f"{os.path.basename(path)} ×{self.speed:g}"
        self.connected, self.error = False, None
        self.position = (0, 0)

    async def run(self, emit):
        try:
            with open(self.path, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        except OSError as e:
            self.error = f"cannot read {self.path}: {e}"
            await asyncio.Event().wait()
        if not rows:
            self.error = "file has no rows"
            await asyncio.Event().wait()
        self.connected = True
        while True:
            prev_ts = None
            for i, row in enumerate(rows):
                self.position = (i + 1, len(rows))
                try:
                    ts = int(float(row.get("ts") or 0))
                except ValueError:
                    ts = prev_ts or 0
                dt = 1.0 if prev_ts is None else (ts - prev_ts) / 1000.0
                await asyncio.sleep(max(0.0, min(5.0, dt)) / self.speed)
                while self.is_paused():
                    await asyncio.sleep(0.1)
                prev_ts = ts
                await emit(csv_row_to_line(row))
            if not self.loop:
                self.connected, self.error = False, "replay finished"
                await asyncio.Event().wait()
            self.on_restart()
