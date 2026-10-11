using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http.Json;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Net.Security;
using System.Text;
using System.Text.Json;
using Microsoft.Win32;

namespace PrivateLineSetup;

internal static class Program
{
    const string Marker="PRIVATE_LINE_BOOTSTRAP_V1:";

    [STAThread]
    static async Task Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        var dir=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"PrivateLine");
        Directory.CreateDirectory(dir);

        if(args.Any(a=>string.Equals(a,"--client",StringComparison.OrdinalIgnoreCase))) {
            Application.Run(new PrivateLine.MainForm());
            return;
        }

        Bootstrap? boot=null;
        try {
            boot=ReadBootstrap();
            var app=Path.Combine(dir,"PrivateLine.exe");
            File.Copy(Environment.ProcessPath!,app,true);
            InstallEmbeddedXray(dir);
            await Pair(boot,dir);
            CreateShortcuts(app,dir);
            Process.Start(new ProcessStartInfo(app,"--client"){UseShellExecute=true,WorkingDirectory=dir});
            MessageBox.Show("Private Line установлен.\n\nКлиент запущен и уже привязан к вашей VPN-подписке. Ничего вводить не нужно.","Private Line",MessageBoxButtons.OK,MessageBoxIcon.Information);
        } catch(Exception ex) {
            var code=SafeCode(ex.Message);
            if(boot is not null) {
                try { await ReportInstallFailure(boot,code); } catch {}
            }
            MessageBox.Show("Установка не завершена.\n\nОшибка: "+code+"\n\nСкачайте свежий персональный PrivateLineSetup.exe из админки и запустите снова.","Private Line",MessageBoxButtons.OK,MessageBoxIcon.Error);
            Environment.ExitCode=1;
        }
    }

    record Bootstrap(string api_base,string pair_code,string tls_pin);
    record PairResponse(bool ok,string device_token);

    static HttpClient PinnedHttp(string pin)
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
        return new HttpClient(handler){Timeout=TimeSpan.FromSeconds(45)};
    }

    static Bootstrap ReadBootstrap()
    {
        var bytes=File.ReadAllBytes(Environment.ProcessPath!);
        var marker=Encoding.ASCII.GetBytes(Marker);
        var pos=LastIndexOf(bytes,marker);
        if(pos<0) throw new InvalidOperationException("BOOTSTRAP_MISSING");
        var payload=Encoding.UTF8.GetString(bytes,pos+marker.Length,bytes.Length-pos-marker.Length);
        var b=JsonSerializer.Deserialize<Bootstrap>(payload)??throw new InvalidOperationException("BOOTSTRAP_INVALID");
        if(!Uri.TryCreate(b.api_base,UriKind.Absolute,out var u)||u.Scheme!="https"||u.AbsolutePath!="/"||string.IsNullOrWhiteSpace(b.pair_code)||!b.tls_pin.StartsWith("sha256//",StringComparison.Ordinal))
            throw new InvalidOperationException("BOOTSTRAP_INVALID");
        return b;
    }

    static int LastIndexOf(byte[] source,byte[] pattern)
    {
        for(int i=source.Length-pattern.Length;i>=0;i--){var ok=true;for(int j=0;j<pattern.Length;j++)if(source[i+j]!=pattern[j]){ok=false;break;}if(ok)return i;}return -1;
    }

    static void ExtractResource(string name,string path)
    {
        var asm=typeof(Program).Assembly;
        using var s=asm.GetManifestResourceStream(name)??throw new InvalidOperationException("CLIENT_PAYLOAD_MISSING");
        using var f=File.Create(path);s.CopyTo(f);
    }

    static void InstallEmbeddedXray(string dir)
    {
        var xray=Path.Combine(dir,"xray.exe");
        ExtractResource("xray.exe",xray);
        var p=Process.Start(new ProcessStartInfo(xray,"version"){UseShellExecute=false,RedirectStandardOutput=true,RedirectStandardError=true,CreateNoWindow=true})
            ?? throw new InvalidOperationException("XRAY_START_FAILED");
        var line=p.StandardOutput.ReadLine(); p.WaitForExit();
        if(p.ExitCode!=0 || line?.StartsWith("Xray 26.6.27",StringComparison.Ordinal)!=true)
            throw new InvalidOperationException("XRAY_VERSION_INVALID");
    }

    static async Task<bool> XrayOk(string path)
    {
        try {
            var p=Process.Start(new ProcessStartInfo(path,"version"){UseShellExecute=false,RedirectStandardOutput=true,RedirectStandardError=true,CreateNoWindow=true});
            if(p==null)return false;var line=await p.StandardOutput.ReadLineAsync();await p.WaitForExitAsync();
            return p.ExitCode==0 && line?.StartsWith("Xray 26.6.27",StringComparison.Ordinal)==true;
        }catch{return false;}
    }

    static async Task ReportInstallFailure(Bootstrap b,string code)
    {
        using var http=PinnedHttp(b.tls_pin);
        var payload=new {pair_code=b.pair_code,code,windows=Environment.OSVersion.Version.ToString(),version="2.0.0"};
        using var response=await http.PostAsJsonAsync(b.api_base.TrimEnd('/')+"/api/windows/install-report",payload);
    }

    static async Task Pair(Bootstrap b,string dir)
    {
        using var http=PinnedHttp(b.tls_pin);
        using var res=await http.PostAsJsonAsync(b.api_base.TrimEnd('/')+"/api/windows/enroll",new{code=b.pair_code});
        if(!res.IsSuccessStatusCode)throw new InvalidOperationException("PAIRING_FAILED");
        var p=await res.Content.ReadFromJsonAsync<PairResponse>()??throw new InvalidOperationException("PAIRING_RESPONSE_INVALID");
        if(!p.ok||p.device_token.Length<45)throw new InvalidOperationException("PAIRING_RESPONSE_INVALID");
        var sealedToken=ProtectedData.Protect(Encoding.UTF8.GetBytes(p.device_token),null,DataProtectionScope.CurrentUser);
        var obj=new{api_base=b.api_base,token_dpapi=Convert.ToBase64String(sealedToken),tls_pin=b.tls_pin,version=2};
        await File.WriteAllTextAsync(Path.Combine(dir,"device.json"),JsonSerializer.Serialize(obj),new UTF8Encoding(false));
        using var check=new HttpRequestMessage(HttpMethod.Get,b.api_base.TrimEnd('/')+"/api/windows/profile");
        check.Headers.Authorization=new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer",p.device_token);
        using var chk=await http.SendAsync(check);if(!chk.IsSuccessStatusCode)throw new InvalidOperationException("PROFILE_UNAVAILABLE");
    }

    static void CreateShortcuts(string app,string dir)
    {
        var desktop=Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
        CreateShortcut(Path.Combine(desktop,"Private Line.lnk"),app,dir);
        var programs=Environment.GetFolderPath(Environment.SpecialFolder.Programs);
        CreateShortcut(Path.Combine(programs,"Private Line.lnk"),app,dir);
    }

    static void CreateShortcut(string shortcutPath,string target,string workdir)
    {
        var shell=Type.GetTypeFromProgID("WScript.Shell")??throw new InvalidOperationException("SHORTCUT_API_UNAVAILABLE");
        dynamic wsh=Activator.CreateInstance(shell)!;dynamic link=wsh.CreateShortcut(shortcutPath);
        link.TargetPath=target;link.WorkingDirectory=workdir;link.Description="Private Line VPN";link.Save();
    }

    static string SafeCode(string s)
    {
        var x=new string(s.Where(c=>char.IsAsciiLetterOrDigit(c)||c=='_').ToArray());
        return string.IsNullOrEmpty(x)?"INSTALL_FAILED":x[..Math.Min(64,x.Length)];
    }
}
