using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

// Extract the Python payload only beneath the outer executable, before it starts.
internal static class Launcher
{
    [STAThread]
    private static int Main(string[] args)
    {
        string home = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string run = null;
        try
        {
            string data = Path.Combine(home, "ecam_recordSTT_data");
            string runtime = Path.Combine(data, "runtime");
            foreach (string folder in new string[] { data, runtime })
            {
                Directory.CreateDirectory(folder);
                if ((File.GetAttributes(folder) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("Storage folders must not be links to other locations.");
            }
            do { run = Path.Combine(runtime, Guid.NewGuid().ToString("N").Substring(0, 12)); }
            while (Directory.Exists(run));
            Directory.CreateDirectory(run);
            string payload = Path.Combine(run, "engine.exe");
            using (Stream input = Assembly.GetExecutingAssembly().GetManifestResourceStream("engine"))
            using (FileStream output = File.Create(payload)) input.CopyTo(output);
            string[] quoted = Array.ConvertAll(args, Quote);
            var info = new ProcessStartInfo(payload, String.Join(" ", quoted));
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.WorkingDirectory = home;
            info.EnvironmentVariables["ECAM_EXE_HOME"] = home;
            info.EnvironmentVariables["TEMP"] = run;
            info.EnvironmentVariables["TMP"] = run;
            using (Process child = Process.Start(info))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "ecam recordSTT", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            if (run != null && Directory.Exists(run))
            {
                try { Directory.Delete(run, true); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }

    private static string Quote(string value)
    {
        // Windows command-line quoting, including trailing backslashes.
        var result = new System.Text.StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            result.Append('\\', ch == '"' ? slashes * 2 + 1 : slashes);
            result.Append(ch);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }
}
