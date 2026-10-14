#!/usr/bin/env bash
# Live-path test over a REAL virtual serial port pair (Linux / Raspberry Pi OS).
#
#   sudo apt install socat
#   ./tools/test_live_path.sh            # then open http://<pi>:8000 and watch LIVE DATA
#
# socat creates two linked ptys: the fake ESP32 writes to /tmp/ttyESP32, the backend
# reads /tmp/ttyPI exactly as it would read /dev/ttyUSB0. Ctrl+C stops everything.
# Pass extra fake_esp32 flags after the script name, e.g.:
#   ./tools/test_live_path.sh --scenario imbalance --bad-every 10 --stall-after 30
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}

command -v socat >/dev/null || { echo "socat not found: sudo apt install socat"; exit 1; }

socat -d -d pty,raw,echo=0,link=/tmp/ttyESP32 pty,raw,echo=0,link=/tmp/ttyPI 2>/tmp/socat.log &
SOCAT=$!
trap 'kill $SOCAT ${FAKE:-} ${SRV:-} 2>/dev/null || true' EXIT
for _ in $(seq 20); do [ -e /tmp/ttyPI ] && break; sleep 0.1; done

"$PY" tools/fake_esp32.py --port /tmp/ttyESP32 --quiet --bad-every 8 --drop-every 15 "$@" &
FAKE=$!
"$PY" -m backend --source serial --serial-port /tmp/ttyPI --log &
SRV=$!

# Smoke check: the HTTP state endpoint must report LIVE with valid messages within 10 s.
for _ in $(seq 20); do
  sleep 0.5
  STATE=$(curl -s localhost:8000/api/state || true)
  if echo "$STATE" | grep -q '"link":"live"'; then
    echo "LIVE path OK:"; echo "$STATE" | "$PY" -c 'import json,sys; s=json.load(sys.stdin); print(" source", s["source_detail"]); print(" counters", {k: s["counters"][k] for k in ("received","valid","invalid","dropped")}); print(" csv", s["logging"]["path"])'
    echo "Dashboard: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000   (Ctrl+C to stop)"
    wait $SRV
    exit 0
  fi
done
echo "FAILED: backend never reported a LIVE link"; echo "$STATE"; exit 1
