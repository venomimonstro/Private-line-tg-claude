using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http.Json;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Win32;

namespace PrivateLineSetup;

internal static class Program
{
    const string Marker="PRIVATE_LINE_BOOTSTRAP_V1:";
    static readonly HttpClient Http=new(){Timeout=TimeSpan.FromSeconds(45)};

    [STAThread]
    static async Task Main()
    {
        ApplicationConfiguration.Initialize();
        try {
            var boot=ReadBootstrap();
            var dir=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"PrivateLine");
            Directory.CreateDirectory(dir);
            var app=Path.Combine(dir,"PrivateLine.exe");
            ExtractResource("PrivateLine.exe",app);
            await InstallXray(dir);
            await Pair(boot,dir);
            CreateShortcuts(app,dir);
            Process.Start(new ProcessStartInfo(app){UseShellExecute=true,WorkingDirectory=dir});
            MessageBox.Show("Private Line установлен.\n\nКлиент запущен и уже привязан к вашей VPN-подписке. Ничего вводить не нужно.","Private Line",MessageBoxButtons.OK,MessageBoxIcon.Information);
        } catch(Exception ex) {
            var code=SafeCode(ex.Message);
            MessageBox.Show("Установка не завершена.\n\nОшибка: "+code+"\n\nСкачайте свежий персональный PrivateLineSetup.exe из админки и запустите снова.","Private Line",MessageBoxButtons.OK,MessageBoxIcon.Error);
            Environment.ExitCode=1;
        }
    }

    record Bootstrap(string api_base,string pair_code);
    record PairResponse(bool ok,string device_token);

    static Bootstrap ReadBootstrap()
    {
        var bytes=File.ReadAllBytes(Environment.ProcessPath!);
        var marker=Encoding.ASCII.GetBytes(Marker);
        var pos=LastIndexOf(bytes,marker);
        if(pos<0) throw new InvalidOperationException("BOOTSTRAP_MISSING");
        var payload=Encoding.UTF8.GetString(bytes,pos+marker.Length,bytes.Length-pos-marker.Length);
        var b=JsonSerializer.Deserialize<Bootstrap>(payload)??throw new InvalidOperationException("BOOTSTRAP_INVALID");
        if(!Uri.TryCreate(b.api_base,UriKind.Absolute,out var u)||u.Scheme!="https"||u.AbsolutePath!="/"||string.IsNullOrWhiteSpace(b.pair_code))
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

    static async Task InstallXray(string dir)
    {
        var xray=Path.Combine(dir,"xray.exe");
        if(File.Exists(xray)&&await XrayOk(xray))return;
        var zip=Path.Combine(Path.GetTempPath(),"pl-"+Guid.NewGuid().ToString("N")+".zip");
        var tmp=Path.Combine(Path.GetTempPath(),"pl-"+Guid.NewGuid().ToString("N"));
        try {
            using var response=await Http.GetAsync("https://github.com/XTLS/Xray-core/releases/download/v26.6.27/Xray-windows-64.zip",HttpCompletionOption.ResponseHeadersRead);
            response.EnsureSuccessStatusCode();
            await using(var output=File.Create(zip))await response.Content.CopyToAsync(output);
            if(new FileInfo(zip).Length<1_000_000)throw new InvalidOperationException("XRAY_DOWNLOAD_INCOMPLETE");
            ZipFile.ExtractToDirectory(zip,tmp,true);
            var found=Directory.GetFiles(tmp,"xray.exe",SearchOption.AllDirectories).FirstOrDefault()??throw new InvalidOperationException("XRAY_BINARY_MISSING");
            File.Copy(found,xray,true);
            if(!await XrayOk(xray))throw new InvalidOperationException("XRAY_VERSION_INVALID");
        } finally {try{File.Delete(zip);}catch{} try{Directory.Delete(tmp,true);}catch{}}
    }

    static async Task<bool> XrayOk(string path)
    {
        try {
            var p=Process.Start(new ProcessStartInfo(path,"version"){UseShellExecute=false,RedirectStandardOutput=true,RedirectStandardError=true,CreateNoWindow=true});
            if(p==null)return false;var line=await p.StandardOutput.ReadLineAsync();await p.WaitForExitAsync();
            return p.ExitCode==0 && line?.StartsWith("Xray 26.6.27",StringComparison.Ordinal)==true;
        }catch{return false;}
    }

    static async Task Pair(Bootstrap b,string dir)
    {
        using var res=await Http.PostAsJsonAsync(b.api_base.TrimEnd('/')+"/api/windows/enroll",new{code=b.pair_code});
        if(!res.IsSuccessStatusCode)throw new InvalidOperationException("PAIRING_FAILED");
        var p=await res.Content.ReadFromJsonAsync<PairResponse>()??throw new InvalidOperationException("PAIRING_RESPONSE_INVALID");
        if(!p.ok||p.device_token.Length<45)throw new InvalidOperationException("PAIRING_RESPONSE_INVALID");
        var sealedToken=ProtectedData.Protect(Encoding.UTF8.GetBytes(p.device_token),null,DataProtectionScope.CurrentUser);
        var obj=new{api_base=b.api_base,token_dpapi=Convert.ToBase64String(sealedToken),version=2};
        await File.WriteAllTextAsync(Path.Combine(dir,"device.json"),JsonSerializer.Serialize(obj),new UTF8Encoding(false));
        using var check=new HttpRequestMessage(HttpMethod.Get,b.api_base.TrimEnd('/')+"/api/windows/profile");
        check.Headers.Authorization=new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer",p.device_token);
        using var chk=await Http.SendAsync(check);if(!chk.IsSuccessStatusCode)throw new InvalidOperationException("PROFILE_UNAVAILABLE");
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
