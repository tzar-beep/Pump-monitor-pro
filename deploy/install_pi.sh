#!/usr/bin/env bash
# One-time setup on the Raspberry Pi 5 (Raspberry Pi OS Bookworm).
#
#   ./deploy/install_pi.sh            # backend starts on boot (auto-detect mode)
#   ./deploy/install_pi.sh --kiosk    # ...and the dashboard opens full-screen on the Pi's display
#
# After this, plugging the ESP32 into any USB port is the only step: the backend is
# already running, finds the board within a few seconds and the dashboard goes LIVE.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR=$(pwd)
APP_USER=$(id -un)
KIOSK=0; [ "${1:-}" = "--kiosk" ] && KIOSK=1

echo "==> Python environment"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "==> Serial port permission (dialout group)"
sudo usermod -aG dialout "$APP_USER"

echo "==> systemd service: pump-console"
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__APP_USER__|$APP_USER|g" deploy/pump-console.service \
  | sudo tee /etc/systemd/system/pump-console.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now pump-console
chmod +x deploy/kiosk.sh

if [ "$KIOSK" = 1 ]; then
  echo "==> Kiosk: open the dashboard full-screen at login"
  sudo apt-get install -y -qq chromium-browser >/dev/null 2>&1 || sudo apt-get install -y -qq chromium
  mkdir -p ~/.config/autostart
  cat > ~/.config/autostart/pump-console-kiosk.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Pump Health Console
Exec=$APP_DIR/deploy/kiosk.sh
X-GNOME-Autostart-enabled=true
EOF
  # Bookworm with the labwc compositor reads its own autostart file.
  if command -v labwc >/dev/null; then
    mkdir -p ~/.config/labwc
    grep -q kiosk.sh ~/.config/labwc/autostart 2>/dev/null || echo "$APP_DIR/deploy/kiosk.sh &" >> ~/.config/labwc/autostart
  fi
fi

IP=$(hostname -I | awk '{print $1}')
echo
echo "Done. Backend is running and starts on every boot."
echo "  Dashboard:  http://$IP:8000   (or http://localhost:8000 on the Pi)"
echo "  Logs:       journalctl -u pump-console -f"
echo "  Status:     systemctl status pump-console"
echo "Log out and back in once (or reboot) so the dialout group applies to your user shell."
