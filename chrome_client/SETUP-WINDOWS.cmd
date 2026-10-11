@echo off
setlocal
chcp 65001 >nul
echo ==========================================================
echo Private Line VPN - Google Chrome / Windows installer
echo ==========================================================
echo.
echo 1. Open chrome://extensions in Chrome.
echo 2. Enable Developer mode, choose Load unpacked.
echo 3. Select the extension folder from this ZIP.
echo 4. Copy the 32-letter ID of your extension.
echo.
set /p PL_EXTENSION_ID=Paste extension ID: 
if "%PL_EXTENSION_ID%"=="" (
  echo Extension ID is required.
  pause
  exit /b 2
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0native\install_windows.ps1" -ExtensionId "%PL_EXTENSION_ID%"
if errorlevel 1 (
  echo.
  echo Installation failed. Read README_RU.md for prerequisites.
  pause
  exit /b 1
)
echo.
echo Installation completed. Restart Chrome, then paste your VLESS key.
pause
