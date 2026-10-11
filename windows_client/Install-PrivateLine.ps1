# Install for current Windows account; no Chrome extension required.
$ErrorActionPreference="Stop"
$dir=Join-Path $env:LOCALAPPDATA "PrivateLineWindows"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "PrivateLine.ps1") -Destination (Join-Path $dir "PrivateLine.ps1") -Force
$exe=Join-Path $dir "xray.exe"
if(-not (Test-Path $exe)){
  $url="https://github.com/XTLS/Xray-core/releases/download/v26.6.27/Xray-windows-64.zip"
  $tmp=Join-Path $env:TEMP ("pl-"+[guid]::NewGuid()+".zip")
  $out=Join-Path $env:TEMP ("pl-"+[guid]::NewGuid())
  try{
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    Expand-Archive -LiteralPath $tmp -DestinationPath $out
    $candidate=Get-ChildItem -Path $out -Recurse -Filter xray.exe -File | Select-Object -First 1
    if(-not $candidate){throw "XRAY_BINARY_MISSING"}
    Copy-Item -LiteralPath $candidate.FullName -Destination $exe -Force
  }finally{
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $out -Force -Recurse -ErrorAction SilentlyContinue
  }
}
$version=& $exe version 2>&1 | Select-Object -First 1
if("$version" -notmatch '^Xray 26\.6\.27(\s|$)'){throw "XRAY_WRONG_VERSION"}
$launch=Join-Path $dir "PrivateLine.cmd"
[IO.File]::WriteAllText($launch, "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0PrivateLine.ps1`"`r`n", [Text.Encoding]::ASCII)
$desktop=[Environment]::GetFolderPath("Desktop")
$shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop "Private Line.lnk"))
$shortcut.TargetPath=$launch
$shortcut.WorkingDirectory=$dir
$shortcut.Description="Private Line Windows proxy client"
$shortcut.Save()
Write-Host "Installed Private Line Windows to $dir"
Write-Host "Desktop shortcut created. Launch Private Line from your desktop."
Start-Process -FilePath $launch
