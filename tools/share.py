"""
Share this rig with someone anywhere: starts the backend (if it isn't running) and opens a free
Cloudflare quick tunnel, then prints the links to send.

  python tools/share.py                       (Windows: double-click share.bat)
  python tools/share.py --vercel my-pump.vercel.app

Needs cloudflared:  Windows  winget install --id Cloudflare.cloudflared
                    Pi/Linux https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
The link works while this window stays open. Close it (Ctrl+C) to stop sharing.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES = [r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe",
              os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Links\cloudflared.exe"), "/usr/local/bin/cloudflared",
              "/usr/bin/cloudflared"]


def backend_up(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--vercel", help="your Vercel domain, e.g. my-pump.vercel.app, to also print a Vercel link")
    a = ap.parse_args()

    exe = shutil.which("cloudflared") or next((c for c in CANDIDATES if os.path.exists(c)), None)
    if not exe:
        print("cloudflared is not installed. Install it once, then run this again:")
        print("  Windows:  winget install --id Cloudflare.cloudflared")
        print("  Pi/Linux: see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
        sys.exit(1)

    backend = None
    if backend_up(a.port):
        print(f"Backend already running on port {a.port}.")
    else:
        print(f"Starting the backend on port {a.port} …")
        backend = subprocess.Popen([sys.executable, "-m", "backend", "--port", str(a.port)], cwd=ROOT)
        for _ in range(40):
            if backend_up(a.port):
                break
            time.sleep(0.5)
        else:
            sys.exit(f"The backend did not start on port {a.port}. Is another app using it? Try --port 8010.")

    print("Opening a public tunnel …")
    tunnel = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{a.port}"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    try:
        for line in tunnel.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                url = m.group(0)
                break
        if not url:
            sys.exit("cloudflared did not return a link. Check your internet connection and try again.")
        host = url.removeprefix("https://")
        print()
        print("=" * 66)
        print("  Send this link. It opens the live dashboard of this rig:")
        print(f"    {url}")
        if a.vercel:
            print("  Or the Vercel version of the same rig:")
            print(f"    https://{a.vercel.removeprefix('https://').rstrip('/')}/?pi={host}")
        print("  Anyone with the link can see the data and use the simulator controls.")
        print("  Keep this window open while sharing; Ctrl+C stops it.")
        print("=" * 66)
        for _ in tunnel.stdout:      # keep draining cloudflared's log
            pass
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.terminate()
        if backend:
            backend.terminate()


if __name__ == "__main__":
    main()
