# Local, read-only installation verification. No VPN secrets displayed.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
$directory = Join-Path $env:LOCALAPPDATA "PrivateLineWindows"
$client = Join-Path $directory "PrivateLine.ps1"
$xray = Join-Path $directory "xray.exe"
$launcher = Join-Path $directory "PrivateLine.cmd"
$status = Join-Path $directory "install-status.json"
$shortcut = Join-Path ([Environment]::GetFolderPath("DesktopDirectory")) "Private Line.lnk"
$missing = @()
if (-not (Test-Path -LiteralPath $client -PathType Leaf)) { $missing += "APPLICATION_MISSING" }
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { $missing += "LAUNCHER_MISSING" }
if (-not (Test-Path -LiteralPath $status -PathType Leaf)) { $missing += "INSTALL_STATUS_MISSING" }
if (-not (Test-Path -LiteralPath $shortcut -PathType Leaf)) { $missing += "SHORTCUT_MISSING" }
if (-not (Test-Path -LiteralPath $xray -PathType Leaf)) {
    $missing += "XRAY_MISSING"
} else {
    try {
        $version = & $xray version 2>&1 | Select-Object -First 1
        if ($LASTEXITCODE -ne 0 -or "$version" -notmatch '^Xray 26\.6\.27(\s|$)') {
            $missing += "XRAY_VERSION_INVALID"
        }
    } catch { $missing += "XRAY_START_FAILED" }
}
if ($missing.Count -gt 0) {
    $message = "УСТАНОВКА НЕ ЗАВЕРШЕНА." + [Environment]::NewLine +
      "Непройденные проверки: " + ($missing -join ", ") + [Environment]::NewLine +
      "Запустите INSTALL-WINDOWS.cmd из полностью распакованного архива."
    Write-Host $message -ForegroundColor Red
    [void][System.Windows.Forms.MessageBox]::Show($message, "Private Line | Не установлено")
    exit 1
}
$message = "УСТАНОВЛЕНО." + [Environment]::NewLine +
  "Xray 26.6.27: OK" + [Environment]::NewLine +
  "Windows-клиент: OK" + [Environment]::NewLine +
  "Ярлык на рабочем столе: OK" + [Environment]::NewLine +
  [Environment]::NewLine + "Это не означает, что VPN подключён. Откройте Private Line и нажмите «Подключить»."
Write-Host $message -ForegroundColor Green
[void][System.Windows.Forms.MessageBox]::Show($message, "Private Line | Установлено")
exit 0
