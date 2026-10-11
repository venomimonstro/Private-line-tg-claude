# Private Line Windows installer/repair, Windows PowerShell 5.1, current user.
# Clear confirmation: all steps must pass, GUI readiness must be observed.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Add-Type -AssemblyName System.Windows.Forms
$InstallDir = Join-Path $env:LOCALAPPDATA "PrivateLineWindows"
$SourceClient = Join-Path $PSScriptRoot "PrivateLine.ps1"
$InstalledClient = Join-Path $InstallDir "PrivateLine.ps1"
$XrayExe = Join-Path $InstallDir "xray.exe"
$InstallerLog = Join-Path $InstallDir "install-events.txt"
$ReadyFile = Join-Path $InstallDir "gui-ready.json"
$Build = "1.1.0"
$Passed = @()

function Milestone([string]$code, [string]$message) {
    $script:Passed += $code
    Write-Host ("[OK] " + $message) -ForegroundColor Green
    Add-Content -LiteralPath $InstallerLog -Encoding UTF8 -Value ($code + ": OK")
}
function ErrorMessage([string]$reason) {
    $safe = ($reason -replace '[\r\n]', ' ')
    if ($safe.Length -gt 200) { $safe = $safe.Substring(0,200) }
    Write-Host ("[ERROR] " + $safe) -ForegroundColor Red
    [void][System.Windows.Forms.MessageBox]::Show(
        ("Установка НЕ завершена." + [Environment]::NewLine + [Environment]::NewLine + $safe + [Environment]::NewLine + [Environment]::NewLine + "Повторите запуск INSTALL-WINDOWS.cmd." + [Environment]::NewLine + "Диагностика: " + $InstallerLog),
        "Private Line | Ошибка установки",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    )
}
function Check-Xray([string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    try {
        $out = & $path version 2>&1 | Select-Object -First 1
        return ($LASTEXITCODE -eq 0 -and ("$out" -match '^Xray 26\.6\.27(\s|$)'))
    } catch { return $false }
}
function Install-Xray {
    if (Check-Xray $XrayExe) {
        Milestone "XRAY_VERIFIED" "Xray 26.6.27 already installed. Download skipped."
        return
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "UNSUPPORTED_WINDOWS_ARCHITECTURE: This kit requires 64-bit Windows."
    }
    Write-Host "[2/5] Downloading Xray 26.6.27 from XTLS GitHub..." -ForegroundColor Cyan
    $sourceUrl = "https://github.com/XTLS/Xray-core/releases/download/v26.6.27/Xray-windows-64.zip"
    $tempZip = Join-Path $env:TEMP ("private-line-" + [Guid]::NewGuid().ToString("N") + ".zip")
    $tempFolder = Join-Path $env:TEMP ("private-line-" + [Guid]::NewGuid().ToString("N"))
    $newExe = Join-Path $InstallDir "xray.new.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $sourceUrl -OutFile $tempZip -UseBasicParsing -MaximumRedirection 5
        if ((Get-Item -LiteralPath $tempZip).Length -lt 1000000) { throw "XRAY_DOWNLOAD_INCOMPLETE" }
        Expand-Archive -LiteralPath $tempZip -DestinationPath $tempFolder -Force
        $candidate = Get-ChildItem -LiteralPath $tempFolder -Filter "xray.exe" -Recurse -File | Select-Object -First 1
        if (-not $candidate) { throw "XRAY_MISSING_FROM_ARCHIVE" }
        Copy-Item -LiteralPath $candidate.FullName -Destination $newExe -Force
        if (-not (Check-Xray $newExe)) { throw "XRAY_VERSION_MISMATCH" }
        Move-Item -LiteralPath $newExe -Destination $XrayExe -Force
        if (-not (Check-Xray $XrayExe)) { throw "XRAY_INSTALL_VERIFICATION_FAILED" }
        Milestone "XRAY_VERIFIED" "Xray 26.6.27 installed and verified."
    } finally {
        Remove-Item -LiteralPath $tempZip -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $tempFolder -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $newExe -Force -ErrorAction SilentlyContinue
    }
}

try {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Set-Content -LiteralPath $InstallerLog -Value ("Private Line setup " + $Build + " | " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) -Encoding UTF8
    Write-Host "Private Line | INSTALL / REPAIR" -ForegroundColor Cyan
    Write-Host "[1/5] Checking extracted installer files..." -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $SourceClient -PathType Leaf)) {
        throw "PACKAGE_INCOMPLETE: Extract ALL files from the downloaded ZIP before installing."
    }
    $clientText = [IO.File]::ReadAllText($SourceClient, [Text.Encoding]::UTF8)
    # Windows PowerShell 5.1 requires a BOM to decode Cyrillic scripts reliably.
    [IO.File]::WriteAllText($InstalledClient, $clientText, (New-Object System.Text.UTF8Encoding($true)))
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($InstalledClient, [ref]$tokens, [ref]$parseErrors) | Out-Null
    if ($parseErrors -and $parseErrors.Count -gt 0) {
        throw ("CLIENT_SCRIPT_SYNTAX: " + $parseErrors[0].ErrorId)
    }
    Milestone "FILES_VERIFIED" "Application files installed, PowerShell syntax valid."

    Install-Xray

    Write-Host "[3/5] Creating launcher and desktop shortcut..." -ForegroundColor Cyan
    $launcher = Join-Path $InstallDir "PrivateLine.cmd"
    $launcherLines = @(
        "@echo off",
        "title Private Line VPN",
        'powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0PrivateLine.ps1"',
        "if errorlevel 1 (echo The Private Line client failed to start. & pause)"
    )
    [IO.File]::WriteAllLines($launcher, $launcherLines, [Text.Encoding]::ASCII)
    $desktop = [Environment]::GetFolderPath("DesktopDirectory")
    if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) { throw "DESKTOP_DIRECTORY_UNAVAILABLE" }
    $shortcutFile = Join-Path $desktop "Private Line.lnk"
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutFile)
    $shortcut.TargetPath = $launcher
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Description = "Private Line VPN for Windows"
    $shortcut.Save()
    if (-not (Test-Path -LiteralPath $shortcutFile)) { throw "SHORTCUT_CREATION_FAILED" }
    Milestone "SHORTCUT_VERIFIED" "Private Line shortcut created on your desktop."

    Write-Host "[4/5] Verifying installation..." -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $InstalledClient) -or -not (Check-Xray $XrayExe)) {
        throw "INSTALL_FILE_VERIFICATION_FAILED"
    }
    Milestone "INSTALL_VERIFIED" "Files, Xray, and desktop shortcut verified."

    Write-Host "[5/5] Opening Private Line..." -ForegroundColor Cyan
    Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue
    # Using the same launcher as the desktop shortcut validates the real path.
    $appProcess = Start-Process -FilePath "powershell.exe" -ArgumentList ("-NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File " + [char]34 + $InstalledClient + [char]34) -WorkingDirectory $InstallDir -PassThru
    $opened = $false
    for ($i = 0; $i -lt 35; $i++) {
        Start-Sleep -Milliseconds 300
        if (Test-Path -LiteralPath $ReadyFile) {
            try {
                $marker = Get-Content -LiteralPath $ReadyFile -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($marker.version -eq $Build -and [int]$marker.pid -gt 0) {
                    $guiProcess = Get-Process -Id ([int]$marker.pid) -ErrorAction SilentlyContinue
                    if ($guiProcess -and -not $guiProcess.HasExited) {
                        $opened = $true
                        break
                    }
                }
            } catch {}
        }
        if ($appProcess.HasExited) { break }
    }
    if (-not $opened) { throw "WINDOW_NOT_CONFIRMED: application did not signal that its window opened." }
    Milestone "GUI_VERIFIED" "Private Line window launched successfully."
    $record = @{
        version = $Build
        xray_version = "26.6.27"
        installed_at = (Get-Date).ToString("o")
        desktop_shortcut = $shortcutFile
        status = "installed"
        gui_confirmed = $true
    } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText((Join-Path $InstallDir "install-status.json"),
                           $record, (New-Object System.Text.UTF8Encoding($false)))

    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host "    PRIVATE LINE IS INSTALLED AND LAUNCHED" -ForegroundColor Green
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host "Desktop shortcut: Private Line"
    Write-Host "Now paste your VLESS key into the application and press Connect."
    [void][System.Windows.Forms.MessageBox]::Show(
        ("Установка завершена!" + [Environment]::NewLine + [Environment]::NewLine + "Xray 26.6.27 проверен." + [Environment]::NewLine + "Ярлык Private Line создан на рабочем столе." + [Environment]::NewLine + "Окно клиента запущено." + [Environment]::NewLine + [Environment]::NewLine + "Важно: VPN подключится только после ввода ключа и нажатия «Подключить»."),
        "Private Line | Установлено",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    )
    exit 0
} catch {
    $reason = $_.Exception.Message
    try { Add-Content -LiteralPath $InstallerLog -Encoding UTF8 -Value ("INSTALL_FAILED: " + $reason.Substring(0, [Math]::Min(200, $reason.Length))) } catch {}
    ErrorMessage $reason
    exit 1
}
