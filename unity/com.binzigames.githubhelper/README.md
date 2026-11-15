# GitHub Helper for Unity

Opens [GitHub Helper](https://github.com/Binzigames/GitHubHelper) straight on your Unity project from inside the Editor.

**Window → GitHub Helper** (Ctrl/Cmd + Shift + G)

- A native, dockable Editor window: changes with diffs, commit, history, branches, and a one-button Sync (pull/push/publish) with live progress. `.meta` files are staged together with their assets.
- It opens on the current project folder. If the folder is not a Git repository yet, you are offered to create one.
- If GitHub Helper is not installed, the package downloads it once (about 1 MB) into `%LOCALAPPDATA%\GitHubHelper\app` (Windows) or `~/.local/share/GitHubHelper/app`.
- **Window → GitHub Helper (standalone app)** opens the full desktop app instead.
- **Window → GitHub Helper (choose app folder...)** points the package at an existing copy of the app.

## Install

Package Manager → **+** → **Add package from git URL**:

```
https://github.com/Binzigames/GitHubHelper.git?path=/unity/com.binzigames.githubhelper
```

Requires Unity 2019.4+. On macOS/Linux `python3` must be installed.
