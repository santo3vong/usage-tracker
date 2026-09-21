@echo off
setlocal
title Usage Tracker
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-background.ps1"
if errorlevel 1 (
  echo.
  echo Khong the khoi dong Usage Tracker. Xem runtime\server.err.log de biet chi tiet.
  pause
  exit /b 1
)
start "" "http://127.0.0.1:5051/"
endlocal
