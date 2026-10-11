@echo off
setlocal
chcp 65001 >nul
title Private Line - Installation Check
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Check-Install.ps1"
set "rc=%errorlevel%"
echo.
if not "%rc%"=="0" (
 echo [NOT INSTALLED] Repair the installation by running INSTALL-WINDOWS.cmd.
) else (
 echo [INSTALLED] Private Line and Xray passed local verification.
)
echo.
pause
exit /b %rc%
