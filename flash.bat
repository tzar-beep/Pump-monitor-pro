@echo off
rem Flash the ESP32 pump-node firmware. Plug the ESP32 into USB, then double-click this file.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  python -m venv .venv || (echo Python 3 not found. Install it from python.org. & pause & exit /b 1)
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
.venv\Scripts\python tools\flash_esp32.py %*
pause
