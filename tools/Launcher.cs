using System;
using System.IO;
using System.Diagnostics;
using System.Windows.Forms;
internal static class Launcher {
    [STAThread]
    private static void Main() {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        try {
            var info = new ProcessStartInfo(Path.Combine(dir, @"runtime\pythonw.exe"),
                "-B -E -s \"" + Path.Combine(dir, @"app\drawing_app.py") + "\"");
            info.WorkingDirectory = dir;
            info.UseShellExecute = false;
            info.EnvironmentVariables["TCL_LIBRARY"] = Path.Combine(dir, @"runtime\tcl\tcl8.6");
            info.EnvironmentVariables["TK_LIBRARY"] = Path.Combine(dir, @"runtime\tcl\tk8.6");
            Process.Start(info);
        } catch (Exception ex) {
            MessageBox.Show("Распакуйте ZIP целиком и повторите запуск.\n" + ex.Message,
                "Чертежи ЕСКД", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
