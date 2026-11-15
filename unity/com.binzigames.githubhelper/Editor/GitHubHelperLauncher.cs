using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net;
using UnityEditor;
using UnityEngine;
using Debug = UnityEngine.Debug;

namespace BinziGames.GitHubHelper
{
    /// <summary>
    /// Opens GitHub Helper (the standalone app from this repository) straight on the current Unity project.
    /// Menu: Window > GitHub Helper (Ctrl/Cmd+Shift+G). If the app is not found it is downloaded once.
    /// </summary>
    public static class GitHubHelperLauncher
    {
        internal const string PrefAppPath = "BinziGames.GitHubHelper.AppPath";
        const string ZipUrl = "https://github.com/Binzigames/GitHubHelper/archive/refs/heads/main.zip";

        [MenuItem("Window/GitHub Helper %#g", false, 2100)]
        public static void Open()
        {
            GitHubHelperWindow.ShowWindow();
        }

        [MenuItem("Window/GitHub Helper (standalone app)", false, 2102)]
        public static void OpenExternal()
        {
            var app = EnsureApp();
            if (app != null) Run(() => Launch(app));
        }

        /// <summary>Finds the app, offering to download it. Returns null if unavailable or cancelled.</summary>
        internal static string EnsureApp()
        {
            string result = null;
            Run(() =>
            {
                result = FindApp();
                if (result != null) return;
                if (!EditorUtility.DisplayDialog("GitHub Helper",
                        "GitHub Helper is not installed on this computer yet.\n\nDownload it now from GitHub (about 1 MB) to:\n" + DefaultAppDir + "?",
                        "Download", "Cancel"))
                    return;
                result = Download();
            });
            return result;
        }

        internal static void Run(Action a)
        {
            try { a(); }
            catch (Exception e)
            {
                EditorUtility.ClearProgressBar();
                Debug.LogError("[GitHub Helper] " + e.Message);
                EditorUtility.DisplayDialog("GitHub Helper", e.Message, "OK");
            }
        }

        [MenuItem("Window/GitHub Helper (choose app folder...)", false, 2101)]
        static void ChooseFolder()
        {
            var dir = EditorUtility.OpenFolderPanel("Select the GitHub Helper folder (contains 'windows' and 'ui')", "", "");
            if (string.IsNullOrEmpty(dir)) return;
            if (!IsApp(dir)) { EditorUtility.DisplayDialog("GitHub Helper", "That folder does not look like GitHub Helper.", "OK"); return; }
            EditorPrefs.SetString(PrefAppPath, dir);
        }

        internal static string ProjectPath { get { return Directory.GetParent(Application.dataPath).FullName; } }

        internal static string DefaultAppDir
        {
            get
            {
                var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                return Path.Combine(local, "GitHubHelper", "app");
            }
        }

        static bool IsApp(string dir)
        {
            return !string.IsNullOrEmpty(dir)
                && File.Exists(Path.Combine(dir, "ui", "index.html"))
                && (File.Exists(Path.Combine(dir, "windows", "server.ps1")) || File.Exists(Path.Combine(dir, "linux", "server.py")));
        }

        internal static string FindApp()
        {
            var saved = EditorPrefs.GetString(PrefAppPath, "");
            if (IsApp(saved)) return saved;

            // Package embedded in a checkout of the repository: <repo>/unity/com.binzigames.githubhelper
            var pkg = UnityEditor.PackageManager.PackageInfo.FindForAssembly(typeof(GitHubHelperLauncher).Assembly);
            if (pkg != null && !string.IsNullOrEmpty(pkg.resolvedPath))
            {
                var repo = Path.GetFullPath(Path.Combine(pkg.resolvedPath, "..", ".."));
                if (IsApp(repo)) return repo;
            }
            if (IsApp(DefaultAppDir)) return DefaultAppDir;

            // The app's own default location when it was installed by hand next to the project.
            var win = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            foreach (var p in new[] { Path.Combine(win, "GitHubHelper"), Path.Combine(win, "Documents", "GitHub", "GitHubHelper") })
                if (IsApp(p)) return p;
            return null;
        }

        internal static string Download()
        {
            var tmp = Path.Combine(Path.GetTempPath(), "GitHubHelper-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(tmp);
            try
            {
                var zip = Path.Combine(tmp, "app.zip");
                EditorUtility.DisplayProgressBar("GitHub Helper", "Downloading...", 0.3f);
                ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072; // TLS 1.2
                using (var wc = new WebClient()) wc.DownloadFile(ZipUrl, zip);

                EditorUtility.DisplayProgressBar("GitHub Helper", "Unpacking...", 0.8f);
                var ex = Path.Combine(tmp, "x");
                ZipFile.ExtractToDirectory(zip, ex);
                var top = Directory.GetDirectories(ex);
                if (top.Length != 1 || !IsApp(top[0])) throw new Exception("The downloaded archive has an unexpected layout.");

                var dest = DefaultAppDir;
                if (Directory.Exists(dest)) Directory.Delete(dest, true);
                Directory.CreateDirectory(Path.GetDirectoryName(dest));
                Directory.Move(top[0], dest);
                return dest;
            }
            finally
            {
                EditorUtility.ClearProgressBar();
                try { Directory.Delete(tmp, true); } catch { }
            }
        }

        internal static void Launch(string app)
        {
            var project = ProjectPath;
            ProcessStartInfo psi;
            if (Application.platform == RuntimePlatform.WindowsEditor)
            {
                psi = new ProcessStartInfo("powershell.exe",
                    "-NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + Path.Combine(app, "windows", "server.ps1") + "\" -Repo \"" + project + "\"");
            }
            else
            {
                psi = new ProcessStartInfo("/bin/bash", "\"" + Path.Combine(app, "linux", "start.sh") + "\" --repo \"" + project + "\"");
            }
            psi.WorkingDirectory = app;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            Process.Start(psi);
        }
    }
}
