using System.Diagnostics;
using System.Net.Http.Json;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Net.Security;
using System.Text;
using System.Text.Json;

namespace PrivateLineSetup;

internal static class Program
{
    const string Marker="PRIVATE_LINE_BOOTSTRAP_V1:";
    const string InstallMutexName=@"Local\PrivateLineSetupV2";
    const string ClientMutexName=@"Local\PrivateLineClientV2";

    [STAThread]
    static async Task Main(string[] args)
    {
        ApplicationConfiguration.Initialize();

        if(args.Any(a=>string.Equals(a,"--client",StringComparison.OrdinalIgnoreCase))) {
            using var clientMutex=new Mutex(true,ClientMutexName,out var ownsClient);
            if(!ownsClient) {
                MessageBox.Show(
                    "Private Line уже запущен.",
                    "Private Line",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Information
                );
                return;
            }
            Application.Run(new PrivateLine.MainForm());
            return;
        }

        var dir=Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "PrivateLine"
        );
        Directory.CreateDirectory(dir);

        FileStream installLock;
        try {
            installLock=new FileStream(
                Path.Combine(dir,"install.lock"),
                FileMode.OpenOrCreate,
                FileAccess.ReadWrite,
                FileShare.None
            );
        } catch(IOException) {
            MessageBox.Show(
                "Установка Private Line уже выполняется. Дождитесь завершения текущего окна.",
                "Private Line",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information
            );
            return;
        }
        using(installLock) {

        Bootstrap? boot=null;
        try {
            EnsureClientNotRunning();
            boot=ReadBootstrap();

            var app=Path.Combine(dir,"PrivateLine.exe");
            InstallApplicationImage(app);
            await InstallEmbeddedXrayAsync(dir);
            await PairAsync(boot,dir);
            CreateShortcuts(app,dir);

            Process.Start(new ProcessStartInfo(app,"--client") {
                UseShellExecute=true,
                WorkingDirectory=dir
            });
            MessageBox.Show(
                "Private Line установлен.\n\nКлиент запущен и уже привязан к вашей VPN-подписке. Ничего вводить не нужно.",
                "Private Line",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information
            );
        } catch(Exception ex) {
            var code=SafeCode(ex.Message);
            try {
                File.WriteAllText(
                    Path.Combine(dir,"install-error.txt"),
                    DateTime.UtcNow.ToString("O")+" "+code+Environment.NewLine,
                    new UTF8Encoding(false)
                );
            } catch {}

            if(boot is not null) {
                try { await ReportInstallFailureAsync(boot,code); } catch {}
            }
            MessageBox.Show(
                "Установка не завершена.\n\nОшибка: "+code+
                "\n\nЗакройте Private Line, если он запущен, и повторите установку. При необходимости скачайте свежий персональный PrivateLineSetup.exe.",
                "Private Line",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            Environment.ExitCode=1;
        }
        }
    }

    record Bootstrap(string api_base,string pair_code,string tls_pin);
    record PairResponse(bool ok,string device_token);

    static void EnsureClientNotRunning()
    {
        using var probe=new Mutex(false,ClientMutexName);
        var acquired=false;
        try {
            acquired=probe.WaitOne(0);
            if(!acquired) throw new InvalidOperationException("CLIENT_RUNNING_CLOSE_FIRST");
        } catch(AbandonedMutexException) {
            acquired=true;
        } finally {
            if(acquired) {
                try { probe.ReleaseMutex(); } catch(ApplicationException) {}
            }
        }
    }

    static HttpClient PinnedHttp(string pin,TimeSpan? timeout=null)
    {
        var handler=new HttpClientHandler();
        handler.ServerCertificateCustomValidationCallback=(request,cert,chain,errors)=>{
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
                return CryptographicOperations.FixedTimeEquals(
                    Encoding.ASCII.GetBytes(actual),
                    Encoding.ASCII.GetBytes(pin)
                );
            } catch {return false;}
        };
        return new HttpClient(handler){Timeout=timeout??TimeSpan.FromSeconds(15)};
    }

    static Bootstrap ReadBootstrap()
    {
        var path=Environment.ProcessPath
            ??throw new InvalidOperationException("INSTALLER_PATH_MISSING");
        var marker=Encoding.ASCII.GetBytes(Marker);
        using var input=new FileStream(
            path,FileMode.Open,FileAccess.Read,FileShare.Read,16*1024,
            FileOptions.RandomAccess
        );
        var tailLen=(int)Math.Min(input.Length,8192);
        var tail=new byte[tailLen];
        input.Seek(-tailLen,SeekOrigin.End);
        input.ReadExactly(tail);
        var pos=LastIndexOf(tail,marker);
        if(pos<0) throw new InvalidOperationException("BOOTSTRAP_MISSING");
        var payload=Encoding.UTF8.GetString(
            tail,pos+marker.Length,tail.Length-pos-marker.Length
        );
        var b=JsonSerializer.Deserialize<Bootstrap>(payload)
            ??throw new InvalidOperationException("BOOTSTRAP_INVALID");
        if(
            !Uri.TryCreate(b.api_base,UriKind.Absolute,out var u)
            || u.Scheme!="https"
            || u.AbsolutePath!="/"
            || string.IsNullOrWhiteSpace(b.pair_code)
            || !b.tls_pin.StartsWith("sha256//",StringComparison.Ordinal)
        ) throw new InvalidOperationException("BOOTSTRAP_INVALID");
        return b;
    }

    static int LastIndexOf(byte[] source,byte[] pattern)
    {
        for(int i=source.Length-pattern.Length;i>=0;i--) {
            var ok=true;
            for(int j=0;j<pattern.Length;j++) {
                if(source[i+j]!=pattern[j]) {ok=false;break;}
            }
            if(ok)return i;
        }
        return -1;
    }

    static void InstallApplicationImage(string destination)
    {
        var source=Environment.ProcessPath
            ??throw new InvalidOperationException("INSTALLER_PATH_MISSING");
        var marker=Encoding.ASCII.GetBytes(Marker);
        var temp=destination+".new."+Guid.NewGuid().ToString("N");
        try {
            using var input=new FileStream(
                source,FileMode.Open,FileAccess.Read,FileShare.Read,1024*1024,
                FileOptions.SequentialScan
            );
            var tailLen=(int)Math.Min(input.Length,8192);
            var tail=new byte[tailLen];
            input.Seek(-tailLen,SeekOrigin.End);
            input.ReadExactly(tail);
            var markerInTail=LastIndexOf(tail,marker);
            if(markerInTail<0) throw new InvalidOperationException("BOOTSTRAP_MISSING");
            var baseLength=input.Length-tailLen+markerInTail;
            if(baseLength<100_000) throw new InvalidOperationException("INSTALLER_IMAGE_INVALID");

            input.Position=0;
            using(var output=new FileStream(
                temp,FileMode.CreateNew,FileAccess.Write,FileShare.None,1024*1024,
                FileOptions.WriteThrough
            )) {
                var buffer=new byte[1024*1024];
                long remaining=baseLength;
                while(remaining>0) {
                    var read=input.Read(buffer,0,(int)Math.Min(buffer.Length,remaining));
                    if(read<=0) throw new EndOfStreamException();
                    output.Write(buffer,0,read);
                    remaining-=read;
                }
                output.Flush(true);
            }
            File.Move(temp,destination,true);
        } finally {
            try { if(File.Exists(temp))File.Delete(temp); } catch {}
        }
    }

    static void ExtractResource(string name,string path)
    {
        var asm=typeof(Program).Assembly;
        using var s=asm.GetManifestResourceStream(name)
            ??throw new InvalidOperationException("CLIENT_PAYLOAD_MISSING");
        using var f=new FileStream(
            path,FileMode.CreateNew,FileAccess.Write,FileShare.None,1024*1024,
            FileOptions.WriteThrough
        );
        s.CopyTo(f);
        f.Flush(true);
    }

    static async Task InstallEmbeddedXrayAsync(string dir)
    {
        var xray=Path.Combine(dir,"xray.exe");
        if(await XrayOkAsync(xray))return;

        var temp=Path.Combine(dir,"xray.new."+Guid.NewGuid().ToString("N")+".exe");
        try {
            ExtractResource("xray.exe",temp);
            if(!await XrayOkAsync(temp))
                throw new InvalidOperationException("XRAY_VERSION_INVALID");
            File.Move(temp,xray,true);
            if(!await XrayOkAsync(xray))
                throw new InvalidOperationException("XRAY_INSTALL_VERIFICATION_FAILED");
        } catch(IOException) {
            throw new InvalidOperationException("XRAY_IN_USE_CLOSE_CLIENT_FIRST");
        } finally {
            try { if(File.Exists(temp))File.Delete(temp); } catch {}
        }
    }

    static async Task<bool> XrayOkAsync(string path)
    {
        if(!File.Exists(path))return false;
        try {
            using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(5));
            using var p=Process.Start(new ProcessStartInfo(path,"version") {
                UseShellExecute=false,
                RedirectStandardOutput=true,
                RedirectStandardError=true,
                CreateNoWindow=true
            });
            if(p==null)return false;
            var line=await p.StandardOutput.ReadLineAsync(timeout.Token);
            await p.WaitForExitAsync(timeout.Token);
            return p.ExitCode==0
                && line?.StartsWith("Xray 26.6.27",StringComparison.Ordinal)==true;
        } catch {return false;}
    }

    static async Task ReportInstallFailureAsync(Bootstrap b,string code)
    {
        using var http=PinnedHttp(b.tls_pin,TimeSpan.FromSeconds(5));
        var payload=new {
            pair_code=b.pair_code,
            code,
            windows=Environment.OSVersion.Version.ToString(),
            version="2.1.0"
        };
        try {
            using var response=await http.PostAsJsonAsync(
                b.api_base.TrimEnd('/')+"/api/windows/install-report",payload
            );
        } catch {}
    }

    static async Task PairAsync(Bootstrap b,string dir)
    {
        using var http=PinnedHttp(b.tls_pin,TimeSpan.FromSeconds(15));
        using var res=await http.PostAsJsonAsync(
            b.api_base.TrimEnd('/')+"/api/windows/enroll",
            new{code=b.pair_code}
        );
        if(!res.IsSuccessStatusCode)throw new InvalidOperationException("PAIRING_FAILED");
        var p=await res.Content.ReadFromJsonAsync<PairResponse>()
            ??throw new InvalidOperationException("PAIRING_RESPONSE_INVALID");
        if(!p.ok||p.device_token.Length<45)
            throw new InvalidOperationException("PAIRING_RESPONSE_INVALID");

        var sealedToken=ProtectedData.Protect(
            Encoding.UTF8.GetBytes(p.device_token),
            null,
            DataProtectionScope.CurrentUser
        );
        var obj=new {
            api_base=b.api_base,
            token_dpapi=Convert.ToBase64String(sealedToken),
            tls_pin=b.tls_pin,
            version=3
        };
        var target=Path.Combine(dir,"device.json");
        var temp=target+".new";
        await File.WriteAllTextAsync(
            temp,JsonSerializer.Serialize(obj),new UTF8Encoding(false)
        );
        File.Move(temp,target,true);
        // Enrollment itself validates the active subscription. Do not perform
        // an extra /profile request during installation: first Connect fetches
        // the current profile once, reducing install-time control-plane load.
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
        var shell=Type.GetTypeFromProgID("WScript.Shell")
            ??throw new InvalidOperationException("SHORTCUT_API_UNAVAILABLE");
        dynamic wsh=Activator.CreateInstance(shell)!;
        dynamic link=wsh.CreateShortcut(shortcutPath);
        link.TargetPath=target;
        link.WorkingDirectory=workdir;
        link.Description="Private Line VPN";
        link.Save();
        try { Marshal.FinalReleaseComObject(link); } catch {}
        try { Marshal.FinalReleaseComObject(wsh); } catch {}
    }

    static string SafeCode(string s)
    {
        var x=new string(s.Where(c=>char.IsAsciiLetterOrDigit(c)||c=='_').ToArray());
        return string.IsNullOrEmpty(x)?"INSTALL_FAILED":x[..Math.Min(64,x.Length)];
    }
}
