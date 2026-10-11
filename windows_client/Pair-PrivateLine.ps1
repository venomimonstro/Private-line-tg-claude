# Redeem a one-time Telegram/admin pairing code; no manual IP, VLESS URL or token.
# Device token is encrypted with Windows DPAPI, current-user scope.
param([Parameter(Mandatory=$true)][string]$InstallDir)
$ErrorActionPreference="Stop"
$Target=Join-Path $InstallDir "device.json"
$Bootstrap=Join-Path $PSScriptRoot "bootstrap.json"

function Is-SafeUrl([string]$url) {
    try {
        $uri=[Uri]$url
        return ($uri.Scheme -eq "https" -and $uri.Host -and $uri.AbsolutePath -eq "/" -and
            -not $uri.UserInfo -and -not $uri.Query -and -not $uri.Fragment)
    } catch {return $false}
}
function Open-Device {
    if(-not (Test-Path -LiteralPath $Target)) {return $null}
    try {
        $data=Get-Content -LiteralPath $Target -Raw -Encoding UTF8 | ConvertFrom-Json
        if(-not (Is-SafeUrl $data.api_base)) {return $null}
        $raw=[Convert]::FromBase64String($data.token_dpapi)
        $unsealed=[Security.Cryptography.ProtectedData]::Unprotect(
            $raw,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
        $token=[Text.Encoding]::UTF8.GetString($unsealed)
        if($token.Length -lt 45) {return $null}
        return @{api_base=$data.api_base; token=$token}
    } catch {return $null}
}
function Profile-Works($pair) {
    try {
        [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
        $head=@{"Authorization"=("Bearer "+$pair.token)}
        $profile=Invoke-RestMethod -Method Get -Uri ($pair.api_base+"/api/windows/profile") -Headers $head -TimeoutSec 20
        return ($profile.ok -eq $true -and [string]$profile.vless_url -match '^vless://')
    } catch {return $false}
}

$existing=Open-Device
if($existing -and (Profile-Works $existing)){
    Write-Host "[OK] Windows device already paired. VPN profile is available." -ForegroundColor Green
    exit 0
}
if(-not (Test-Path -LiteralPath $Bootstrap)){
    throw "PAIRING_FILE_MISSING: download your PERSONAL installation ZIP using Telegram /windows or the admin personalization page. Generic ZIP cannot connect without pairing."
}
try { $data=Get-Content -LiteralPath $Bootstrap -Raw -Encoding UTF8 | ConvertFrom-Json }
catch { throw "PAIRING_FILE_INVALID" }
$base=[string]$data.api_base
$code=[string]$data.pair_code
if(-not (Is-SafeUrl $base) -or $code.Length -lt 35 -or $code.Length -gt 128){
    throw "PAIRING_SETTINGS_INVALID"
}
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
try {
    $payload=@{code=$code}|ConvertTo-Json -Compress
    $response=Invoke-RestMethod -Method Post -Uri ($base+"/api/windows/enroll") -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -ContentType "application/json" -TimeoutSec 20
} catch {
    # Do not include the code, token or request details in user-facing logs.
    throw "PAIRING_REQUEST_FAILED: code expired, already used, HTTPS unavailable or subscription inactive. Download a fresh personal ZIP from /windows."
}
if($response.ok -ne $true -or [string]$response.device_token -notmatch '^[A-Za-z0-9_-]{45,128}$'){
    throw "PAIRING_RESPONSE_INVALID"
}
$unsealedToken=[string]$response.device_token
$encrypted=[Security.Cryptography.ProtectedData]::Protect(
    [Text.Encoding]::UTF8.GetBytes($unsealedToken),
    $null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
$record=@{api_base=$base;token_dpapi=[Convert]::ToBase64String($encrypted);version=1}|ConvertTo-Json -Compress
[IO.File]::WriteAllText($Target,$record,(New-Object Text.UTF8Encoding($false)))
$paired=Open-Device
if(-not $paired -or -not (Profile-Works $paired)){
    throw "PAIRING_PROFILE_UNAVAILABLE: installation saved device authorization, but subscription profile cannot be fetched."
}
Write-Host "[OK] Personal VPN enrollment complete. No manual keys or IP address needed." -ForegroundColor Green
exit 0
