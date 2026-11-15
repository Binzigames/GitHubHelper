using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace BinziGames.GitHubHelper
{
    /// <summary>
    /// GitHub Helper as a native, dockable Editor window: changes, commit, history, branches and sync.
    /// It drives the same local engine as the standalone app (see <see cref="HelperServer"/>).
    /// </summary>
    public class GitHubHelperWindow : EditorWindow
    {
        enum Stage { NoApp, Starting, Tools, NeedRepo, Ready, Error }

        class Row
        {
            public string path;            // main file
            public string[] ids;           // main file + its .meta (staged / unstaged together)
            public char status;
            public bool staged, untracked;
            public string key;             // changes when the file changes (used to reload the diff)
        }

        static HelperServer server;

        Stage stage = Stage.Starting;
        string error = "";
        string repo;

        StateDto app = new StateDto();
        StatusDto st = new StatusDto();
        BranchesDto br = new BranchesDto();
        LogDto log = new LogDto();
        ShowDto shown;
        readonly List<Row> rows = new List<Row>();
        readonly Dictionary<string, FileDto> fileByPath = new Dictionary<string, FileDto>();
        HashSet<string> unpushed = new HashSet<string>();

        int tab;
        bool busy, refreshing, progInFlight;
        string busyLabel = "";
        ProgressDto prog;
        string message; bool messageError;
        double nextPoll, nextProg, nextPing, nextLogin;
        string lastHead, lastBranchesHead;

        string selPath, selKey; string[] diffLines = new string[0]; string diffFor;
        string selSha, selCommitFile; string[] commitDiff = new string[0];
        string summary = "", description = "", idName = "", idEmail = "";
        Vector2 listScroll, diffScroll, histScroll, filesScroll;
        string loginCode; bool loginActive;
        bool visible = true;

        GUIStyle mono, dim, bold, rowLabel;

        // ------------------------------------------------------------------ open / lifecycle
        public static void ShowWindow()
        {
            var w = GetWindow<GitHubHelperWindow>();
            w.titleContent = new GUIContent("GitHub Helper", LoadIcon());
            w.minSize = new Vector2(560, 360);
            w.Show();
            w.Focus();
        }

        static Texture2D LoadIcon()
        {
            var pkg = UnityEditor.PackageManager.PackageInfo.FindForAssembly(typeof(GitHubHelperWindow).Assembly);
            return pkg == null ? null : AssetDatabase.LoadAssetAtPath<Texture2D>(pkg.assetPath + "/Editor/Icons/GitHubHelper.png");
        }

        void OnEnable()
        {
            titleContent = new GUIContent("GitHub Helper", LoadIcon());
            EditorApplication.update += Tick;
            EditorApplication.focusChanged += OnAppFocus;
            StartUp();
        }

        void OnDisable()
        {
            EditorApplication.update -= Tick;
            EditorApplication.focusChanged -= OnAppFocus;
        }

        void OnDestroy()
        {
            if (server != null) { server.Stop(); server = null; }
        }

        void OnBecameVisible() { visible = true; Refresh(true); }
        void OnBecameInvisible() { visible = false; }
        void OnFocus() { Refresh(false); }
        void OnAppFocus(bool focused) { if (focused) Refresh(false); }

        void StartUp()
        {
            error = ""; message = null; busy = false; refreshing = false;
            repo = null;
            var appDir = GitHubHelperLauncher.FindApp();
            if (appDir == null) { stage = Stage.NoApp; return; }
            if (server == null || !server.Running) { server = new HelperServer(); server.Start(appDir); }
            stage = Stage.Starting;
        }

        // ------------------------------------------------------------------ background work
        void Tick()
        {
            if (server == null) return;
            server.Pump();

            if (stage == Stage.Starting)
            {
                if (server.StartError != null) { error = server.StartError; stage = Stage.Error; Repaint(); }
                else if (server.Ready) BeginTools();
                return;
            }
            if (stage != Stage.Ready && stage != Stage.Tools && stage != Stage.NeedRepo) return;

            var now = EditorApplication.timeSinceStartup;
            if (now > nextPing) { nextPing = now + 8; server.Call("ping", "{}", (j, e) => { }); }

            if (busy && now > nextProg && !progInFlight)
            {
                nextProg = now + 0.35; progInFlight = true;
                server.Call("progress", "{}", (j, e) =>
                {
                    progInFlight = false;
                    if (e == null && busy) { try { prog = JsonUtility.FromJson<ProgressDto>(j); } catch { } Repaint(); }
                });
            }

            if (loginActive && now > nextLogin)
            {
                nextLogin = now + 2;
                server.Call("ghloginStatus", "{}", (j, e) =>
                {
                    if (!loginActive || e != null) return;
                    var s = JsonUtility.FromJson<LoginDto>(j);
                    if (s.state == "done") { loginActive = false; loginCode = null; SetMsg("Signed in to GitHub.", false); LoadState(); var a = afterLogin; afterLogin = null; if (a != null) { app.ghAuthed = true; a(); } }
                    else if (s.state == "failed") { loginActive = false; loginCode = null; SetMsg("Sign-in failed. " + s.error, true); }
                    Repaint();
                });
            }

            if (stage == Stage.Ready && visible && !busy && now > nextPoll) { nextPoll = now + 5; Refresh(false); }
        }

        void BeginTools()
        {
            stage = Stage.Tools; busy = true; busyLabel = "Preparing Git…";
            server.Call("ensureTools", "{}", (j, e) =>
            {
                busy = false;
                if (e != null) { error = e; stage = Stage.Error; Repaint(); return; }
                var t = JsonUtility.FromJson<ToolsDto>(j);
                if (t != null && !t.ok && !string.IsNullOrEmpty(t.error)) SetMsg(t.error, true);
                LoadState();
                AddRepo();
            });
        }

        void LoadState()
        {
            server.Call("state", "{}", (j, e) => { if (e == null) { app = JsonUtility.FromJson<StateDto>(j); Repaint(); } });
        }

        void AddRepo()
        {
            server.Call("addRepo", HelperServer.Json("repo", GitHubHelperLauncher.ProjectPath), (j, e) =>
            {
                if (e != null) { error = e; stage = Stage.NeedRepo; Repaint(); return; }
                repo = JsonUtility.FromJson<PathDto>(j).path;
                stage = Stage.Ready;
                Refresh(true);
            });
        }

        void InitRepo()
        {
            try
            {
                var gi = Path.Combine(GitHubHelperLauncher.ProjectPath, ".gitignore");
                if (!File.Exists(gi)) File.WriteAllText(gi, UnityGitIgnore);
            }
            catch (Exception ex) { SetMsg("Could not write .gitignore: " + ex.Message, true); }
            busy = true; busyLabel = "Creating repository…";
            server.Call("init", HelperServer.Json("repo", GitHubHelperLauncher.ProjectPath), (j, e) =>
            {
                busy = false;
                if (e != null) { error = e; Repaint(); return; }
                repo = JsonUtility.FromJson<PathDto>(j).path;
                stage = Stage.Ready;
                Refresh(true);
            });
        }

        void SetMsg(string m, bool isError) { message = m; messageError = isError; Repaint(); }

        string R(params object[] extra)
        {
            var kv = new List<object> { "repo", repo };
            kv.AddRange(extra);
            return HelperServer.Json(kv.ToArray());
        }

        /// <summary>Runs a (possibly long) operation: disables the UI, shows progress, refreshes afterwards.</summary>
        void Op(string label, string name, string body, Action ok = null, string success = null)
        {
            if (busy || server == null) return;
            busy = true; busyLabel = label; prog = null; message = null; nextProg = 0;
            server.Call(name, body, (j, e) =>
            {
                busy = false; prog = null;
                if (e != null) SetMsg(e, true);
                else
                {
                    if (success != null) SetMsg(success, false);
                    if (ok != null) ok();
                }
                Refresh(true);
                Repaint();
            });
        }

        void Refresh(bool full)
        {
            if (stage != Stage.Ready || refreshing || busy || server == null || repo == null) return;
            refreshing = true;
            server.Call("status", R(), (j, e) =>
            {
                refreshing = false;
                if (e != null) { SetMsg(e, true); return; }
                st = JsonUtility.FromJson<StatusDto>(j);
                BuildRows();
                if (full || st.head != lastBranchesHead) { lastBranchesHead = st.head; LoadBranches(); }
                if (tab == 1 && (full || st.head != lastHead)) LoadLog();
                lastHead = st.head;
                var row = rows.Find(r => r.path == selPath);
                if (row == null) { selPath = null; diffLines = new string[0]; diffFor = null; }
                else if (row.key != diffFor) LoadDiff(row);
                Repaint();
            });
        }

        void LoadBranches()
        {
            server.Call("branches", R(), (j, e) => { if (e == null) { br = JsonUtility.FromJson<BranchesDto>(j); Repaint(); } });
        }

        void LoadLog()
        {
            server.Call("log", R(), (j, e) =>
            {
                if (e != null) return;
                log = JsonUtility.FromJson<LogDto>(j);
                unpushed = new HashSet<string>(log.unpushed ?? new string[0]);
                Repaint();
            });
        }

        void LoadDiff(Row r)
        {
            diffFor = r.key;
            server.Call("diff", R("path", r.path, "untracked", r.untracked), (j, e) =>
            {
                if (r.key != diffFor) return;
                diffLines = e != null ? new[] { e } : SplitDiff(JsonUtility.FromJson<DiffDto>(j).diff);
                Repaint();
            });
        }

        void SelectCommit(string sha)
        {
            selSha = sha; shown = null; selCommitFile = null; commitDiff = new string[0];
            server.Call("show", R("sha", sha), (j, e) =>
            {
                if (selSha != sha || e != null) return;
                shown = JsonUtility.FromJson<ShowDto>(j);
                if (shown.files != null && shown.files.Length > 0) SelectCommitFile(shown.files[0].path);
                Repaint();
            });
        }

        void SelectCommitFile(string path)
        {
            selCommitFile = path;
            var sha = selSha;
            server.Call("showDiff", R("sha", sha, "path", path), (j, e) =>
            {
                if (selSha != sha || selCommitFile != path) return;
                commitDiff = e != null ? new[] { e } : SplitDiff(JsonUtility.FromJson<DiffDto>(j).diff);
                Repaint();
            });
        }

        static string[] SplitDiff(string d)
        {
            if (string.IsNullOrEmpty(d)) return new[] { "(no textual changes)" };
            var lines = d.Replace("\r\n", "\n").TrimEnd('\n').Split('\n');
            if (lines.Length > 5000) { Array.Resize(ref lines, 5000); lines[4999] = "… diff truncated …"; }
            return lines;
        }

        void BuildRows()
        {
            rows.Clear(); fileByPath.Clear();
            var all = new HashSet<string>();
            foreach (var f in st.files) { fileByPath[f.path] = f; all.Add(f.path); }
            foreach (var f in st.files)
            {
                if (f.path.EndsWith(".meta") && all.Contains(f.path.Substring(0, f.path.Length - 5))) continue;   // shown with its asset
                var ids = new List<string> { f.path };
                var metaPath = f.path + ".meta";
                FileDto meta;
                if (fileByPath.TryGetValue(metaPath, out meta)) ids.Add(metaPath);
                bool staged = IsStaged(f) && (meta == null || IsStaged(meta));
                char s = f.x != null && f.x.Length > 0 && f.x != " " ? f.x[0] : (f.y != null && f.y.Length > 0 ? f.y[0] : 'M');
                rows.Add(new Row
                {
                    path = f.path, ids = ids.ToArray(), staged = staged, untracked = f.x == "?",
                    status = f.x == "?" ? 'U' : s, key = f.path + "|" + f.x + f.y + "|" + f.m + (meta != null ? "|" + meta.m : "")
                });
            }
            rows.Sort((a, b) => string.Compare(a.path, b.path, StringComparison.OrdinalIgnoreCase));
        }

        static bool IsStaged(FileDto f) { return !string.IsNullOrEmpty(f.x) && f.x != " " && f.x != "?"; }

        // ------------------------------------------------------------------ actions
        void Sync()
        {
            if (!st.hasCommits) return;
            if (string.IsNullOrEmpty(st.remote)) { PublishRepo(); return; }
            if (string.IsNullOrEmpty(st.upstream)) { Op("Publishing branch…", "push", R(), null, "Branch published."); return; }
            if (st.ahead > 0 && st.behind > 0) { Op("Pulling…", "pull", R(), () => Op("Pushing…", "push", R(), null, "Synced.")); return; }
            if (st.behind > 0) { Op("Pulling…", "pull", R(), null, "Pulled."); return; }
            if (st.ahead > 0) { Op("Pushing…", "push", R(), null, "Pushed."); return; }
            Op("Fetching…", "fetch", R(), null, "Up to date.");
        }

        string SyncLabel()
        {
            if (!st.hasCommits) return "Sync";
            if (string.IsNullOrEmpty(st.remote)) return "Publish repository";
            if (string.IsNullOrEmpty(st.upstream)) return "Publish branch";
            if (st.ahead > 0 && st.behind > 0) return "Sync ↓" + st.behind + " ↑" + st.ahead;
            if (st.behind > 0) return "Pull ↓" + st.behind;
            if (st.ahead > 0) return "Push ↑" + st.ahead;
            return "Fetch";
        }

        void PublishRepo()
        {
            if (!app.ghAuthed) { SignIn(PublishRepo); return; }
            TextPrompt.Show("Publish to GitHub", "Repository name", Path.GetFileName(repo.TrimEnd('\\', '/')), "Publish", (name, priv) =>
                Op("Publishing…", "publish", R("name", name, "private", priv), null, "Published to GitHub."), "Private repository", true);
        }

        Action afterLogin;
        void SignIn(Action then)
        {
            afterLogin = then;
            server.Call("ghlogin", "{}", (j, e) =>
            {
                if (e != null) { SetMsg(e, true); return; }
                var l = JsonUtility.FromJson<LoginDto>(j);
                loginCode = l.code; loginActive = true; nextLogin = EditorApplication.timeSinceStartup + 3;
                Application.OpenURL(l.url);
                Repaint();
            });
        }

        void CancelLogin()
        {
            loginActive = false; loginCode = null;
            server.Call("ghloginCancel", "{}", (j, e) => { });
        }

        void DoCommit()
        {
            var s = summary; var d = description;
            Op("Committing…", "commit", R("summary", s, "description", d, "amend", false), () => { summary = ""; description = ""; }, "Committed.");
        }

        void ToggleRow(Row r)
        {
            Op(r.staged ? "Unstaging…" : "Staging…", r.staged ? "unstage" : "stage", R("paths", r.ids));
        }

        void Discard(Row r)
        {
            if (!EditorUtility.DisplayDialog("Discard changes", "Discard all changes in\n" + r.path + "?\nThis cannot be undone.", "Discard", "Cancel")) return;
            var paths = new List<string>(); var un = new List<bool>();
            foreach (var id in r.ids)
            {
                FileDto f;
                if (!fileByPath.TryGetValue(id, out f)) continue;
                paths.Add(id); un.Add(f.x == "?");
            }
            Op("Discarding…", "discard", HelperServer.DiscardFiles(repo, paths.ToArray(), un.ToArray()));
        }

        void BranchMenu(Rect rect)
        {
            var m = new GenericMenu();
            foreach (var b in br.local)
            {
                var name = b.name;
                m.AddItem(new GUIContent(name), b.current, () => { if (name != st.branch) Op("Switching…", "switch", R("name", name)); });
            }
            if (br.remote.Length > 0) m.AddSeparator("");
            foreach (var b in br.remote)
            {
                var full = b.name; var local = full.Substring(full.IndexOf('/') + 1);
                m.AddItem(new GUIContent("Remote/" + full), false, () => Op("Switching…", "switch", R("name", local)));
            }
            m.AddSeparator("");
            m.AddItem(new GUIContent("New branch…"), false, () =>
                TextPrompt.Show("New branch", "Branch name", "", "Create", (n, _) => Op("Creating…", "createBranch", R("name", n.Replace(' ', '-')))));
            foreach (var b in br.local)
            {
                if (b.current) continue;
                var name = b.name;
                m.AddItem(new GUIContent("Merge into current/" + name), false, () =>
                {
                    if (EditorUtility.DisplayDialog("Merge", "Merge '" + name + "' into '" + st.branch + "'?", "Merge", "Cancel"))
                        Op("Merging…", "merge", R("name", name), null, "Merged.");
                });
                m.AddItem(new GUIContent("Delete branch/" + name), false, () =>
                {
                    if (EditorUtility.DisplayDialog("Delete branch", "Delete branch '" + name + "'?", "Delete", "Cancel"))
                        Op("Deleting…", "deleteBranch", R("name", name, "force", false));
                });
            }
            m.DropDown(rect);
        }

        void MoreMenu(Rect rect)
        {
            var m = new GenericMenu();
            m.AddItem(new GUIContent("Refresh"), false, () => Refresh(true));
            m.AddItem(new GUIContent("Fetch"), false, () => Op("Fetching…", "fetch", R(), null, "Fetched."));
            m.AddSeparator("");
            m.AddItem(new GUIContent("Stash changes"), false, () => Op("Stashing…", "stash", R(), null, "Changes stashed."));
            m.AddItem(new GUIContent("Restore stashed changes"), false, () => Op("Restoring…", "stashPop", R(), null, "Stash restored."));
            if (st.hasCommits)
                m.AddItem(new GUIContent("Undo last commit (keep changes)"), false, () =>
                {
                    if (EditorUtility.DisplayDialog("Undo last commit", "Undo the last commit and keep its changes?", "Undo", "Cancel"))
                        Op("Undoing…", "undoCommit", R(), null, "Last commit undone.");
                });
            if (st.merging) m.AddItem(new GUIContent("Abort merge"), false, () => Op("Aborting…", "abortMerge", R()));
            m.AddSeparator("");
            if (!string.IsNullOrEmpty(st.webUrl) && st.webUrl.StartsWith("https://"))
                m.AddItem(new GUIContent("Open on GitHub"), false, () => Application.OpenURL(st.webUrl));
            if (app.ghAuthed) m.AddItem(new GUIContent("Sign out of GitHub (" + app.ghUser + ")"), false, () =>
            {
                if (EditorUtility.DisplayDialog("GitHub", "Sign out of GitHub?", "Sign out", "Cancel"))
                    Op("Signing out…", "ghlogout", "{}", LoadState, "Signed out.");
            });
            else m.AddItem(new GUIContent("Sign in with GitHub"), false, () => SignIn(null));
            m.AddSeparator("");
            m.AddItem(new GUIContent("Open standalone app"), false, GitHubHelperLauncher.OpenExternal);
            m.DropDown(rect);
        }

        // ------------------------------------------------------------------ GUI
        void Styles()
        {
            if (mono != null) return;
            mono = new GUIStyle(EditorStyles.label)
            {
                font = Font.CreateDynamicFontFromOSFont(new[] { "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New" }, 12),
                fontSize = 12, richText = false, wordWrap = false, padding = new RectOffset(6, 4, 0, 0), alignment = TextAnchor.MiddleLeft
            };
            dim = new GUIStyle(EditorStyles.miniLabel) { normal = { textColor = new Color(0.55f, 0.55f, 0.55f) }, clipping = TextClipping.Clip };
            bold = new GUIStyle(EditorStyles.boldLabel) { clipping = TextClipping.Clip };
            rowLabel = new GUIStyle(EditorStyles.label) { clipping = TextClipping.Clip };
        }

        void OnGUI()
        {
            Styles();
            switch (stage)
            {
                case Stage.NoApp: CenterBox(() =>
                    {
                        GUILayout.Label("GitHub Helper is not installed on this computer yet.", EditorStyles.wordWrappedLabel);
                        if (GUILayout.Button("Download (about 1 MB)", GUILayout.Height(26)))
                        {
                            if (GitHubHelperLauncher.EnsureApp() != null) StartUp();
                        }
                    }); return;
                case Stage.Starting: CenterBox(() => GUILayout.Label("Starting GitHub Helper…", EditorStyles.centeredGreyMiniLabel)); return;
                case Stage.Error: CenterBox(() =>
                    {
                        EditorGUILayout.HelpBox(error, MessageType.Error);
                        if (GUILayout.Button("Try again", GUILayout.Height(24))) { if (server != null) server.Stop(); server = null; StartUp(); }
                    }); return;
                case Stage.Tools: CenterBox(() => { GUILayout.Label("Preparing Git…", EditorStyles.centeredGreyMiniLabel); DrawProgress(); }); return;
                case Stage.NeedRepo: CenterBox(() =>
                    {
                        GUILayout.Label("This project is not a Git repository yet.", EditorStyles.boldLabel);
                        GUILayout.Label("Create one here (a Unity .gitignore is added if the project has none) and start tracking your changes.", EditorStyles.wordWrappedLabel);
                        if (!string.IsNullOrEmpty(error)) EditorGUILayout.HelpBox(error, MessageType.None);
                        GUI.enabled = !busy;
                        if (GUILayout.Button("Create repository", GUILayout.Height(26))) InitRepo();
                        GUI.enabled = true;
                        DrawProgress();
                    }); return;
            }

            DrawToolbar();
            DrawBanners();
            using (new GUILayout.HorizontalScope(GUILayout.ExpandHeight(true)))
            {
                if (tab == 0) DrawChanges(); else DrawHistory();
            }
            DrawStatusBar();
        }

        static void CenterBox(Action content)
        {
            GUILayout.FlexibleSpace();
            using (new GUILayout.HorizontalScope())
            {
                GUILayout.FlexibleSpace();
                using (new GUILayout.VerticalScope(GUILayout.Width(380))) content();
                GUILayout.FlexibleSpace();
            }
            GUILayout.FlexibleSpace();
        }

        void DrawToolbar()
        {
            using (new GUILayout.HorizontalScope(EditorStyles.toolbar))
            {
                var label = new GUIContent(string.IsNullOrEmpty(st.branch) ? "(no branch)" : st.branch, "Switch, create or merge branches");
                GUI.enabled = !busy;
                if (GUILayout.Button(label, EditorStyles.toolbarDropDown, GUILayout.MinWidth(110), GUILayout.MaxWidth(240)))
                    BranchMenu(GUILayoutUtility.GetLastRect());
                GUILayout.Space(6);
                int nt = GUILayout.Toolbar(tab, new[] { "Changes" + (rows.Count > 0 ? " (" + rows.Count + ")" : ""), "History" }, EditorStyles.toolbarButton, GUILayout.Width(200));
                if (nt != tab) { tab = nt; if (tab == 1) LoadLog(); }
                GUILayout.FlexibleSpace();
                GUI.enabled = !busy && st.hasCommits;
                if (GUILayout.Button(new GUIContent(SyncLabel()), EditorStyles.toolbarButton, GUILayout.MinWidth(110))) Sync();
                GUI.enabled = !busy;
                if (GUILayout.Button("⋯", EditorStyles.toolbarButton, GUILayout.Width(28))) MoreMenu(GUILayoutUtility.GetLastRect());
                GUI.enabled = true;
            }
        }

        void DrawBanners()
        {
            if (loginActive && !string.IsNullOrEmpty(loginCode))
            {
                using (new EditorGUILayout.VerticalScope(EditorStyles.helpBox))
                {
                    GUILayout.Label("Sign in to GitHub: enter this code in the browser page that just opened", EditorStyles.wordWrappedLabel);
                    using (new GUILayout.HorizontalScope())
                    {
                        EditorGUILayout.SelectableLabel(loginCode, new GUIStyle(EditorStyles.boldLabel) { fontSize = 18 }, GUILayout.Height(26), GUILayout.Width(130));
                        if (GUILayout.Button("Copy code", GUILayout.Width(90))) EditorGUIUtility.systemCopyBuffer = loginCode;
                        if (GUILayout.Button("Open page", GUILayout.Width(90))) Application.OpenURL("https://github.com/login/device");
                        GUILayout.FlexibleSpace();
                        if (GUILayout.Button("Cancel", GUILayout.Width(70))) CancelLogin();
                    }
                }
            }
            if (app.git && string.IsNullOrEmpty(app.userName) && stage == Stage.Ready)
            {
                using (new EditorGUILayout.VerticalScope(EditorStyles.helpBox))
                {
                    GUILayout.Label("Git needs your name and e-mail for commits.", EditorStyles.wordWrappedLabel);
                    using (new GUILayout.HorizontalScope())
                    {
                        idName = EditorGUILayout.TextField(idName);
                        idEmail = EditorGUILayout.TextField(idEmail);
                        GUI.enabled = !busy && idName.Trim().Length > 0 && idEmail.Trim().Length > 0;
                        if (GUILayout.Button("Save", GUILayout.Width(60)))
                            Op("Saving…", "setIdentity", HelperServer.Json("name", idName.Trim(), "email", idEmail.Trim()), LoadState);
                        GUI.enabled = true;
                    }
                }
            }
            if (st.merging)
                EditorGUILayout.HelpBox("A merge is in progress. Resolve conflicts, commit, or choose ⋯ → Abort merge.", MessageType.Warning);
        }

        void DrawProgress()
        {
            if (!busy) return;
            var r = GUILayoutUtility.GetRect(0, 18, GUILayout.ExpandWidth(true));
            var text = busyLabel;
            float v = 1f;
            if (prog != null && prog.active)
            {
                if (!string.IsNullOrEmpty(prog.phase)) text = busyLabel + "  " + prog.phase;
                if (prog.pct >= 0) { v = prog.pct / 100f; text += " " + prog.pct + "%"; }
                if (!string.IsNullOrEmpty(prog.detail)) text += "  " + prog.detail;
                else if (!string.IsNullOrEmpty(prog.what)) text += "  " + prog.what;
            }
            if (prog == null || prog.pct < 0) { v = Mathf.PingPong((float)EditorApplication.timeSinceStartup * 0.6f, 1f); Repaint(); }
            EditorGUI.ProgressBar(r, v, text);
        }

        void DrawStatusBar()
        {
            if (busy) { DrawProgress(); return; }
            if (string.IsNullOrEmpty(message)) return;
            using (new GUILayout.HorizontalScope(EditorStyles.helpBox))
            {
                GUILayout.Label(new GUIContent(message, message), messageError ? ErrStyle() : EditorStyles.wordWrappedLabel);
                if (GUILayout.Button("✕", EditorStyles.miniButton, GUILayout.Width(22))) message = null;
            }
        }

        GUIStyle err;
        GUIStyle ErrStyle()
        {
            if (err == null) err = new GUIStyle(EditorStyles.wordWrappedLabel) { normal = { textColor = new Color(0.9f, 0.35f, 0.35f) } };
            return err;
        }

        // ---------------------------------------------------------- changes tab
        void DrawChanges()
        {
            float leftW = Mathf.Clamp(position.width * 0.4f, 260, 460);
            using (new GUILayout.VerticalScope(GUILayout.Width(leftW)))
            {
                int staged = rows.FindAll(r => r.staged).Count;
                using (new GUILayout.HorizontalScope(EditorStyles.toolbar))
                {
                    GUILayout.Label(rows.Count == 0 ? "No changes" : staged + " of " + rows.Count + " selected", EditorStyles.miniLabel);
                    GUILayout.FlexibleSpace();
                    GUI.enabled = !busy && rows.Count > 0;
                    if (GUILayout.Button(staged == rows.Count ? "Select none" : "Select all", EditorStyles.toolbarButton))
                        Op("Updating…", staged == rows.Count ? "unstageAll" : "stageAll", R());
                    GUI.enabled = true;
                }

                listScroll = GUILayout.BeginScrollView(listScroll, GUILayout.ExpandHeight(true));
                if (rows.Count == 0)
                {
                    GUILayout.Space(30);
                    GUILayout.Label(st.hasCommits ? "Everything is committed ✓" : "Nothing to commit yet.", EditorStyles.centeredGreyMiniLabel);
                }
                foreach (var r in rows) DrawRow(r);
                GUILayout.EndScrollView();

                // commit box
                using (new EditorGUILayout.VerticalScope(EditorStyles.helpBox))
                {
                    summary = EditorGUILayout.TextField(summary);
                    var rr = GUILayoutUtility.GetLastRect();
                    if (string.IsNullOrEmpty(summary)) EditorGUI.LabelField(rr, "  Summary (required)", dim);
                    description = EditorGUILayout.TextArea(description, GUILayout.Height(44));
                    if (string.IsNullOrEmpty(description)) EditorGUI.LabelField(GUILayoutUtility.GetLastRect(), "  Description", dim);
                    GUI.enabled = !busy && staged > 0 && !string.IsNullOrWhiteSpace(summary);
                    var btn = "Commit " + staged + " file" + (staged == 1 ? "" : "s") + (string.IsNullOrEmpty(st.branch) ? "" : " to " + st.branch);
                    if (GUILayout.Button(btn, GUILayout.Height(26))) DoCommit();
                    GUI.enabled = true;
                    if (!string.IsNullOrEmpty(st.lastSubject)) GUILayout.Label("Last commit: " + st.lastSubject, dim);
                }
            }

            using (new GUILayout.VerticalScope(GUILayout.ExpandWidth(true)))
            {
                using (new GUILayout.HorizontalScope(EditorStyles.toolbar))
                    GUILayout.Label(selPath ?? "Select a file to see its changes", EditorStyles.miniLabel);
                DrawDiff(diffLines, ref diffScroll);
            }
        }

        void DrawRow(Row r)
        {
            var rect = GUILayoutUtility.GetRect(0, 22, GUILayout.ExpandWidth(true));
            bool sel = r.path == selPath;
            if (sel) EditorGUI.DrawRect(rect, new Color(0.24f, 0.49f, 0.9f, 0.35f));
            else if (Event.current.type == EventType.Repaint && rect.Contains(Event.current.mousePosition)) EditorGUI.DrawRect(rect, new Color(1, 1, 1, 0.05f));

            var tg = new Rect(rect.x + 4, rect.y + 3, 16, 16);
            bool now = EditorGUI.Toggle(tg, r.staged);
            if (now != r.staged && !busy) ToggleRow(r);

            var lr = new Rect(rect.x + 24, rect.y, rect.width - 50, rect.height);
            var dir = Path.GetDirectoryName(r.path) ?? "";
            var file = Path.GetFileName(r.path);
            var fw = rowLabel.CalcSize(new GUIContent(file)).x;
            GUI.Label(new Rect(lr.x, lr.y + 2, Mathf.Min(fw + 4, lr.width), 18), file, rowLabel);
            if (dir.Length > 0 && fw + 8 < lr.width)
                GUI.Label(new Rect(lr.x + fw + 8, lr.y + 3, lr.width - fw - 8, 18), dir.Replace('\\', '/'), dim);

            var sc = new GUIStyle(EditorStyles.boldLabel) { alignment = TextAnchor.MiddleCenter, normal = { textColor = StatusColor(r.status) } };
            GUI.Label(new Rect(rect.xMax - 24, rect.y, 20, rect.height), r.status.ToString(), sc);

            var e = Event.current;
            if (e.type == EventType.MouseDown && rect.Contains(e.mousePosition) && !tg.Contains(e.mousePosition))
            {
                if (e.button == 0)
                {
                    selPath = r.path; diffLines = new[] { "Loading…" }; LoadDiff(r); e.Use();
                }
                else if (e.button == 1)
                {
                    var row = r;
                    var m = new GenericMenu();
                    m.AddItem(new GUIContent(row.staged ? "Unselect" : "Select"), false, () => ToggleRow(row));
                    m.AddItem(new GUIContent("Discard changes…"), false, () => Discard(row));
                    m.AddItem(new GUIContent("Ping asset"), false, () =>
                    {
                        var o = AssetDatabase.LoadMainAssetAtPath(row.path);
                        if (o != null) EditorGUIUtility.PingObject(o);
                    });
                    m.ShowAsContext(); e.Use();
                }
            }
        }

        static Color StatusColor(char s)
        {
            switch (s)
            {
                case 'A': case 'U': return new Color(0.35f, 0.8f, 0.4f);
                case 'D': return new Color(0.9f, 0.4f, 0.4f);
                case 'R': case 'C': return new Color(0.4f, 0.65f, 1f);
                default: return new Color(0.95f, 0.75f, 0.3f);
            }
        }

        // ---------------------------------------------------------- history tab
        void DrawHistory()
        {
            float leftW = Mathf.Clamp(position.width * 0.4f, 260, 460);
            using (new GUILayout.VerticalScope(GUILayout.Width(leftW)))
            {
                histScroll = GUILayout.BeginScrollView(histScroll, GUILayout.ExpandHeight(true));
                if (log.commits.Length == 0) { GUILayout.Space(30); GUILayout.Label("No commits yet.", EditorStyles.centeredGreyMiniLabel); }
                foreach (var c in log.commits)
                {
                    var rect = GUILayoutUtility.GetRect(0, 38, GUILayout.ExpandWidth(true));
                    if (c.sha == selSha) EditorGUI.DrawRect(rect, new Color(0.24f, 0.49f, 0.9f, 0.35f));
                    bool up = unpushed.Contains(c.sha);
                    GUI.Label(new Rect(rect.x + 6, rect.y + 2, rect.width - (up ? 30 : 10), 18), c.subject, bold);
                    var date = string.IsNullOrEmpty(c.date) || c.date.Length < 10 ? "" : c.date.Substring(0, 10);
                    GUI.Label(new Rect(rect.x + 6, rect.y + 19, rect.width - 10, 16), c.@short + "  ·  " + c.author + "  ·  " + date + (string.IsNullOrEmpty(c.refs) ? "" : "  ·  " + c.refs), dim);
                    if (up) GUI.Label(new Rect(rect.xMax - 24, rect.y + 2, 20, 18), new GUIContent("↑", "Not pushed yet"), bold);
                    var e = Event.current;
                    if (e.type == EventType.MouseDown && e.button == 0 && rect.Contains(e.mousePosition)) { SelectCommit(c.sha); e.Use(); }
                }
                GUILayout.EndScrollView();
            }

            using (new GUILayout.VerticalScope(GUILayout.ExpandWidth(true)))
            {
                if (shown == null) { GUILayout.Space(30); GUILayout.Label("Select a commit", EditorStyles.centeredGreyMiniLabel); return; }
                using (new EditorGUILayout.VerticalScope(EditorStyles.helpBox))
                {
                    GUILayout.Label(shown.message, EditorStyles.wordWrappedLabel);
                    GUILayout.Label(shown.author + " · " + (shown.date ?? ""), dim);
                }
                filesScroll = GUILayout.BeginScrollView(filesScroll, GUILayout.Height(Mathf.Min(110, 20 * Mathf.Max(1, shown.files.Length) + 4)));
                foreach (var f in shown.files)
                {
                    var rect = GUILayoutUtility.GetRect(0, 20, GUILayout.ExpandWidth(true));
                    if (f.path == selCommitFile) EditorGUI.DrawRect(rect, new Color(0.24f, 0.49f, 0.9f, 0.35f));
                    var ch = string.IsNullOrEmpty(f.status) ? 'M' : f.status[0];
                    var sc = new GUIStyle(EditorStyles.boldLabel) { alignment = TextAnchor.MiddleCenter, normal = { textColor = StatusColor(ch) } };
                    GUI.Label(new Rect(rect.x + 2, rect.y, 18, rect.height), ch.ToString(), sc);
                    GUI.Label(new Rect(rect.x + 22, rect.y + 1, rect.width - 24, 18), f.path, rowLabel);
                    var e = Event.current;
                    if (e.type == EventType.MouseDown && e.button == 0 && rect.Contains(e.mousePosition)) { SelectCommitFile(f.path); e.Use(); }
                }
                GUILayout.EndScrollView();
                DrawDiff(commitDiff, ref diffScroll);
            }
        }

        // ---------------------------------------------------------- diff viewer (virtualised)
        void DrawDiff(string[] lines, ref Vector2 scroll)
        {
            var area = GUILayoutUtility.GetRect(0, 0, GUILayout.ExpandWidth(true), GUILayout.ExpandHeight(true));
            const float lh = 16f;
            float cw = mono.CalcSize(new GUIContent("M")).x;
            int maxLen = 0;
            for (int i = 0; i < lines.Length; i++) if (lines[i].Length > maxLen) maxLen = Math.Min(lines[i].Length, 400);
            var content = new Rect(0, 0, Mathf.Max(area.width - 16, maxLen * cw + 20), lines.Length * lh + 6);
            scroll = GUI.BeginScrollView(area, scroll, content);
            int first = Mathf.Max(0, (int)(scroll.y / lh));
            int last = Mathf.Min(lines.Length, first + (int)(area.height / lh) + 2);
            for (int i = first; i < last; i++)
            {
                var l = lines[i];
                var r = new Rect(0, i * lh, content.width, lh);
                if (l.StartsWith("+") && !l.StartsWith("+++")) EditorGUI.DrawRect(r, new Color(0.2f, 0.6f, 0.25f, 0.25f));
                else if (l.StartsWith("-") && !l.StartsWith("---")) EditorGUI.DrawRect(r, new Color(0.8f, 0.25f, 0.25f, 0.25f));
                else if (l.StartsWith("@@")) EditorGUI.DrawRect(r, new Color(0.3f, 0.5f, 0.9f, 0.2f));
                GUI.Label(r, l.Replace("\t", "    "), mono);
            }
            GUI.EndScrollView();
        }

        const string UnityGitIgnore =
@"# Unity
/[Ll]ibrary/
/[Tt]emp/
/[Oo]bj/
/[Bb]uild/
/[Bb]uilds/
/[Ll]ogs/
/[Uu]ser[Ss]ettings/
/[Mm]emoryCaptures/
/[Rr]ecordings/

# IDE / generated
.vs/
.idea/
.vscode/
*.csproj
*.unityproj
*.sln
*.suo
*.user
*.userprefs
*.pidb
*.booproj
*.svd
*.pdb
*.mdb
*.opendb
*.VC.db

# Build outputs
*.apk
*.aab
*.unitypackage
*.app
crashlytics-build.properties
/[Aa]ssets/AssetStoreTools*
.DS_Store
";
    }
}
