@echo off
cd /d "%~dp0"
if exist VRSMonitor.exe (
  VRSMonitor.exe
) else (
  python app.py
)
pause
