using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using UnityEngine;

namespace BinziGames.GitHubHelper
{
    /// <summary>
    /// Runs the GitHub Helper local server (hidden) and talks to its JSON API. All git / gh work is done by the
    /// server, so the Unity UI is a native front-end for the same engine as the standalone app.
    /// </summary>
    class HelperServer
    {
        public volatile bool Ready;           // token obtained, API callable
        public volatile string StartError;    // set when the server could not be started

        string baseUrl, token;
        Process proc;
        readonly ConcurrentQueue<Action> done = new ConcurrentQueue<Action>();

        public bool Running { get { try { return proc != null && !proc.HasExited; } catch { return false; } } }

        public void Start(string app)
        {
            Ready = false; StartError = null;
            ServicePointManager.DefaultConnectionLimit = 16;
            int port = FreePort();
            baseUrl = "http://127.0.0.1:" + port;
            ProcessStartInfo psi;
            if (Application.platform == RuntimePlatform.WindowsEditor)
                psi = new ProcessStartInfo("powershell.exe",
                    "-NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + Path.Combine(app, "windows", "server.ps1") +
                    "\" -NoBrowser -NoShortcut -Port " + port);
            else
                psi = new ProcessStartInfo("/bin/bash",
                    "\"" + Path.Combine(app, "linux", "start.sh") + "\" --no-browser --no-shortcut --foreground --port " + port);
            psi.WorkingDirectory = app;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { proc = Process.Start(psi); }
            catch (Exception e) { StartError = e.Message; return; }

            var url = baseUrl;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                var until = DateTime.UtcNow.AddSeconds(45);
                while (DateTime.UtcNow < until)
                {
                    try
                    {
                        var html = Get(url + "/");
                        var m = Regex.Match(html, "TOKEN = \"([0-9a-f]+)\"");
                        if (m.Success) { token = m.Groups[1].Value; Ready = true; return; }
                    }
                    catch { Thread.Sleep(300); }
                }
                StartError = "The GitHub Helper server did not start. On macOS/Linux python3 must be installed.";
            });
        }

        public void Stop()
        {
            try { if (Ready) Post("bye", "{}", 1500); } catch { }
            try { if (proc != null && !proc.HasExited) proc.Kill(); } catch { }
            proc = null; Ready = false;
        }

        /// <summary>Runs an API call on a worker thread; the callback runs from <see cref="Pump"/> on the main thread.</summary>
        public void Call(string name, string body, Action<string, string> cb)
        {
            ThreadPool.QueueUserWorkItem(_ =>
            {
                string json = null, err = null;
                try { json = Post(name, body ?? "{}", 1200000); }
                catch (WebException we)
                {
                    err = we.Message;
                    try
                    {
                        if (we.Response != null)
                            using (var r = new StreamReader(we.Response.GetResponseStream(), Encoding.UTF8))
                            {
                                var txt = r.ReadToEnd();
                                var dto = JsonUtility.FromJson<ErrorDto>(txt);
                                err = dto != null && !string.IsNullOrEmpty(dto.error) ? dto.error : txt;
                            }
                    }
                    catch { }
                }
                catch (Exception e) { err = e.Message; }
                done.Enqueue(() => cb(json, err));
            });
        }

        public void Pump()
        {
            Action a;
            while (done.TryDequeue(out a))
            {
                try { a(); } catch (Exception e) { UnityEngine.Debug.LogException(e); }
            }
        }

        string Post(string name, string body, int timeoutMs)
        {
            var req = (HttpWebRequest)WebRequest.Create(baseUrl + "/api/" + name);
            req.Method = "POST";
            req.ContentType = "application/json";
            req.Headers["X-Token"] = token;
            req.KeepAlive = false;
            req.Timeout = timeoutMs; req.ReadWriteTimeout = timeoutMs;
            var bytes = Encoding.UTF8.GetBytes(body);
            req.ContentLength = bytes.Length;
            using (var s = req.GetRequestStream()) s.Write(bytes, 0, bytes.Length);
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var r = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                return r.ReadToEnd();
        }

        static string Get(string url)
        {
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Timeout = 2000; req.KeepAlive = false;
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var r = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                return r.ReadToEnd();
        }

        static int FreePort()
        {
            var l = new TcpListener(IPAddress.Loopback, 0);
            l.Start();
            var p = ((IPEndPoint)l.LocalEndpoint).Port;
            l.Stop();
            return p;
        }

        // ---- tiny JSON object builder (values: null, string, bool, int, string[]) ----
        public static string Json(params object[] kv)
        {
            var sb = new StringBuilder("{");
            for (int i = 0; i + 1 < kv.Length; i += 2)
            {
                if (i > 0) sb.Append(',');
                sb.Append(Str((string)kv[i])).Append(':');
                var v = kv[i + 1];
                if (v == null) sb.Append("null");
                else if (v is string) sb.Append(Str((string)v));
                else if (v is bool) sb.Append((bool)v ? "true" : "false");
                else if (v is string[])
                {
                    sb.Append('[');
                    var a = (string[])v;
                    for (int j = 0; j < a.Length; j++) { if (j > 0) sb.Append(','); sb.Append(Str(a[j])); }
                    sb.Append(']');
                }
                else sb.Append(Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture));
            }
            return sb.Append('}').ToString();
        }

        /// <summary>JSON array of objects {path, untracked} for the 'discard' call.</summary>
        public static string DiscardFiles(string repo, string[] paths, bool[] untracked)
        {
            var sb = new StringBuilder("{\"repo\":" + Str(repo) + ",\"files\":[");
            for (int i = 0; i < paths.Length; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append("{\"path\":" + Str(paths[i]) + ",\"untracked\":" + (untracked[i] ? "true" : "false") + "}");
            }
            return sb.Append("]}").ToString();
        }

        static string Str(string s)
        {
            if (s == null) return "null";
            var sb = new StringBuilder("\"");
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4")); else sb.Append(c);
                        break;
                }
            }
            return sb.Append('"').ToString();
        }
    }
}
