@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Private Line - Windows Installer
echo.
echo ================================================================
echo               PRIVATE LINE - INSTALL / REPAIR
echo ================================================================
echo.
if not exist "%~dp0Install-PrivateLine.ps1" goto :missing
if not exist "%~dp0PrivateLine.ps1" goto :missing
powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0Install-PrivateLine.ps1"
set "rc=%errorlevel%"
echo.
if not "%rc%"=="0" (
  echo [FAILED] Installation or application launch was not confirmed.
  echo See the error message above. Do not assume the VPN is installed.
) else (
  echo [SUCCESS] Installation verified; open the Private Line desktop shortcut.
)
echo.
echo Press any key to close this window.
pause >nul
exit /b %rc%

:missing
echo [ERROR] Installer files not found.
echo Extract ALL files from private-line-windows.zip into a folder first.
echo Do not run INSTALL-WINDOWS.cmd from inside an archive preview.
echo.
pause
exit /b 2
