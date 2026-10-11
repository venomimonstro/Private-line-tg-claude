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
using Microsoft.Win32;

namespace PrivateLine;

public sealed class MainForm : Form
{
    const int SocksPort = 19590;
    readonly string BaseDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PrivateLine");
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
    Process? xray;
    int previousProxyEnable;
    string previousProxyServer = "";
    bool proxyCaptured;

    [DllImport("wininet.dll", SetLastError=true)]
    static extern bool InternetSetOption(IntPtr hInternet, int option, IntPtr buffer, int bufferLength);

    public MainForm()
    {
        XrayPath = Path.Combine(BaseDir, "xray.exe");
        DevicePath = Path.Combine(BaseDir, "device.json");
        ConfigPath = Path.Combine(BaseDir, "client.json");
        StatePath = Path.Combine(BaseDir, "state.json");
        PendingPath = Path.Combine(BaseDir, "pending-reports.json");

        Text = "Private Line";
        Width = 560; Height = 430;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        FormBorderStyle = FormBorderStyle.FixedSingle;

        var title = new Label { Left=24, Top=22, Width=480, Height=32, Text="PRIVATE LINE", Font=new Font("Segoe UI",18,FontStyle.Bold) };
        var subtitle = new Label { Left=24, Top=62, Width=485, Height=44, Text="Профиль VPN загружается автоматически. IP, ключ и токен вводить не нужно." };
        status.Left=24; status.Top=120; status.Width=485; status.Height=60; status.Text="Не подключено.";

        connect.Left=24; connect.Top=195; connect.Width=235; connect.Height=44; connect.Text="Подключить VPN";
        disconnect.Left=274; disconnect.Top=195; disconnect.Width=235; disconnect.Height=44; disconnect.Text="Отключить";
        diagnostics.Left=24; diagnostics.Top=260; diagnostics.Width=485; diagnostics.Height=44;
        diagnostics.Text="Автоматически отправлять обезличенные коды ошибок"; diagnostics.Checked=true;
        send.Left=24; send.Top=315; send.Width=485; send.Height=36; send.Text="Отправить диагностику сейчас";

        Controls.AddRange([title,subtitle,status,connect,disconnect,diagnostics,send]);
        connect.Click += async (_,_) => await ConnectAsync();
        disconnect.Click += (_,_) => Disconnect();
        send.Click += async (_,_) => status.Text = await SendReportAsync();
        FormClosing += (_,_) => Disconnect();

        Directory.CreateDirectory(BaseDir);
        _ = FlushPendingAsync();
        status.Text = File.Exists(DevicePath)
            ? "Установка привязана к подписке. Нажмите «Подключить VPN»."
            : "Нет привязки. Скачайте новый персональный PrivateLineSetup.exe.";
    }

    record Device(string api_base, string token_dpapi, string tls_pin);
    record Stage(string stage, string result, string code);

    (string BaseUrl,string Token,string TlsPin) ReadDevice()
    {
        if (!File.Exists(DevicePath)) throw new InvalidOperationException("DEVICE_NOT_PAIRED");
        var d = JsonSerializer.Deserialize<Device>(File.ReadAllText(DevicePath)) ?? throw new InvalidOperationException("DEVICE_INVALID");
        if (!Uri.TryCreate(d.api_base, UriKind.Absolute, out var uri) || uri.Scheme != "https" || uri.AbsolutePath != "/")
            throw new InvalidOperationException("DEVICE_SERVER_INVALID");
        var token = Encoding.UTF8.GetString(ProtectedData.Unprotect(Convert.FromBase64String(d.token_dpapi), null, DataProtectionScope.CurrentUser));
        if (token.Length < 45) throw new InvalidOperationException("DEVICE_TOKEN_INVALID");
        if(string.IsNullOrWhiteSpace(d.tls_pin)||!d.tls_pin.StartsWith("sha256//",StringComparison.Ordinal)) throw new InvalidOperationException("DEVICE_TLS_PIN_INVALID");
        return (d.api_base.TrimEnd('/'),token,d.tls_pin);
    }

    HttpClient PinnedHttp(string pin)
    {
        var handler=new HttpClientHandler();
        handler.ServerCertificateCustomValidationCallback=(request,cert,chain,errors)=>{
            if(errors==SslPolicyErrors.None)return true;
            if(cert is null)return false;
            try {
                using var rsa=cert.GetRSAPublicKey();
                byte[] spki;
                if(rsa is not null) spki=rsa.ExportSubjectPublicKeyInfo();
                else {
                    using var ecdsa=cert.GetECDsaPublicKey();
                    if(ecdsa is null)return false;
                    spki=ecdsa.ExportSubjectPublicKeyInfo();
                }
                var actual="sha256//"+Convert.ToBase64String(SHA256.HashData(spki));
                return CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(actual),Encoding.ASCII.GetBytes(pin));
            } catch {return false;}
        };
        return new HttpClient(handler){Timeout=TimeSpan.FromSeconds(20)};
    }

    async Task<string> FetchProfileAsync()
    {
        var d=ReadDevice();
        using var http=PinnedHttp(d.TlsPin);
        using var req=new HttpRequestMessage(HttpMethod.Get,d.BaseUrl+"/api/windows/profile");
        req.Headers.Authorization=new AuthenticationHeaderValue("Bearer",d.Token);
        using var res=await http.SendAsync(req);
        if(!res.IsSuccessStatusCode) throw new InvalidOperationException("PROFILE_FETCH_FAILED");
        using var doc=JsonDocument.Parse(await res.Content.ReadAsStringAsync());
        var url=doc.RootElement.GetProperty("vless_url").GetString();
        if(string.IsNullOrWhiteSpace(url)||!url.StartsWith("vless://",StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("PROFILE_INVALID");
        return url;
    }

    static Dictionary<string,string> Query(Uri uri)
    {
        var d=new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);
        foreach(var part in uri.Query.TrimStart('?').Split('&',StringSplitOptions.RemoveEmptyEntries))
        {
            var x=part.Split('=',2); var k=Uri.UnescapeDataString(x[0]); var v=x.Length>1?Uri.UnescapeDataString(x[1]):"";
            if(!d.TryAdd(k,v)) throw new InvalidOperationException("PROFILE_DUPLICATE_PARAM");
        }
        return d;
    }

    string BuildXrayConfig(string raw)
    {
        var uri=new Uri(raw);
        var q=Query(uri);
        if(uri.Scheme!="vless" || !Guid.TryParse(uri.UserInfo,out var id) || uri.Port<1 ||
           q.GetValueOrDefault("security")!="reality" || q.GetValueOrDefault("flow")!="xtls-rprx-vision")
            throw new InvalidOperationException("PROFILE_INVALID");
        var obj=new {
            log=new {loglevel="warning"},
            inbounds=new[]{new {listen="127.0.0.1",port=SocksPort,protocol="socks",settings=new {auth="noauth",udp=false}}},
            outbounds=new[]{new {
                protocol="vless",
                settings=new {vnext=new[]{new {address=uri.Host,port=uri.Port,users=new[]{new {id=id.ToString(),encryption="none",flow="xtls-rprx-vision"}}}}},
                streamSettings=new {network="tcp",security="reality",realitySettings=new {
                    serverName=q.GetValueOrDefault("sni",""),fingerprint=q.GetValueOrDefault("fp","chrome"),
                    password=q.GetValueOrDefault("pbk",""),shortId=q.GetValueOrDefault("sid",""),spiderX=""
                }}
            }}
        };
        return JsonSerializer.Serialize(obj);
    }

    async Task<bool> SocksReadyAsync()
    {
        try {
            using var c=new TcpClient(); using var cts=new CancellationTokenSource(1200);
            await c.ConnectAsync(IPAddress.Loopback,SocksPort,cts.Token);
            var s=c.GetStream(); await s.WriteAsync(new byte[]{5,1,0},cts.Token);
            var b=new byte[2]; var n=await s.ReadAsync(b,cts.Token);
            return n==2&&b[0]==5&&b[1]==0;
        } catch { return false; }
    }

    async Task<string> CheckEgressAsync()
    {
        var psi=new ProcessStartInfo(Path.Combine(Environment.SystemDirectory,"curl.exe"),
            $"--silent --show-error --fail --ipv4 --socks5-hostname 127.0.0.1:{SocksPort} --connect-timeout 8 --max-time 18 https://api.ipify.org")
        {UseShellExecute=false,RedirectStandardOutput=true,RedirectStandardError=true,CreateNoWindow=true};
        using var p=Process.Start(psi) ?? throw new InvalidOperationException("CURL_START_FAILED");
        var output=await p.StandardOutput.ReadToEndAsync(); await p.WaitForExitAsync();
        if(p.ExitCode!=0 || !IPAddress.TryParse(output.Trim(),out _)) throw new InvalidOperationException("VPN_EGRESS_FAILED");
        return output.Trim();
    }

    void CaptureAndEnableProxy()
    {
        using var key=Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Internet Settings",true)
            ?? throw new InvalidOperationException("PROXY_REGISTRY_FAILED");
        previousProxyEnable=Convert.ToInt32(key.GetValue("ProxyEnable",0));
        previousProxyServer=Convert.ToString(key.GetValue("ProxyServer","")) ?? "";
        proxyCaptured=true;
        key.SetValue("ProxyServer",$"socks=127.0.0.1:{SocksPort}");
        key.SetValue("ProxyEnable",1,RegistryValueKind.DWord);
        InternetSetOption(IntPtr.Zero,39,IntPtr.Zero,0); InternetSetOption(IntPtr.Zero,37,IntPtr.Zero,0);
    }

    void RestoreProxy()
    {
        if(!proxyCaptured) return;
        try {
            using var key=Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Internet Settings",true);
            key?.SetValue("ProxyEnable",previousProxyEnable,RegistryValueKind.DWord);
            key?.SetValue("ProxyServer",previousProxyServer);
            InternetSetOption(IntPtr.Zero,39,IntPtr.Zero,0); InternetSetOption(IntPtr.Zero,37,IntPtr.Zero,0);
        } catch {}
        proxyCaptured=false;
    }

    async Task ConnectAsync()
    {
        connect.Enabled=false; stages.Clear(); stages.Add(new("install","ok","CLIENT_STARTED"));
        try {
            if(!File.Exists(XrayPath)) throw new InvalidOperationException("XRAY_NOT_INSTALLED");
            status.Text="Получаем актуальный VPN-профиль...";
            var profile=await FetchProfileAsync(); stages.Add(new("parse","ok","PROFILE_VALID"));
            await File.WriteAllTextAsync(ConfigPath,BuildXrayConfig(profile),new UTF8Encoding(false));
            DisconnectCore(false);
            xray=Process.Start(new ProcessStartInfo(XrayPath,$"run -config \"{ConfigPath}\""){UseShellExecute=false,CreateNoWindow=true});
            if(xray is null) throw new InvalidOperationException("XRAY_START_FAILED");
            var ok=false;
            for(int i=0;i<30;i++){await Task.Delay(300);if(await SocksReadyAsync()){ok=true;break;}}
            if(!ok) throw new InvalidOperationException("LOCAL_XRAY_FAILED");
            stages.Add(new("socks","ok","SOCKS_LISTENER_OK"));
            var ip=await CheckEgressAsync(); stages.Add(new("https","ok","VPN_EGRESS_VERIFIED"));
            CaptureAndEnableProxy(); stages.Add(new("proxy","ok","WINDOWS_PROXY_ENABLED"));
            status.Text=$"Подключено. VPN IP: {ip}";
        } catch(Exception ex) {
            stages.Add(new("https","failed",SafeCode(ex.Message)));
            DisconnectCore(false);
            status.Text="VPN не подключён: "+SafeCode(ex.Message)+". "+await SendReportAsync();
        } finally {connect.Enabled=true;}
    }

    static string SafeCode(string value)
    {
        var s=new string(value.Where(c=>char.IsAsciiLetterOrDigit(c)||c=='_').ToArray());
        return string.IsNullOrEmpty(s)?"UNKNOWN_ERROR":s[..Math.Min(48,s.Length)];
    }

    void DisconnectCore(bool show)
    {
        RestoreProxy();
        try { if(xray is {HasExited:false}) xray.Kill(true); } catch {}
        xray?.Dispose(); xray=null;
        if(show) status.Text="Отключено.";
    }
    void Disconnect()=>DisconnectCore(true);

    async Task<string> SendReportAsync()
    {
        if(!diagnostics.Checked) return "Диагностика отключена.";
        var report=new {version="2.0.0",windows=Environment.OSVersion.Version.ToString(),consent=true,stages=stages};
        try {
            var d=ReadDevice();
            using var http=PinnedHttp(d.TlsPin);
            using var req=new HttpRequestMessage(HttpMethod.Post,d.BaseUrl+"/api/windows/report");
            req.Headers.Authorization=new AuthenticationHeaderValue("Bearer",d.Token);
            req.Content=new StringContent(JsonSerializer.Serialize(report),Encoding.UTF8,"application/json");
            using var res=await http.SendAsync(req);
            if(!res.IsSuccessStatusCode) throw new Exception();
            using var doc=JsonDocument.Parse(await res.Content.ReadAsStringAsync());
            return "Отчёт "+doc.RootElement.GetProperty("report_id").GetString()+" принят сервером.";
        } catch {
            await QueueReportAsync(report); return "Отчёт сохранён и будет отправлен позже.";
        }
    }

    async Task QueueReportAsync(object report)
    {
        List<JsonElement> items=[];
        try { if(File.Exists(PendingPath)) items=JsonSerializer.Deserialize<List<JsonElement>>(await File.ReadAllTextAsync(PendingPath))??[]; } catch {}
        using var d=JsonDocument.Parse(JsonSerializer.Serialize(report)); items.Add(d.RootElement.Clone());
        if(items.Count>10) items=items.TakeLast(10).ToList();
        await File.WriteAllTextAsync(PendingPath,JsonSerializer.Serialize(items),new UTF8Encoding(false));
    }

    async Task FlushPendingAsync()
    {
        if(!File.Exists(PendingPath)||!diagnostics.Checked)return;
        try {
            var d=ReadDevice(); using var http=PinnedHttp(d.TlsPin); var items=JsonSerializer.Deserialize<List<JsonElement>>(await File.ReadAllTextAsync(PendingPath))??[];
            var remaining=new List<JsonElement>();
            foreach(var item in items) {
                try {
                    using var req=new HttpRequestMessage(HttpMethod.Post,d.BaseUrl+"/api/windows/report");
                    req.Headers.Authorization=new AuthenticationHeaderValue("Bearer",d.Token);
                    req.Content=new StringContent(item.GetRawText(),Encoding.UTF8,"application/json");
                    using var res=await http.SendAsync(req); if(!res.IsSuccessStatusCode) remaining.Add(item);
                } catch {remaining.Add(item);}
            }
            if(remaining.Count==0)File.Delete(PendingPath);else await File.WriteAllTextAsync(PendingPath,JsonSerializer.Serialize(remaining));
        } catch {}
    }
}
