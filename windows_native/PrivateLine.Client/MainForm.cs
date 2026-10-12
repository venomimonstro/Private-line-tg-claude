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
    readonly CheckBox diagnostics = new();
    readonly List<Stage> stages = new();
    readonly SemaphoreSlim connectionGate = new(1,1);

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

        Text = "Private Line";
        Width = 560;
        Height = 430;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        FormBorderStyle = FormBorderStyle.FixedSingle;

        var title = new Label {
            Left=24,Top=22,Width=480,Height=32,Text="PRIVATE LINE",
            Font=new Font("Segoe UI",18,FontStyle.Bold)
        };
        var subtitle = new Label {
            Left=24,Top=62,Width=485,Height=44,
            Text="Профиль VPN загружается автоматически. IP, ключ и токен вводить не нужно."
        };
        status.Left=24;status.Top=120;status.Width=485;status.Height=60;
        status.Text="Не подключено.";

        connect.Left=24;connect.Top=195;connect.Width=235;connect.Height=44;
        connect.Text="Подключить VPN";
        disconnect.Left=274;disconnect.Top=195;disconnect.Width=235;
        disconnect.Height=44;disconnect.Text="Отключить";
        diagnostics.Left=24;diagnostics.Top=260;diagnostics.Width=485;
        diagnostics.Height=44;
        diagnostics.Text="Разрешить отправку обезличенной диагностики вручную";
        diagnostics.Checked=false;
        send.Left=24;send.Top=315;send.Width=485;send.Height=36;
        send.Text="Отправить диагностику сейчас";

        Controls.AddRange([title,subtitle,status,connect,disconnect,diagnostics,send]);
        connect.Click += async (_,_) => await ConnectAsync();
        disconnect.Click += (_,_) => Disconnect();
        send.Click += async (_,_) => status.Text = await SendReportAsync();
        FormClosing += (_,_) => DisconnectCore(false);

        Directory.CreateDirectory(BaseDir);
        CleanupOrphanXray();
        RecoverStaleProxy();

        status.Text = File.Exists(DevicePath)
            ? "Установка привязана к подписке. Нажмите «Подключить VPN»."
            : "Нет привязки. Скачайте новый персональный PrivateLineSetup.exe.";
    }

    record Device(string api_base,string token_dpapi,string tls_pin);
    record Stage(string stage,string result,string code);
    record ProxyState(int proxy_enable,string proxy_server,int socks_port);

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

    async Task<string> FetchProfileAsync(CancellationToken token)
    {
        var d=ReadDevice();
        using var http=PinnedHttp(d.TlsPin);
        using var req=new HttpRequestMessage(HttpMethod.Get,d.BaseUrl+"/api/windows/profile");
        req.Headers.Authorization=new AuthenticationHeaderValue("Bearer",d.Token);
        using var res=await http.SendAsync(req,HttpCompletionOption.ResponseHeadersRead,token);
        if(!res.IsSuccessStatusCode)
            throw new InvalidOperationException("PROFILE_FETCH_FAILED");
        using var doc=JsonDocument.Parse(await res.Content.ReadAsStringAsync(token));
        var url=doc.RootElement.GetProperty("vless_url").GetString();
        if(
            string.IsNullOrWhiteSpace(url)
            || !url.StartsWith("vless://",StringComparison.OrdinalIgnoreCase)
        ) throw new InvalidOperationException("PROFILE_INVALID");
        return url;
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
            || q.GetValueOrDefault("type","tcp") is not ("tcp" or "raw")
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
            ? $"--silent --show-error --ipv4 --socks5-hostname 127.0.0.1:{socksPort} --connect-timeout 6 --max-time 12 {url}"
            : $"--silent --show-error --ipv4 --socks5-hostname 127.0.0.1:{socksPort} --connect-timeout 6 --max-time 12 -o NUL -w %{{http_code}} {url}";
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
        var success=0;
        var exitIp="";
        var first=await CurlProbeAsync("https://api.ipify.org",true,token);
        if(first.Ok){success++;exitIp=first.Body;}
        var second=await CurlProbeAsync(
            "https://www.cloudflare.com/cdn-cgi/trace",false,token
        );
        if(second.Ok)success++;
        var third=await CurlProbeAsync(
            "https://www.google.com/generate_204",false,token
        );
        if(third.Ok)success++;
        if(success<2)throw new InvalidOperationException("VPN_EGRESS_FAILED");
        return string.IsNullOrWhiteSpace(exitIp)?"проверен":exitIp;
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
        RestoreProxy();
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

    async Task ConnectAsync()
    {
        if(!await connectionGate.WaitAsync(0))return;
        connect.Enabled=false;
        connectCts?.Cancel();
        connectCts?.Dispose();
        connectCts=new CancellationTokenSource();
        var token=connectCts.Token;
        stages.Clear();
        stages.Add(new("install","ok","CLIENT_STARTED"));

        try {
            DisconnectCore(false);
            token.ThrowIfCancellationRequested();
            if(!File.Exists(XrayPath))
                throw new InvalidOperationException("XRAY_NOT_INSTALLED");

            socksPort=AllocateSocksPort();
            status.Text="Получаем актуальный VPN-профиль...";
            var profile=await FetchProfileAsync(token);
            stages.Add(new("parse","ok","PROFILE_VALID"));

            var config=BuildXrayConfig(profile);
            AtomicWrite(ConfigPath,config);

            xray=Process.Start(new ProcessStartInfo(
                XrayPath,$"run -config \"{ConfigPath}\""
            ){
                UseShellExecute=false,
                CreateNoWindow=true
            });
            if(xray is null)throw new InvalidOperationException("XRAY_START_FAILED");

            var ready=false;
            for(var i=0;i<30;i++) {
                token.ThrowIfCancellationRequested();
                await Task.Delay(250,token);
                if(await SocksReadyAsync(token)){ready=true;break;}
                if(xray.HasExited)break;
            }
            if(!ready)throw new InvalidOperationException("LOCAL_XRAY_FAILED");
            stages.Add(new("socks","ok","SOCKS_LISTENER_OK"));

            var ip=await CheckEgressAsync(token);
            token.ThrowIfCancellationRequested();
            stages.Add(new("https","ok","VPN_EGRESS_VERIFIED"));

            CaptureAndEnableProxy();
            stages.Add(new("proxy","ok","WINDOWS_PROXY_ENABLED"));
            status.Text=$"Подключено. VPN IP: {ip}";
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

    void DisconnectCore(bool show)
    {
        try {connectCts?.Cancel();} catch {}
        RestoreProxy();
        try {if(xray is {HasExited:false})xray.Kill(true);} catch {}
        try {xray?.Dispose();} catch {}
        xray=null;
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
            foreach(var item in items.Take(5)) {
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
                    remaining.Add(item);
                    remaining.AddRange(items.Skip(remaining.Count));
                    break;
                }
            }
            if(remaining.Count==0)File.Delete(PendingPath);
            else AtomicWrite(PendingPath,JsonSerializer.Serialize(remaining.TakeLast(5)));
        } catch {}
    }
}
