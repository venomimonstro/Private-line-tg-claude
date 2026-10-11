# Private Line desktop controller. Windows 10/11, PowerShell 5.1, .NET WinForms.
# This is a local browser/WinINET SOCKS proxy, not a system-wide VPN.
# No telemetry is sent unless the user explicitly clicks Send diagnostics.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class PrivateLineWinInet {
    [DllImport("wininet.dll", SetLastError=true)]
    public static extern bool InternetSetOption(IntPtr handle, int option, IntPtr buffer, int bufferLength);
}
'@
$ErrorActionPreference = "Stop"
$Base = Join-Path $env:LOCALAPPDATA "PrivateLineWindows"
$ConfigFile = Join-Path $Base "profile.json"
$StateFile = Join-Path $Base "process.json"
$AppSettings = Join-Path $Base "settings.json"
$Xray = Join-Path $Base "xray.exe"
$Port = 19590
$ClientVersion = "1.1.0"
$ReadyFile = Join-Path $Base "gui-ready.json"
New-Item -ItemType Directory -Force -Path $Base | Out-Null
$script:Stage = New-Object 'System.Collections.Generic.List[object]'
$script:SavedProxy = $null

function Add-Stage([string]$Name,[string]$Result,[string]$Code) {
    $script:Stage.Add(@{stage=$Name;result=$Result;code=$Code})
}
function Show-Info([string]$Text) {
    $Status.Text = $Text
    [System.Windows.Forms.Application]::DoEvents()
}
function Refresh-WindowsProxy {
    # Registry changes alone can be ignored by running Chrome/WinINET clients.
    [void][PrivateLineWinInet]::InternetSetOption([IntPtr]::Zero,39,[IntPtr]::Zero,0)
    [void][PrivateLineWinInet]::InternetSetOption([IntPtr]::Zero,37,[IntPtr]::Zero,0)
}
function Load-Settings {
    if(Test-Path $AppSettings) {
        try { return (Get-Content $AppSettings -Raw -Encoding UTF8 | ConvertFrom-Json) } catch {}
    }
    return $null
}
function Save-Settings {
    @{ endpoint=$Endpoint.Text.Trim(); token=$Token.Text.Trim() } |
      ConvertTo-Json -Compress | Set-Content -Encoding UTF8 -LiteralPath $AppSettings
}
function Get-Profile([string]$Link) {
    $uri = [Uri]$Link
    if ($uri.Scheme -ne 'vless') { throw "INVALID_SCHEME" }
    $parts = $uri.UserInfo.Split(':')
    if($parts.Count -ne 1 -or $parts[0] -notmatch '^[0-9a-fA-F-]{36}$') {throw "INVALID_UUID"}
    $uid = [Guid]::Parse($parts[0]).ToString()
    if($uri.Port -lt 1 -or $uri.Port -gt 65535) {throw "INVALID_PORT"}
    $ip=[System.Net.IPAddress]::None
    if(-not [System.Net.IPAddress]::TryParse($uri.Host, [ref]$ip)) {throw "INVALID_NODE_ADDRESS"}
    if(([System.Net.IPAddress]::IsLoopback($ip)) -or $ip.Equals([System.Net.IPAddress]::Any)) {throw "INVALID_NODE_ADDRESS"}
    $params=@{}
    foreach($pair in $uri.Query.TrimStart('?').Split('&')) {
        if(-not $pair) {continue}
        $bits=$pair.Split('=',2)
        $k=[Uri]::UnescapeDataString($bits[0])
        if($params.ContainsKey($k)) {throw "DUPLICATE_PARAM"}
        $params[$k]= if($bits.Count -eq 2) {[Uri]::UnescapeDataString($bits[1])} else {''}
    }
    if($params['security'] -ne 'reality' -or $params['flow'] -ne 'xtls-rprx-vision' -or
       $params['fp'] -ne 'chrome' -or $params['type'] -notin @('tcp','raw') -or
       $params['encryption'] -notin @('none',$null,'')) {throw "INVALID_REALITY_PARAMS"}
    if($params['pbk'] -notmatch '^[A-Za-z0-9_-]{43,44}$' -or
       $params['sid'] -notmatch '^(?:[a-fA-F0-9]{2}){1,8}$' -or
       $params['sni'] -notmatch '^[A-Za-z0-9.-]{1,253}$') {throw "INVALID_REALITY_KEYS"}
    return @{
        log=@{loglevel="warning"}
        inbounds=@(@{tag="local-socks";listen="127.0.0.1";port=$Port;protocol="socks";settings=@{auth="noauth";udp=$false}})
        outbounds=@(@{
            tag="via-node";protocol="vless"
            settings=@{vnext=@(@{address=$uri.Host;port=$uri.Port;users=@(@{id=$uid;encryption="none";flow="xtls-rprx-vision"})})}
            streamSettings=@{network="tcp";security="reality";realitySettings=@{
                serverName=$params['sni'];fingerprint="chrome";password=$params['pbk'];shortId=$params['sid'];spiderX=""
            }}
        })
        routing=@{domainStrategy="AsIs";rules=@()}
    }
}
function Check-Xray {
    if(-not (Test-Path $Xray)) {Add-Stage "binary" "failed" "XRAY_NOT_INSTALLED";throw "XRAY_NOT_INSTALLED"}
    $version=& $Xray version 2>&1 | Select-Object -First 1
    if("$version" -notmatch '^Xray 26\.6\.27(\s|$)') {
        Add-Stage "binary" "failed" "WRONG_XRAY_VERSION";throw "WRONG_XRAY_VERSION"
    }
    Add-Stage "binary" "ok" "XRAY_VERSION_OK"
}
function Stop-Client {
    if(Test-Path $StateFile) {
        try {
            $state=Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $proc=Get-CimInstance Win32_Process -Filter ("ProcessId="+[int]$state.pid) -ErrorAction SilentlyContinue
            if($proc -and $proc.ExecutablePath -and $proc.ExecutablePath -ieq $Xray) {
                Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
            }
        }catch {}
        Remove-Item $StateFile -Force -ErrorAction SilentlyContinue
    }
    $settingsPath='HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
    if($script:SavedProxy -ne $null){
        Set-ItemProperty -Path $settingsPath -Name ProxyEnable -Value $script:SavedProxy.enabled
        Set-ItemProperty -Path $settingsPath -Name ProxyServer -Value $script:SavedProxy.server
        $script:SavedProxy=$null
        Remove-Item (Join-Path $Base "original-proxy.json") -Force -ErrorAction SilentlyContinue
        Refresh-WindowsProxy
    } elseif(Test-Path (Join-Path $Base "original-proxy.json")) {
        try {
            $old=Get-Content (Join-Path $Base "original-proxy.json") -Raw | ConvertFrom-Json
            Set-ItemProperty -Path $settingsPath -Name ProxyEnable -Value ([int]$old.enabled)
            Set-ItemProperty -Path $settingsPath -Name ProxyServer -Value ([string]$old.server)
            Remove-Item (Join-Path $Base "original-proxy.json") -Force
            Refresh-WindowsProxy
        }catch{}
    }
}
function Verify-Socks {
    $c=New-Object System.Net.Sockets.TcpClient
    try{
        $task=$c.ConnectAsync('127.0.0.1',$Port)
        if(-not $task.Wait(1000)){return $false}
        $stream=$c.GetStream()
        $stream.ReadTimeout=2000
        $stream.Write([byte[]](5,1,0),0,3)
        $buffer=New-Object byte[] 2
        $n=$stream.Read($buffer,0,2)
        return $n -eq 2 -and $buffer[0] -eq 5 -and $buffer[1] -eq 0
    }catch{return $false}finally{$c.Dispose()}
}
function Connect-Client {
    $script:Stage.Clear()
    Add-Stage "install" "ok" "GUI_STARTED"
    try {
        Check-Xray
        Show-Info "Получаем VPN-профиль автоматически..."
        try {$text=Fetch-VpnProfile}
        catch {Add-Stage "parse" "failed" "PROFILE_FETCH_FAILED";throw "PROFILE_FETCH_FAILED"}
        try {$config=Get-Profile $text;Add-Stage "parse" "ok" "PROFILE_VALID"}
        catch {Add-Stage "parse" "failed" "INVALID_PROFILE";throw "INVALID_PROFILE"}
        [IO.File]::WriteAllText($ConfigFile, ($config | ConvertTo-Json -Depth 24), (New-Object Text.UTF8Encoding($false)))
        # Never leak the VLESS credentials into the diagnostic stage log.
        $profileJson=Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $remote=$profileJson.outbounds[0].settings.vnext[0]
        $tcp=New-Object System.Net.Sockets.TcpClient
        try {
            $res=$tcp.BeginConnect([string]$remote.address,[int]$remote.port,$null,$null)
            if(-not $res.AsyncWaitHandle.WaitOne(5000)){throw "TCP_TIMEOUT"}
            $tcp.EndConnect($res)
            Add-Stage "tcp" "ok" "NODE_REACHABLE"
        } catch {Add-Stage "tcp" "failed" "NODE_UNREACHABLE";throw "NODE_UNREACHABLE"}
        finally{$tcp.Dispose()}
        Stop-Client
        $p=Start-Process -FilePath $Xray -ArgumentList ("run -config `""+$ConfigFile+"`"") -WorkingDirectory $Base -WindowStyle Hidden -PassThru
        @{pid=$p.Id} | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8
        $ok=$false
        for($i=0;$i -lt 30;$i++){Start-Sleep -Milliseconds 300;if(Verify-Socks){$ok=$true;break}}
        if(-not $ok){Add-Stage "socks" "failed" "LOCAL_XRAY_FAILED";throw "LOCAL_XRAY_FAILED"}
        Add-Stage "socks" "ok" "SOCKS_LISTENER_OK"
        $curl=Join-Path $env:SystemRoot "System32\curl.exe"
        if(-not (Test-Path $curl)){Add-Stage "https" "failed" "CURL_NOT_FOUND";throw "CURL_NOT_FOUND"}
        $result=& $curl --silent --show-error --fail --ipv4 --noproxy "localhost" --socks5-hostname "127.0.0.1:$Port" --connect-timeout 8 --max-time 18 "https://api.ipify.org" 2>$null
        if($LASTEXITCODE -ne 0 -or "$result" -notmatch '^\d{1,3}(\.\d{1,3}){3}$'){
            Add-Stage "https" "failed" "VPN_EGRESS_FAILED";throw "VPN_EGRESS_FAILED"
        }
        Add-Stage "https" "ok" "VPN_EGRESS_VERIFIED"
        $settingsPath='HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
        $prev=Get-ItemProperty $settingsPath
        $script:SavedProxy=@{enabled=[int]$prev.ProxyEnable;server=[string]$prev.ProxyServer}
        $script:SavedProxy | ConvertTo-Json | Set-Content (Join-Path $Base "original-proxy.json") -Encoding UTF8
        Set-ItemProperty -Path $settingsPath -Name ProxyServer -Value ("socks=127.0.0.1:"+$Port)
        Set-ItemProperty -Path $settingsPath -Name ProxyEnable -Value 1
        Refresh-WindowsProxy
        Add-Stage "proxy" "ok" "WINDOWS_USER_PROXY_ENABLED"
        Show-Info "Подключено. Внешний VPN IP: $result. Настроен прокси Windows/Chrome (НЕ системный VPN)."
    }catch{
        $code="$($_.Exception.Message)"
        Stop-Client
        $report=Send-Diagnostics -Automatic
        Show-Info ("VPN не подключён: " + $code + ". " + $report)
    }
}
function Send-Diagnostics {
    if(-not $Consent.Checked){Show-Info "Отметьте согласие на отправку диагностических кодов.";return}
    $endpoint=$Endpoint.Text.Trim().TrimEnd('/')
    if(-not $endpoint.StartsWith('https://')){Show-Info "Введите HTTPS-адрес основного сервера.";return}
    if(-not $Token.Text.Trim()){Show-Info "Введите диагностический токен из админки.";return}
    try {
        $os=[Environment]::OSVersion.Version
        $body=@{
            version=$ClientVersion;windows=([string]$os.Major+"."+[string]$os.Minor)
            consent=$true;stages=@($script:Stage.ToArray())
        }| ConvertTo-Json -Depth 8 -Compress
        $response=Invoke-RestMethod -Uri ($endpoint+"/api/windows-diagnostics") -Method Post -ContentType "application/json" -Headers @{"x-private-line-diagnostics-token"=$Token.Text.Trim()} -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 20
        Save-Settings
        Show-Info ("Отчёт принят сервером. ID: " + $response.report_id)
    }catch{Show-Info ("Отправка не удалась. HTTP/сеть/токен: " + $_.Exception.GetType().Name)}
}
$form=New-Object Windows.Forms.Form
$form.Text="Private Line | Windows (1.1.0)"
$form.Size=New-Object Drawing.Size(570,705)
$form.StartPosition="CenterScreen"
$form.BackColor=[Drawing.Color]::FromArgb(243,248,246)
$form.Font=New-Object Drawing.Font("Segoe UI",10)
function Label($text,$y){$l=New-Object Windows.Forms.Label;$l.Text=$text;$l.Location=New-Object Drawing.Point(20,$y);$l.Size=New-Object Drawing.Size(515,25);$form.Controls.Add($l)}
Label "PRIVATE LINE  •  Windows клиент" 18
Label "Вставьте VLESS Reality ключ из Telegram-бота:" 65
$ProfileInput=New-Object Windows.Forms.TextBox;$ProfileInput.Multiline=$true;$ProfileInput.ScrollBars="Vertical";$ProfileInput.Location=New-Object Drawing.Point(20,98);$ProfileInput.Size=New-Object Drawing.Size(510,95);$form.Controls.Add($ProfileInput)
$connect=New-Object Windows.Forms.Button;$connect.Text="Подключить";$connect.Location=New-Object Drawing.Point(20,205);$connect.Size=New-Object Drawing.Size(248,40);$form.Controls.Add($connect)
$disconnect=New-Object Windows.Forms.Button;$disconnect.Text="Отключить";$disconnect.Location=New-Object Drawing.Point(283,205);$disconnect.Size=New-Object Drawing.Size(247,40);$form.Controls.Add($disconnect)
$Status=New-Object Windows.Forms.Label;$Status.Text="Клиент установлен. VPN пока отключён. Вставьте ключ и нажмите «Подключить».";$Status.Location=New-Object Drawing.Point(20,258);$Status.Size=New-Object Drawing.Size(510,60);$form.Controls.Add($Status)
Label "Диагностика • отправляется только по вашему согласию" 320
Label "Адрес основного сервера (https://...)" 351
$Endpoint=New-Object Windows.Forms.TextBox;$Endpoint.Location=New-Object Drawing.Point(20,378);$Endpoint.Size=New-Object Drawing.Size(510,26);$form.Controls.Add($Endpoint)
Label "Токен диагностики из админки" 410
$Token=New-Object Windows.Forms.TextBox;$Token.Location=New-Object Drawing.Point(20,437);$Token.Size=New-Object Drawing.Size(510,26);$Token.UseSystemPasswordChar=$true;$form.Controls.Add($Token)
$Consent=New-Object Windows.Forms.CheckBox;$Consent.Location=New-Object Drawing.Point(20,475);$Consent.Size=New-Object Drawing.Size(515,50);$Consent.Text="Согласен отправить только результаты этапов подключения. Без UUID, VPN-ключей, IP и сырых журналов.";$form.Controls.Add($Consent)
$send=New-Object Windows.Forms.Button;$send.Text="Отправить диагностику на сервер";$send.Location=New-Object Drawing.Point(20,531);$send.Size=New-Object Drawing.Size(510,37);$form.Controls.Add($send)
$verify=New-Object Windows.Forms.Button
$verify.Text="Проверить установку"
$verify.Location=New-Object Drawing.Point(20,576)
$verify.Size=New-Object Drawing.Size(510,36)
$form.Controls.Add($verify)
$verify.Add_Click({
    try {
        Check-Xray
        $shortcut=Join-Path ([Environment]::GetFolderPath("DesktopDirectory")) "Private Line.lnk"
        if(-not (Test-Path -LiteralPath $shortcut)) { Show-Info "Xray установлен, но ярлык отсутствует. Повторите INSTALL-WINDOWS.cmd для восстановления.";return }
        Add-Stage "install" "ok" "FILES_AND_XRAY_VERIFIED"
        Show-Info "УСТАНОВЛЕНО: приложение и Xray 26.6.27. Ярлык на рабочем столе. VPN подключится после нажатия «Подключить»."
    } catch {
        Show-Info "УСТАНОВКА НЕПОЛНАЯ: проверьте INSTALL-WINDOWS.cmd. Xray 26.6.27 отсутствует или повреждён."
    }
})
$form.Add_Shown({
    # Installer verifies this exact marker before claiming successful launch.
    $marker=@{version=$ClientVersion;pid=$PID} | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($ReadyFile,$marker,(New-Object Text.UTF8Encoding($false)))
})
$form.Add_FormClosed({
    Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue
    Stop-Client
})
Add-Stage "install" "ok" "GUI_STARTED"
$settings=Load-Settings
if($settings){$Endpoint.Text=$settings.endpoint;$Token.Text=$settings.token}
$connect.Add_Click({Connect-Client})
$disconnect.Add_Click({Stop-Client;Add-Stage "disconnect" "ok" "USER_DISCONNECTED";Show-Info "Отключено. Предыдущие настройки прокси восстановлены."})
$send.Add_Click({Send-Diagnostics})
[void]$form.ShowDialog()
