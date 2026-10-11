@echo off
setlocal
chcp 65001 >nul
echo Private Line Windows - install
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-PrivateLine.ps1"
if errorlevel 1 (
  echo Installation failed.
  pause
  exit /b 1
)
echo Installation completed.
pause
