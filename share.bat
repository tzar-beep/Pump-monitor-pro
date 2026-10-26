@echo off
rem Share this rig: starts the backend and prints a public link you can send to anyone.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  python -m venv .venv || (echo Python 3 not found. Install it from python.org. & pause & exit /b 1)
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
where cloudflared >nul 2>nul || (
  echo Installing cloudflared, one time only...
  winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
)
.venv\Scripts\python tools\share.py %*
pause
