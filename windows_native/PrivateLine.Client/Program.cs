using System;
using System.Threading;
using System.Windows.Forms;

namespace PrivateLine;

internal static class Program
{
    const string ClientMutexName=@"Local\PrivateLineClientV2";

    [STAThread]
    static void Main()
    {
        ApplicationConfiguration.Initialize();
        using var mutex=new Mutex(true,ClientMutexName,out var owns);
        if(!owns) {
            MessageBox.Show(
                "Private Line уже запущен.",
                "Private Line",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information
            );
            return;
        }
        Application.Run(new MainForm());
    }
}
