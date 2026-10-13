using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Net.Security;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Win32;

namespace PrivateLine;

public sealed class MainForm : Form
{
    readonly string BaseDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "PrivateLine"
    );
    readonly string XrayPath;
    readonly string DevicePath;
    readonly string ConfigPath;
    readonly string StatePath;
    readonly string PendingPath;
    readonly Label status = new();
    readonly Button connect = new();
    readonly Button disconnect = new();
    readonly Button send = new();
    readonly Button refresh = new();
    readonly Button pasteKey = new();
    readonly ComboBox serverSelector = new();
    readonly CheckBox diagnostics = new();
    readonly List<Stage> stages = new();
    readonly List<ServerProfile> availableServers = new();
    string? manualKey;
    readonly string manualKeyPath;
    readonly SemaphoreSlim connectionGate = new(1,1);
    readonly System.Windows.Forms.Timer tunnelWatchdog = new() { Interval=2000 };

    Process? xray;
    CancellationTokenSource? connectCts;
    int socksPort;

    [DllImport("wininet.dll", SetLastError=true)]
    static extern bool InternetSetOption(
        IntPtr hInternet,int option,IntPtr buffer,int bufferLength
    );

    public MainForm()
    {
        XrayPath = Path.Combine(BaseDir, "xray.exe");
        DevicePath = Path.Combine(BaseDir, "device.json");
        ConfigPath = Path.Combine(BaseDir, "client.json");
        StatePath = Path.Combine(BaseDir, "state.json");
        PendingPath = Path.Combine(BaseDir, "pending-reports.json");
        manualKeyPath = Path.Combine(BaseDir, "telegram-key.dpapi");

        Text = "Private Line";
        Width = 590;
        Height = 490;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        FormBorderStyle = FormBorderStyle.FixedSingle;

        var title = new Label {
            Left=24,Top=18,Width=530,Height=32,Text="PRIVATE LINE",
            Font=new Font("Segoe UI",18,FontStyle.Bold)
        };
        var subtitle = new Label {
            Left=24,Top=57,Width=530,Height=39,
            Text="Выберите сервер и нажмите «Подключить». Ключ можно получить в Telegram."
        };
        serverSelector.Left=24;serverSelector.Top=113;serverSelector.Width=320;
        serverSelector.Height=32;serverSelector.DropDownStyle=ComboBoxStyle.DropDownList;
        serverSelector.Items.Add("Автоматически (лучший доступный)");
        serverSelector.SelectedIndex=0;

        refresh.Left=355;refresh.Top=113;refresh.Width=95;refresh.Height=30;
        refresh.Text="Обновить";
        pasteKey.Left=458;pasteKey.Top=113;pasteKey.Width=106;pasteKey.Height=30;
        pasteKey.Text="Ключ из TG";

        status.Left=24;status.Top=158;status.Width=540;status.Height=62;
        status.Text="Не подключено.";
        connect.Left=24;connect.Top=235;connect.Width=260;connect.Height=45;
        connect.Text="Подключить VPN";
        disconnect.Left=304;disconnect.Top=235;disconnect.Width=260;
        disconnect.Height=45;disconnect.Text="Отключить";

        diagnostics.Left=24;diagnostics.Top=303;diagnostics.Width=540;
        diagnostics.Height=42;
        diagnostics.Text="Разрешить ручную отправку обезличенной диагностики";
        diagnostics.Checked=false;
        send.Left=24;send.Top=363;send.Width=540;send.Height=37;
        send.Text="Отправить диагностику сейчас";

        Controls.AddRange([
            title,subtitle,serverSelector,refresh,pasteKey,
            status,connect,disconnect,diagnostics,send
        ]);
        connect.Click += async (_,_) => await ConnectAsync();
        disconnect.Click += (_,_) => Disconnect();
        refresh.Click += async (_,_) => await RefreshServersAsync();
        pasteKey.Click += (_,_) => PasteTelegramKey();
        send.Click += async (_,_) => status.Text = await SendReportAsync();
        Shown += async (_,_) => await RefreshServersAsync();
        FormClosing += (_,_) => {
            tunnelWatchdog.Stop();
            DisconnectCore(false);
        };

        Directory.CreateDirectory(BaseDir);
        LoadSavedManualKey();
        CleanupOrphanXray();
        RecoverStaleProxy();

        tunnelWatchdog.Tick += (_,_) => {
            try {
                if(File.Exists(StatePath) && (xray is null || xray.HasExited)) {
                    RestoreProxy();
                    StopXrayOnly();
                    status.Text="VPN-процесс остановился. Интернет восстановлен, нажмите «Подключить VPN» снова.";
                }
            } catch {}
        };
        tunnelWatchdog.Start();

        status.Text = File.Exists(DevicePath)
            ? "Установка привязана к подписке. Нажмите «Подключить VPN»."
            : "Нет привязки. Скачайте новый персональный PrivateLineSetup.exe.";
    }

    record Device(string api_base,string token_dpapi,string tls_pin);
    record Stage(string stage,string result,string code);
    record ProxyState(int proxy_enable,string proxy_server,int socks_port);
    record ServerProfile(int id,string name,string health,string url);

    (string BaseUrl,string Token,string TlsPin) ReadDevice()
    {
        if(!File.Exists(DevicePath))
            throw new InvalidOperationException("DEVICE_NOT_PAIRED");
        var d=JsonSerializer.Deserialize<Device>(File.ReadAllText(DevicePath))
            ??throw new InvalidOperationException("DEVICE_INVALID");
        if(
            !Uri.TryCreate(d.api_base,UriKind.Absolute,out var uri)
            || uri.Scheme!="https"
            || uri.AbsolutePath!="/"
        ) throw new InvalidOperationException("DEVICE_SERVER_INVALID");
        var token=Encoding.UTF8.GetString(
            ProtectedData.Unprotect(
                Convert.FromBase64String(d.token_dpapi),
                null,
                DataProtectionScope.CurrentUser
            )
        );
        if(token.Length<45)throw new InvalidOperationException("DEVICE_TOKEN_INVALID");
        if(
            string.IsNullOrWhiteSpace(d.tls_pin)
            || !d.tls_pin.StartsWith("sha256//",StringComparison.Ordinal)
        ) throw new InvalidOperationException("DEVICE_TLS_PIN_INVALID");
        return (d.api_base.TrimEnd('/'),token,d.tls_pin);
    }

    HttpClient PinnedHttp(string pin)
    {
        var handler=new HttpClientHandler();
        handler.ServerCertificateCustomValidationCallback=(request,cert,chain,errors)=>{
            if(cert is null)return false;
            try {
                using var rsa=cert.GetRSAPublicKey();
                byte[] spki;
                if(rsa is not null)spki=rsa.ExportSubjectPublicKeyInfo();
                else {
                    using var ecdsa=cert.GetECDsaPublicKey();
                    if(ecdsa is null)return false;
                    spki=ecdsa.ExportSubjectPublicKeyInfo();
                }
                var actual="sha256//"+Convert.ToBase64String(SHA256.HashData(spki));
                return CryptographicOperations.FixedTimeEquals(
                    Encoding.ASCII.GetBytes(actual),
                    Encoding.ASCII.GetBytes(pin)
                );
            } catch {return false;}
        };
        return new HttpClient(handler){Timeout=TimeSpan.FromSeconds(12)};
    }

    async Task<List<ServerProfile>> FetchProfilesAsync(CancellationToken token)
    {
        var d=ReadDevice();
        using var http=PinnedHttp(d.TlsPin);
        using var req=new HttpRequestMessage(
            HttpMethod.Get,d.BaseUrl+"/api/windows/profile"
        );
        req.Headers.Authorization=new AuthenticationHeaderValue("Bearer",d.Token);
        using var res=await http.SendAsync(
            req,HttpCompletionOption.ResponseHeadersRead,token
        );
        if(!res.IsSuccessStatusCode)
            throw new InvalidOperationException("PROFILE_FETCH_FAILED");
        using var doc=JsonDocument.Parse(await res.Content.ReadAsStringAsync(token));
        var result=new List<ServerProfile>();
        if(
            doc.RootElement.TryGetProperty("servers",out var servers)
            && servers.ValueKind==JsonValueKind.Array
        ) {
            foreach(var item in servers.EnumerateArray()) {
                if(
                    !item.TryGetProperty("vless_url",out var urlProp)
                    || urlProp.ValueKind!=JsonValueKind.String
                )continue;
                var link=urlProp.GetString();
                if(string.IsNullOrWhiteSpace(link)||
                   !link.StartsWith("vless://",StringComparison.OrdinalIgnoreCase))continue;
                var id=item.TryGetProperty("id",out var idProp)
                    && idProp.ValueKind==JsonValueKind.Number
                    ? idProp.GetInt32() : result.Count+1;
                var name=item.TryGetProperty("name",out var nameProp)
                    && nameProp.ValueKind==JsonValueKind.String
                    ? nameProp.GetString()??"Сервер" : "Сервер";
                var health=item.TryGetProperty("status",out var healthProp)
                    && healthProp.ValueKind==JsonValueKind.String
                    ? healthProp.GetString()??"unchecked" : "unchecked";
                if(result.All(x=>x.id!=id))
                    result.Add(new ServerProfile(id,name,health,link));
                if(result.Count>=20)break;
            }
        }
        if(result.Count==0 && doc.RootElement.TryGetProperty("vless_urls",out var urls)
            && urls.ValueKind==JsonValueKind.Array) {
            foreach(var item in urls.EnumerateArray()) {
                if(item.ValueKind!=JsonValueKind.String)continue;
                var link=item.GetString();
                if(!string.IsNullOrWhiteSpace(link)
                    && link.StartsWith("vless://",StringComparison.OrdinalIgnoreCase))
                    result.Add(new ServerProfile(result.Count+1,
                        $"Сервер {result.Count+1}","unchecked",link));
                if(result.Count>=20)break;
            }
        }
        return result;
    }

    void RefreshSelector(int selectedId=0)
    {
        serverSelector.Items.Clear();
        serverSelector.Items.Add("Автоматически (лучший доступный)");
        foreach(var item in availableServers) {
            var suffix=item.health=="healthy"?" ✓":" · проверить";
            serverSelector.Items.Add(item.name+suffix);
        }
        if(!string.IsNullOrWhiteSpace(manualKey))
            serverSelector.Items.Add("Ключ из Telegram (локально)");

        var ix=selectedId==-1
            ? serverSelector.Items.Count-1
            : selectedId==0
                ? 0
                : availableServers.FindIndex(x=>x.id==selectedId)+1;
        serverSelector.SelectedIndex=ix>=0&&ix<serverSelector.Items.Count?ix:0;
    }

    async Task RefreshServersAsync()
    {
        var wasSelected=SelectedServerId();
        refresh.Enabled=false;
        try {
            if(File.Exists(DevicePath)) {
                using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(12));
                var updated=await FetchProfilesAsync(timeout.Token);
                availableServers.Clear();
                availableServers.AddRange(updated);
                status.Text=$"Серверов по подписке: {availableServers.Count}. Выберите и подключитесь.";
            } else {
                status.Text="Укажите ключ из Telegram или установите персональный клиент.";
            }
        } catch {
            status.Text="Список недоступен. Можно вставить ключ из Telegram напрямую.";
        } finally {
            RefreshSelector(wasSelected);
            refresh.Enabled=true;
        }
    }

    int SelectedServerId()
    {
        var idx=serverSelector.SelectedIndex;
        if(idx<=0)return 0;
        if(idx<=availableServers.Count)return availableServers[idx-1].id;
        return -1;
    }

    void LoadSavedManualKey()
    {
        try {
            if(!File.Exists(manualKeyPath))return;
            var sealedBytes=Convert.FromBase64String(File.ReadAllText(manualKeyPath));
            var raw=ProtectedData.Unprotect(
                sealedBytes,null,DataProtectionScope.CurrentUser
            );
            var value=Encoding.UTF8.GetString(raw);
            if(value.StartsWith("vless://",StringComparison.OrdinalIgnoreCase))
                manualKey=value;
        } catch {}
    }

    void PasteTelegramKey()
    {
        try {
            var value=Clipboard.GetText().Trim();
            if(!value.StartsWith("vless://",StringComparison.OrdinalIgnoreCase)
                || !Uri.TryCreate(value,UriKind.Absolute,out _))
                throw new InvalidOperationException("PROFILE_INVALID");
            manualKey=value;
            File.WriteAllText(
                manualKeyPath,
                Convert.ToBase64String(ProtectedData.Protect(
                    Encoding.UTF8.GetBytes(value),null,DataProtectionScope.CurrentUser
                ))
            );
            RefreshSelector(-1);
            status.Text="Ключ Telegram сохранён для этого Windows-пользователя. Нажмите «Подключить VPN».";
        } catch {
            status.Text="Скопируйте полный VLESS-ключ из Telegram, затем нажмите «Ключ из TG».";
        }
    }

    static Dictionary<string,string> Query(Uri uri)
    {
        var d=new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);
        foreach(var part in uri.Query.TrimStart('?').Split(
            '&',StringSplitOptions.RemoveEmptyEntries
        )) {
            var x=part.Split('=',2);
            var k=Uri.UnescapeDataString(x[0]);
            var v=x.Length>1?Uri.UnescapeDataString(x[1]):"";
            if(!d.TryAdd(k,v))
                throw new InvalidOperationException("PROFILE_DUPLICATE_PARAM");
        }
        return d;
    }

    string BuildXrayConfig(string raw)
    {
        var uri=new Uri(raw);
        var q=Query(uri);
        var pbk=q.GetValueOrDefault("pbk","");
        var sid=q.GetValueOrDefault("sid","");
        var sni=q.GetValueOrDefault("sni","");
        if(
            uri.Scheme!="vless"
            || !Guid.TryParse(uri.UserInfo,out var id)
            || uri.Port<1
            || q.GetValueOrDefault("security")!="reality"
            || q.GetValueOrDefault("flow")!="xtls-rprx-vision"
            || q.GetValueOrDefault("encryption","none")!="none"
            || (
                q.GetValueOrDefault("type","tcp")!="tcp"
                && q.GetValueOrDefault("type","tcp")!="raw"
            )
            || q.GetValueOrDefault("fp","chrome")!="chrome"
            || !Regex.IsMatch(pbk,@"^[A-Za-z0-9_-]{43,44}$")
            || !Regex.IsMatch(sid,@"^(?:[0-9A-Fa-f]{2}){1,8}$")
            || !Regex.IsMatch(sni,@"^[A-Za-z0-9.-]{1,253}$")
        ) throw new InvalidOperationException("PROFILE_INVALID");

        var obj=new {
            log=new {loglevel="warning"},
            inbounds=new[] {
                new {
                    listen="127.0.0.1",port=socksPort,protocol="socks",
                    settings=new {auth="noauth",udp=false}
                }
            },
            outbounds=new[] {
                new {
                    protocol="vless",
                    settings=new {
                        vnext=new[] {
                            new {
                                address=uri.Host,port=uri.Port,
                                users=new[] {
                                    new {
                                        id=id.ToString(),
                                        encryption="none",
                                        flow="xtls-rprx-vision"
                                    }
                                }
                            }
                        }
                    },
                    streamSettings=new {
                        network="tcp",security="reality",
                        realitySettings=new {
                            serverName=sni,
                            fingerprint="chrome",
                            password=pbk,
                            shortId=sid,
                            spiderX=""
                        }
                    }
                }
            },
            routing=new {domainStrategy="AsIs",rules=Array.Empty<object>()}
        };
        return JsonSerializer.Serialize(obj);
    }

    static int AllocateSocksPort()
    {
        var listener=new TcpListener(IPAddress.Loopback,0);
        listener.Start();
        var port=((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        if(port<1025)throw new InvalidOperationException("SOCKS_PORT_INVALID");
        return port;
    }

    async Task<bool> SocksReadyAsync(CancellationToken token)
    {
        try {
            using var c=new TcpClient();
            using var cts=CancellationTokenSource.CreateLinkedTokenSource(token);
            cts.CancelAfter(1200);
            await c.ConnectAsync(IPAddress.Loopback,socksPort,cts.Token);
            var s=c.GetStream();
            await s.WriteAsync(new byte[]{5,1,0},cts.Token);
            var b=new byte[2];
            var n=await s.ReadAsync(b,cts.Token);
            return n==2&&b[0]==5&&b[1]==0;
        } catch(OperationCanceledException) when(!token.IsCancellationRequested) {
            return false;
        } catch {
            return false;
        }
    }

    async Task<(bool Ok,string Body)> CurlProbeAsync(
        string url,bool captureBody,CancellationToken token
    )
    {
        var args=captureBody
            ? $"--silent --show-error --ipv4 --noproxy \"\" --socks5-hostname 127.0.0.1:{socksPort} --connect-timeout 6 --max-time 12 {url}"
            : $"--silent --show-error --ipv4 --noproxy \"\" --socks5-hostname 127.0.0.1:{socksPort} --connect-timeout 6 --max-time 12 -o NUL -w %{{http_code}} {url}";
        using var p=Process.Start(new ProcessStartInfo(
            Path.Combine(Environment.SystemDirectory,"curl.exe"),args
        ){
            UseShellExecute=false,
            RedirectStandardOutput=true,
            RedirectStandardError=true,
            CreateNoWindow=true
        })??throw new InvalidOperationException("CURL_START_FAILED");
        try {
            var outputTask=p.StandardOutput.ReadToEndAsync(token);
            await p.WaitForExitAsync(token);
            var output=(await outputTask).Trim();
            if(p.ExitCode!=0)return (false,"");
            if(captureBody)return (IPAddress.TryParse(output,out _),output);
            return (
                int.TryParse(output,out var code)&&code>=100&&code<=599,
                output
            );
        } catch(OperationCanceledException) {
            try {if(!p.HasExited)p.Kill(true);} catch {}
            throw;
        }
    }

    async Task<string> CheckEgressAsync(CancellationToken token)
    {
        var probes=await Task.WhenAll(
            CurlProbeAsync("https://api.ipify.org",true,token),
            CurlProbeAsync("https://www.cloudflare.com/cdn-cgi/trace",false,token),
            CurlProbeAsync("https://www.google.com/generate_204",false,token)
        );
        var success=probes.Count(x=>x.Ok);
        if(success<2)throw new InvalidOperationException("VPN_EGRESS_FAILED");
        return probes[0].Ok&&!string.IsNullOrWhiteSpace(probes[0].Body)
            ?probes[0].Body
            :"проверен";
    }

    static void AtomicWrite(string path,string text)
    {
        var temp=path+".new";
        File.WriteAllText(temp,text,new UTF8Encoding(false));
        File.Move(temp,path,true);
    }

    void CaptureAndEnableProxy()
    {
        using var key=Registry.CurrentUser.OpenSubKey(
            @"Software\Microsoft\Windows\CurrentVersion\Internet Settings",true
        )??throw new InvalidOperationException("PROXY_REGISTRY_FAILED");
        var previousEnable=Convert.ToInt32(key.GetValue("ProxyEnable",0));
        var previousServer=Convert.ToString(key.GetValue("ProxyServer",""))??"";

        var state=new ProxyState(previousEnable,previousServer,socksPort);
        AtomicWrite(StatePath,JsonSerializer.Serialize(state));

        try {
            key.SetValue("ProxyServer",$"socks=127.0.0.1:{socksPort}");
            key.SetValue("ProxyEnable",1,RegistryValueKind.DWord);
            RefreshWindowsProxy();
        } catch {
            RestoreProxy();
            throw;
        }
    }

    static void RefreshWindowsProxy()
    {
        InternetSetOption(IntPtr.Zero,39,IntPtr.Zero,0);
        InternetSetOption(IntPtr.Zero,37,IntPtr.Zero,0);
    }

    void RestoreProxy()
    {
        if(!File.Exists(StatePath))return;
        try {
            var state=JsonSerializer.Deserialize<ProxyState>(File.ReadAllText(StatePath));
            if(state is null)return;
            using var key=Registry.CurrentUser.OpenSubKey(
                @"Software\Microsoft\Windows\CurrentVersion\Internet Settings",true
            );
            if(key is null)return;
            var current=Convert.ToString(key.GetValue("ProxyServer",""))??"";
            var ours=$"socks=127.0.0.1:{state.socks_port}";
            if(string.Equals(current,ours,StringComparison.OrdinalIgnoreCase)) {
                key.SetValue("ProxyEnable",state.proxy_enable,RegistryValueKind.DWord);
                key.SetValue("ProxyServer",state.proxy_server??"");
                RefreshWindowsProxy();
            }
        } catch {}
        finally {
            try {File.Delete(StatePath);} catch {}
        }
    }

    void RecoverStaleProxy()
    {
        if(File.Exists(StatePath)) {
            RestoreProxy();
            return;
        }
        // Version 2.0 used a fixed SOCKS port without crash-state persistence.
        // Only undo its exact known stale proxy; never modify another VPN,
        // administrator-configured proxy or corporate network setting.
        try {
            using var key=Registry.CurrentUser.OpenSubKey(
                @"Software\Microsoft\Windows\CurrentVersion\Internet Settings",true
            );
            if(key is null)return;
            var current=Convert.ToString(key.GetValue("ProxyServer",""))??"";
            if(
                Convert.ToInt32(key.GetValue("ProxyEnable",0))==1
                && string.Equals(
                    current,"socks=127.0.0.1:19590",
                    StringComparison.OrdinalIgnoreCase
                )
            ) {
                key.SetValue("ProxyEnable",0,RegistryValueKind.DWord);
                key.SetValue("ProxyServer","");
                RefreshWindowsProxy();
                status.Text="Обнаружен и отключён устаревший proxy предыдущей версии.";
            }
        } catch {}
    }

    void CleanupOrphanXray()
    {
        foreach(var p in Process.GetProcessesByName("xray")) {
            try {
                var file=p.MainModule?.FileName;
                if(
                    file is not null
                    && string.Equals(
                        Path.GetFullPath(file),
                        Path.GetFullPath(XrayPath),
                        StringComparison.OrdinalIgnoreCase
                    )
                ) {
                    p.Kill(true);
                    p.WaitForExit(3000);
                }
            } catch {}
            finally {p.Dispose();}
        }
    }

    async Task<bool> XrayOkAsync(CancellationToken token)
    {
        if(!File.Exists(XrayPath))return false;
        try {
            using var cts=CancellationTokenSource.CreateLinkedTokenSource(token);
            cts.CancelAfter(TimeSpan.FromSeconds(5));
            using var p=Process.Start(new ProcessStartInfo(XrayPath,"version") {
                UseShellExecute=false,
                RedirectStandardOutput=true,
                RedirectStandardError=true,
                CreateNoWindow=true
            });
            if(p is null)return false;
            var line=await p.StandardOutput.ReadLineAsync(cts.Token);
            await p.WaitForExitAsync(cts.Token);
            return p.ExitCode==0
                && line?.StartsWith("Xray 26.6.27",StringComparison.Ordinal)==true;
        } catch {return false;}
    }

    async Task ConnectAsync()
    {
        if(!await connectionGate.WaitAsync(0))return;
        connect.Enabled=false;

        // Stop/cancel the previous session before creating the new token.
        DisconnectCore(false);
        connectCts?.Dispose();
        connectCts=new CancellationTokenSource();
        var token=connectCts.Token;
        stages.Clear();
        stages.Add(new("install","ok","CLIENT_STARTED"));

        try {
            token.ThrowIfCancellationRequested();
            if(!await XrayOkAsync(token))
                throw new InvalidOperationException("XRAY_VERSION_INVALID");

            status.Text="Готовим VPN-подключение...";
            var selection=SelectedServerId();
            if(availableServers.Count==0 && selection!=-1
                && File.Exists(DevicePath)) {
                var fresh=await FetchProfilesAsync(token);
                availableServers.Clear();
                availableServers.AddRange(fresh);
                RefreshSelector(selection);
            }
            List<ServerProfile> candidates;
            if(selection==-1 && !string.IsNullOrWhiteSpace(manualKey)) {
                candidates=[new ServerProfile(-1,"Ключ из Telegram","manual",manualKey)];
            } else if(selection>0) {
                candidates=availableServers.Where(s=>s.id==selection).ToList();
            } else {
                candidates=availableServers.ToList();
                // One manually imported Telegram key is a last-resort fallback
                // even when the control-plane list contains stale endpoints.
                if(!string.IsNullOrWhiteSpace(manualKey)
                    && candidates.All(s=>s.url!=manualKey))
                    candidates.Add(new ServerProfile(-1,"Ключ из Telegram","manual",manualKey));
            }
            if(candidates.Count==0)
                throw new InvalidOperationException("NO_VPN_SERVERS");
            stages.Add(new("parse","ok","PROFILE_VALID"));

            string? connectedIp=null;
            Exception? lastFailure=null;
            for(var endpoint=0;endpoint<candidates.Count;endpoint++) {
                token.ThrowIfCancellationRequested();
                StopXrayOnly();
                socksPort=AllocateSocksPort();
                status.Text=candidates.Count>1
                    ? $"Подключаемся: {candidates[endpoint].name} ({endpoint+1}/{candidates.Count})"
                    : $"Подключаемся: {candidates[endpoint].name}";

                try {
                    var config=BuildXrayConfig(candidates[endpoint].url);
                    AtomicWrite(ConfigPath,config);

                    xray=Process.Start(new ProcessStartInfo(
                        XrayPath,$"run -config \"{ConfigPath}\""
                    ){
                        UseShellExecute=false,
                        CreateNoWindow=true
                    });
                    if(xray is null)
                        throw new InvalidOperationException("XRAY_START_FAILED");

                    var ready=false;
                    for(var i=0;i<30;i++) {
                        token.ThrowIfCancellationRequested();
                        await Task.Delay(250,token);
                        if(await SocksReadyAsync(token)){ready=true;break;}
                        if(xray.HasExited)break;
                    }
                    if(!ready)
                        throw new InvalidOperationException("LOCAL_XRAY_FAILED");

                    connectedIp=await CheckEgressAsync(token);
                    lastFailure=null;
                    break;
                } catch(OperationCanceledException) {
                    throw;
                } catch(Exception ex) {
                    lastFailure=ex;
                    StopXrayOnly();
                }
            }

            if(connectedIp is null)
                throw lastFailure??new InvalidOperationException("VPN_EGRESS_FAILED");

            stages.Add(new("socks","ok","SOCKS_LISTENER_OK"));
            stages.Add(new("https","ok","VPN_EGRESS_VERIFIED"));
            token.ThrowIfCancellationRequested();

            CaptureAndEnableProxy();
            stages.Add(new("proxy","ok","WINDOWS_PROXY_ENABLED"));
            status.Text=$"Подключено. VPN IP: {connectedIp}";
        } catch(OperationCanceledException) {
            DisconnectCore(false);
            status.Text="Отключено.";
        } catch(Exception ex) {
            stages.Add(new("https","failed",SafeCode(ex.Message)));
            DisconnectCore(false);
            status.Text="VPN не подключён: "+SafeCode(ex.Message)+
                ". Диагностика отправляется только по кнопке ниже.";
        } finally {
            connect.Enabled=true;
            connectionGate.Release();
        }
    }

    static string SafeCode(string value)
    {
        var s=new string(
            value.Where(c=>char.IsAsciiLetterOrDigit(c)||c=='_').ToArray()
        );
        return string.IsNullOrEmpty(s)
            ?"UNKNOWN_ERROR"
            :s[..Math.Min(48,s.Length)];
    }

    void StopXrayOnly()
    {
        try {if(xray is {HasExited:false})xray.Kill(true);} catch {}
        try {xray?.Dispose();} catch {}
        xray=null;
    }

    void DisconnectCore(bool show)
    {
        try {connectCts?.Cancel();} catch {}
        RestoreProxy();
        StopXrayOnly();
        if(show)status.Text="Отключено.";
    }

    void Disconnect()=>DisconnectCore(true);

    async Task<string> SendReportAsync()
    {
        if(!diagnostics.Checked)return "Диагностика не разрешена пользователем.";
        var report=new {
            version="2.1.0",
            windows=Environment.OSVersion.Version.ToString(),
            consent=true,
            stages=stages
        };
        try {
            var d=ReadDevice();
            using var http=PinnedHttp(d.TlsPin);
            await FlushPendingAsync(http,d);
            using var req=new HttpRequestMessage(
                HttpMethod.Post,d.BaseUrl+"/api/windows/report"
            );
            req.Headers.Authorization=
                new AuthenticationHeaderValue("Bearer",d.Token);
            req.Content=new StringContent(
                JsonSerializer.Serialize(report),Encoding.UTF8,"application/json"
            );
            using var res=await http.SendAsync(req);
            if(!res.IsSuccessStatusCode)throw new Exception();
            using var doc=JsonDocument.Parse(await res.Content.ReadAsStringAsync());
            return "Отчёт "+doc.RootElement.GetProperty("report_id").GetString()+
                " принят сервером.";
        } catch {
            await QueueReportAsync(report);
            return "Отчёт сохранён локально. Сервер не нагружается повторными попытками.";
        }
    }

    async Task QueueReportAsync(object report)
    {
        List<JsonElement> items=[];
        try {
            if(File.Exists(PendingPath))
                items=JsonSerializer.Deserialize<List<JsonElement>>(
                    await File.ReadAllTextAsync(PendingPath)
                )??[];
        } catch {}
        using var d=JsonDocument.Parse(JsonSerializer.Serialize(report));
        items.Add(d.RootElement.Clone());
        if(items.Count>5)items=items.TakeLast(5).ToList();
        AtomicWrite(PendingPath,JsonSerializer.Serialize(items));
    }

    async Task FlushPendingAsync(
        HttpClient http,(string BaseUrl,string Token,string TlsPin) d
    )
    {
        if(!File.Exists(PendingPath))return;
        try {
            var items=JsonSerializer.Deserialize<List<JsonElement>>(
                await File.ReadAllTextAsync(PendingPath)
            )??[];
            var remaining=new List<JsonElement>();
            var batch=items.Take(5).ToList();
            for(var i=0;i<batch.Count;i++) {
                var item=batch[i];
                try {
                    using var req=new HttpRequestMessage(
                        HttpMethod.Post,d.BaseUrl+"/api/windows/report"
                    );
                    req.Headers.Authorization=
                        new AuthenticationHeaderValue("Bearer",d.Token);
                    req.Content=new StringContent(
                        item.GetRawText(),Encoding.UTF8,"application/json"
                    );
                    using var res=await http.SendAsync(req);
                    if(!res.IsSuccessStatusCode)remaining.Add(item);
                } catch {
                    remaining.AddRange(batch.Skip(i));
                    break;
                }
            }
            if(remaining.Count==0)File.Delete(PendingPath);
            else AtomicWrite(PendingPath,JsonSerializer.Serialize(remaining.TakeLast(5)));
        } catch {}
    }
}
