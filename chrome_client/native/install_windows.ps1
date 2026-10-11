# Private Line Chrome native client installation (per-user, no administrator rights).
# Run after loading the unpacked Chrome extension and copying its 32-letter ID:
# powershell -NoProfile -ExecutionPolicy Bypass -File .\native\install_windows.ps1 -ExtensionId aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
param(
    [Parameter(Mandatory=$true)][string]$ExtensionId,
    [string]$XrayPath = ""
)
$ErrorActionPreference = "Stop"
if ($ExtensionId -cnotmatch '^[a-p]{32}$') {
    throw "Invalid Chrome extension ID. Copy the ID from chrome://extensions."
}
$python = Get-Command py -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python 3 not found. Install Python 3 from https://www.python.org/downloads/windows/ and retry."
}
& py -3 -V
if ($LASTEXITCODE -ne 0) { throw "Python 3 launcher is not available." }

$InstallDir = Join-Path $env:LOCALAPPDATA "PrivateLineChrome"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$BridgeSource = Join-Path $PSScriptRoot "bridge.py"
if (-not (Test-Path -LiteralPath $BridgeSource)) { throw "bridge.py not found. Unpack the complete ZIP first." }
Copy-Item -LiteralPath $BridgeSource -Destination (Join-Path $InstallDir "bridge.py") -Force

$XrayExe = Join-Path $InstallDir "xray.exe"
if ($XrayPath) {
    if (-not (Test-Path -LiteralPath $XrayPath)) { throw "Specified Xray.exe not found." }
    Copy-Item -LiteralPath (Resolve-Path -LiteralPath $XrayPath).Path -Destination $XrayExe -Force
} elseif (-not (Test-Path -LiteralPath $XrayExe)) {
    $Url = "https://github.com/XTLS/Xray-core/releases/download/v26.6.27/Xray-windows-64.zip"
    $ZipFile = Join-Path $env:TEMP ("private-line-xray-" + [guid]::NewGuid().ToString() + ".zip")
    $ExtractDir = Join-Path $env:TEMP ("private-line-xray-" + [guid]::NewGuid().ToString())
    try {
        Write-Host "Downloading Xray 26.6.27 from official XTLS GitHub release..."
        Invoke-WebRequest -Uri $Url -OutFile $ZipFile -MaximumRedirection 5
        Expand-Archive -LiteralPath $ZipFile -DestinationPath $ExtractDir -Force
        $Source = Get-ChildItem -LiteralPath $ExtractDir -Filter "xray.exe" -Recurse -File | Select-Object -First 1
        if (-not $Source) { throw "Xray executable missing from the official release ZIP." }
        Copy-Item -LiteralPath $Source.FullName -Destination $XrayExe -Force
    } finally {
        Remove-Item -LiteralPath $ZipFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ExtractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
$VersionOutput = (& $XrayExe version 2>&1 | Select-Object -First 1)
if ($LASTEXITCODE -ne 0 -or "$VersionOutput" -notmatch '^Xray 26\.6\.27(\s|$)') {
    throw "Unexpected Xray version: $VersionOutput. Expected 26.6.27."
}
Set-Content -LiteralPath (Join-Path $InstallDir "xray-path.txt") -Value $XrayExe -Encoding UTF8

$CmdPath = Join-Path $InstallDir "host.cmd"
[System.IO.File]::WriteAllText($CmdPath, "@echo off`r`npy -3 -u `"%~dp0bridge.py`"`r`n", [System.Text.Encoding]::ASCII)
$Manifest = @{
    name = "ru.privateline.chrome"
    description = "Private Line VPN native Xray bridge"
    path = $CmdPath
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
} | ConvertTo-Json -Depth 4
$ManifestPath = Join-Path $InstallDir "native-host.json"
[System.IO.File]::WriteAllText($ManifestPath, $Manifest, (New-Object System.Text.UTF8Encoding($false)))

$RegKey = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\ru.privateline.chrome"
New-Item -Path $RegKey -Force | Out-Null
Set-Item -Path $RegKey -Value $ManifestPath

Write-Host ""
Write-Host "Private Line Chrome installed for the current Windows user."
Write-Host "Xray: $VersionOutput"
Write-Host "Chrome extension ID: $ExtensionId"
Write-Host "Close/reopen Chrome, open Private Line and paste your VLESS key."
Write-Host "Native host path: $ManifestPath"
