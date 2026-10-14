#!/usr/bin/env bash
# Opens the dashboard full-screen once the backend answers. Started at login (install_pi.sh --kiosk).
URL=${PUMP_URL:-http://localhost:8000}
for _ in $(seq 60); do curl -sf "$URL/api/state" >/dev/null && break; sleep 1; done
BROWSER=$(command -v chromium-browser || command -v chromium)
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --check-for-update-interval=31536000 "$URL"
