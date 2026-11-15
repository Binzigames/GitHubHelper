# GitHub Helper - local server (PowerShell 5.1+, no dependencies except git)
# Serves the UI on http://127.0.0.1:<port> and translates UI actions into git / gh commands.
param([int]$Port = 47321, [switch]$NoBrowser, [switch]$SkipInstall, [switch]$NoUpdate, [switch]$Restarted, [switch]$NoShortcut)

$ErrorActionPreference = 'Stop'
$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = $MyInvocation.MyCommand.Path
$AppRoot = Split-Path -Parent $Root
$UiFile  = Join-Path $AppRoot 'ui\index.html'   # UI shared with the Linux client
$UpdateRepo = 'Binzigames/GitHubHelper'          # where new versions are published (branch main, file VERSION)
$script:restart = $false
$DataDir = Join-Path $env:APPDATA 'GitHubHelper'
$ReposFile = Join-Path $DataDir 'repos.json'
New-Item -ItemType Directory -Force $DataDir | Out-Null
try { Start-Transcript -Path (Join-Path $DataDir 'server.log') -Force | Out-Null } catch { }   # the console is hidden by start.cmd; keep a log

# ---------- tool discovery ----------
function Find-Exe($name, $extra) {
    $c = Get-Command $name -ErrorAction SilentlyContinue | Where-Object { $_.CommandType -eq 'Application' } | Select-Object -First 1
    if ($c) { return $c.Source }
    foreach ($p in $extra) { if ($p -and (Test-Path -LiteralPath $p)) { return $p } }
    return $null
}
function Get-Git { Find-Exe 'git' @("$env:ProgramFiles\Git\cmd\git.exe", "${env:ProgramFiles(x86)}\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe") }
function Get-Gh  { Find-Exe 'gh'  @("$env:ProgramFiles\GitHub CLI\gh.exe", "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe") }

# ---------- dependency installer (winget) ----------
function Refresh-Path {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine'); $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($m, $u) | Where-Object { $_ }) -join ';'
}
function Test-Dep([string]$what) { if ($what -eq 'gh') { return [bool](Get-Gh) } else { return [bool](Get-Git) } }

# Fallback used when winget is missing or broken: download the official release from GitHub directly.
function Install-Direct([string]$what) {
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $repoName = if ($what -eq 'gh') { 'cli/cli' } else { 'git-for-windows/git' }
    $pattern  = if ($what -eq 'gh') { '_windows_amd64\.zip$' } else { '^Git-[\d.]+-64-bit\.exe$' }
    $rel = Invoke-RestMethod "https://api.github.com/repos/$repoName/releases/latest" -Headers @{ 'User-Agent' = 'GitHubHelper' }
    $asset = $rel.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
    if (-not $asset) { throw "No Windows download found for $repoName." }
    $tmp = Join-Path $env:TEMP ('ghh-dl-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force $tmp | Out-Null
    try {
        $file = Join-Path $tmp $asset.name
        Write-Host "Downloading $($asset.name) ($([math]::Round($asset.size / 1MB, 1)) MB)..." -ForegroundColor Cyan
        Invoke-WebRequest $asset.browser_download_url -OutFile $file -UseBasicParsing
        if ($what -eq 'gh') {
            $sumAsset = $rel.assets | Where-Object { $_.name -match 'checksums\.txt$' } | Select-Object -First 1
            if ($sumAsset) {
                $sums = (Invoke-WebRequest $sumAsset.browser_download_url -UseBasicParsing).Content
                # text/plain downloads can arrive as byte[] on PS 5.1
                if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
                $line = ($sums -split "`r?`n") | Where-Object { $_ -match ('\s\*?' + [regex]::Escape($asset.name) + '\s*$') } | Select-Object -First 1
                $expected = if ($line) { ($line -split '\s+')[0].ToLower() } else { $null }
                if (-not $expected -or (Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $expected) { throw 'Checksum mismatch - download discarded.' }
            }
            $ex = Join-Path $tmp 'x'; Expand-Archive $file $ex -Force
            $dest = Join-Path $env:LOCALAPPDATA 'Programs\GitHub CLI'
            New-Item -ItemType Directory -Force $dest | Out-Null
            Copy-Item (Join-Path $ex 'bin\gh.exe') $dest -Force
            $up = [Environment]::GetEnvironmentVariable('Path', 'User')
            if (($up -split ';') -notcontains $dest) { [Environment]::SetEnvironmentVariable('Path', (($up, $dest) | Where-Object { $_ }) -join ';', 'User') }
        } else {
            if ((Get-AuthenticodeSignature $file).Status -ne 'Valid') { throw 'Installer signature is not valid - download discarded.' }
            Start-Process $file -ArgumentList '/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES' -Wait
        }
    } finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

function Install-Dep([string]$what) {
    $id = if ($what -eq 'gh') { 'GitHub.cli' } else { 'Git.Git' }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Installing $id with winget (this can take a minute)..." -ForegroundColor Cyan
        try { & $winget.Source install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Host } catch { }
        Refresh-Path
        if (Test-Dep $what) { return $true }
        Write-Host 'winget did not work - downloading directly from GitHub instead.' -ForegroundColor Yellow
    }
    try { Install-Direct $what } catch { Write-Host "Direct download failed: $($_.Exception.Message)" -ForegroundColor Red }
    Refresh-Path
    return (Test-Dep $what)
}
function Ensure-Deps {
    if (-not (Get-Git)) { [void](Install-Dep 'git') }
    if (-not (Get-Gh))  { [void](Install-Dep 'gh') }
}

# ---------- security helpers ----------
# Hide credentials embedded in URLs (https://user:token@host/...) in anything shown to the UI.
function Mask([string]$s) { if (-not $s) { return $s }; return ($s -replace '(?i)(https?://)[^/\s@]+@', '$1***@') }
# Reject values git/gh could interpret as options or dangerous transports.
function Check-Ref($v) {
    $v = [string]$v
    if (-not $v.Trim() -or $v.StartsWith('-') -or $v -match '[\x00-\x1f]') { throw 'Invalid name.' }
}
function Check-Url($v) {
    $v = ([string]$v).Trim()
    if (-not $v -or $v.StartsWith('-') -or $v -match '^(ext|fd)::' -or $v -match '[\x00-\x1f]') { throw 'Invalid URL.' }
}

# ---------- process helpers ----------
function Quote-Arg([string]$a) {
    if ($a -eq '') { return '""' }
    if ($a -notmatch '[\s"]') { return $a }
    $s = $a -replace '(\\*)"', '$1$1\"'
    $s = $s -replace '(\\+)$', '$1$1'
    return '"' + $s + '"'
}

function Invoke-Exe([string]$Exe, [string]$Cwd, [string[]]$ArgList, [string]$Stdin = $null) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = (($ArgList | ForEach-Object { Quote-Arg $_ }) -join ' ')
    if ($Cwd) { $psi.WorkingDirectory = $Cwd }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $psi.StandardOutputEncoding = $utf8
    $psi.StandardErrorEncoding = $utf8
    # NOTE: ProcessStartInfo.StandardInputEncoding does not exist on .NET Framework (Windows PowerShell 5.1),
    # so stdin is written as raw UTF-8 bytes below instead.
    $psi.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $psi.EnvironmentVariables['GIT_EDITOR'] = 'true'
    $psi.EnvironmentVariables['GCM_INTERACTIVE'] = 'auto'
    $p = [System.Diagnostics.Process]::Start($psi)
    $outT = $p.StandardOutput.ReadToEndAsync()
    $errT = $p.StandardError.ReadToEndAsync()
    if ($Stdin) {
        $bytes = $utf8.GetBytes($Stdin)
        $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $p.StandardInput.BaseStream.Flush()
    }
    $p.StandardInput.Close()
    $p.WaitForExit()
    return @{ code = $p.ExitCode; out = $outT.Result; err = $errT.Result }
}

function G([string]$Repo, [string[]]$GitArgs, [string]$Stdin = $null, [switch]$Fail) {
    $git = Get-Git
    if (-not $git) { throw 'Git is not installed. Use the "Install Git" button in the app.' }
    $all = @('-c', 'core.quotepath=false', '-c', 'color.ui=false', '-c', 'core.safecrlf=false', '-c', 'protocol.ext.allow=never', '-c', 'protocol.file.allow=user') + $GitArgs
    $r = Invoke-Exe $git $Repo $all $Stdin
    if ($Fail -and $r.code -ne 0) {
        $m = if ($r.err.Trim()) { $r.err.Trim() } else { $r.out.Trim() }
        throw $m
    }
    return $r
}

function Require-Repo($repo) {
    if (-not $repo -or -not (Test-Path -LiteralPath $repo -PathType Container)) { throw "Folder not found: $repo" }
}

# ---------- repo list ----------
function Load-Repos {
    # JavaScriptSerializer returns a plain string[]; ConvertFrom-Json in PS 5.1 mangles 1-element arrays.
    if (-not (Test-Path $ReposFile)) { return @() }
    $raw = Get-Content $ReposFile -Raw -Encoding UTF8
    if (-not $raw -or -not $raw.Trim()) { return @() }
    Add-Type -AssemblyName System.Web.Extensions
    $parsed = (New-Object System.Web.Script.Serialization.JavaScriptSerializer).DeserializeObject($raw)
    return @($parsed | ForEach-Object { [string]$_ })
}
function Save-Repos($list) { ConvertTo-Json -InputObject @($list) | Set-Content $ReposFile -Encoding UTF8 }
function Add-Repo($path) {
    $r = G $path @('rev-parse', '--show-toplevel') -Fail
    $top = ($r.out.Trim() -replace '/', '\')
    $list = @(Load-Repos | Where-Object { $_ -ne $top })
    Save-Repos (@($top) + $list)
    return $top
}

function To-WebUrl($u) {
    if (-not $u) { return $null }
    $u = $u.Trim()
    if ($u -match '^git@([^:]+):(.+?)(\.git)?$') { return "https://$($Matches[1])/$($Matches[2])" }
    if ($u -match '^ssh://git@([^/]+)/(.+?)(\.git)?$') { return "https://$($Matches[1])/$($Matches[2])" }
    if ($u -match '^https?://(?:[^@/]+@)?([^/]+)/(.+?)(\.git)?/?$') { return "https://$($Matches[1])/$($Matches[2])" }
    return $null
}

function Cut([string]$s, [int]$max = 600000) {
    if ($s.Length -gt $max) { return $s.Substring(0, $max) + "`n... (output truncated)" }
    return $s
}

# ---------- auto-update ----------
function Get-LocalVersion {
    $f = Join-Path $AppRoot 'VERSION'
    if (Test-Path -LiteralPath $f) { return (Get-Content -LiteralPath $f -Raw).Trim() }
    return '0.0.0'
}
function ConvertTo-Ver([string]$s) {
    if ($s -notmatch '^\d+(\.\d+){0,3}$') { return $null }
    $p = @($s.Split('.') | ForEach-Object { [int]$_ })
    while ($p.Count -lt 4) { $p += 0 }
    return [version]($p -join '.')
}
# Compares the local VERSION with the one on GitHub. Never throws: offline / errors just mean "no update".
function Check-Update {
    $cur = Get-LocalVersion
    $res = @{ current = $cur; latest = $cur; available = $false }
    if ($NoUpdate) { return $res }
    try {
        $ProgressPreference = 'SilentlyContinue'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $r = Invoke-WebRequest "https://raw.githubusercontent.com/$UpdateRepo/main/VERSION" -UseBasicParsing -TimeoutSec 6 -Headers @{ 'Cache-Control' = 'no-cache' }
        $txt = $r.Content
        if ($txt -is [byte[]]) { $txt = [Text.Encoding]::UTF8.GetString($txt) }
        $latest = ([string]$txt).Trim()
        $a = ConvertTo-Ver $cur; $b = ConvertTo-Ver $latest
        if ($a -and $b) { $res.latest = $latest; $res.available = ($b -gt $a) }
    } catch { }
    # A git checkout with local changes is never auto-updated: report it as "manual" instead of failing later.
    if ($res.available -and (Test-Path -LiteralPath (Join-Path $AppRoot '.git'))) {
        $dirty = $true
        try { $st = G $AppRoot @('status', '--porcelain'); $dirty = ($st.code -ne 0 -or [bool]$st.out.Trim()) } catch { }
        if ($dirty) { $res.available = $false; $res.manual = $true }
    }
    return $res
}
# Git checkouts are updated with a fast-forward pull (never touching local changes); plain downloads get the new zip.
function Install-Update {
    if ($NoUpdate) { throw 'Updates are disabled.' }
    $before = ConvertTo-Ver (Get-LocalVersion)
    if (Test-Path -LiteralPath (Join-Path $AppRoot '.git')) {
        $st = G $AppRoot @('status', '--porcelain') -Fail
        if ($st.out.Trim()) { throw 'This copy is a git checkout with local changes - update it manually with "git pull".' }
        G $AppRoot @('pull', '--ff-only') -Fail | Out-Null
    } else {
        $ProgressPreference = 'SilentlyContinue'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $tmp = Join-Path $env:TEMP ('ghh-up-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force $tmp | Out-Null
        try {
            $zip = Join-Path $tmp 'update.zip'
            Invoke-WebRequest "https://github.com/$UpdateRepo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
            $ex = Join-Path $tmp 'x'; Expand-Archive $zip $ex -Force
            $top = Get-ChildItem $ex -Directory | Select-Object -First 1
            foreach ($need in @('VERSION', 'ui\index.html', 'windows\server.ps1', 'linux\server.py')) {
                if (-not $top -or -not (Test-Path -LiteralPath (Join-Path $top.FullName $need))) { throw 'Downloaded update is incomplete.' }
            }
            Get-ChildItem $top.FullName -Force | Where-Object { $_.Name -ne 'VERSION' } | Copy-Item -Destination $AppRoot -Recurse -Force
            Copy-Item (Join-Path $top.FullName 'VERSION') $AppRoot -Force   # last, so a half-finished copy is retried next time
        } finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }
    $after = ConvertTo-Ver (Get-LocalVersion)
    if (-not $after -or ($before -and $after -le $before)) { throw 'The update did not change the version.' }
    $script:restart = $true
}
# Starts a fresh copy of this script on the same port (it waits for the port to be released) and lets this one exit.
function Do-Restart {
    try { $listener.Stop() } catch { }
    $a = @('-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', ('"' + $ScriptPath + '"'), '-Port', $Port, '-NoBrowser', '-Restarted')
    if ($SkipInstall) { $a += '-SkipInstall' }
    Start-Process powershell -ArgumentList $a
}

# ---------- API ----------
function Api([string]$name, $b) {
    $repo = [string]$b.repo

    # Only operate on repositories the user has added in the app.
    $exempt = @('state', 'pickfolder', 'install', 'ghlogin', 'ghloginStatus', 'ghloginCancel', 'ghlogout', 'setIdentity', 'addRepo', 'removeRepo', 'init', 'clone', 'updateCheck', 'updateInstall')
    if (($exempt -notcontains $name) -and -not ($name -eq 'open' -and [string]$b.what -eq 'url')) {
        if (-not @(Load-Repos | Where-Object { $_ -ieq $repo })) { throw 'Unknown repository.' }
    }
    if (@('switch', 'createBranch', 'deleteBranch', 'merge', 'publish') -contains $name) { Check-Ref $b.name }
    if (@('show', 'showDiff') -contains $name) { Check-Ref $b.sha }
    if (@('addRemote', 'clone') -contains $name) { Check-Url $b.url }

    switch ($name) {

        'state' {
            $gh = Get-Gh; $authed = $false; $ghUser = ''
            if ($gh) {
                $authed = ((Invoke-Exe $gh $null @('auth', 'status', '--hostname', 'github.com')).code -eq 0)
                if ($authed) { $ghUser = (Invoke-Exe $gh $null @('api', 'user', '--jq', '.login')).out.Trim() }
            }
            $name_ = ''; $email = ''
            if (Get-Git) {
                $name_ = (G $null @('config', '--global', 'user.name')).out.Trim()
                $email = (G $null @('config', '--global', 'user.email')).out.Trim()
            }
            $list = @(Load-Repos | Where-Object { Test-Path -LiteralPath $_ })
            return @{ git = [bool](Get-Git); gh = [bool]$gh; ghAuthed = $authed; ghUser = $ghUser; repos = $list; userName = $name_; userEmail = $email; docs = [Environment]::GetFolderPath('MyDocuments'); sep = '\'; os = 'windows' }
        }

        'updateCheck'   { return (Check-Update) }
        'updateInstall' { Install-Update; return @{ ok = $true } }

        'pickfolder' {
            Add-Type -AssemblyName System.Windows.Forms
            $f = New-Object System.Windows.Forms.Form
            $f.TopMost = $true; $f.Opacity = 0; $f.ShowInTaskbar = $false; $f.StartPosition = 'CenterScreen'
            $f.Show()
            $d = New-Object System.Windows.Forms.FolderBrowserDialog
            if ($b.start -and (Test-Path -LiteralPath $b.start)) { $d.SelectedPath = $b.start }
            $res = $d.ShowDialog($f)
            $f.Close()
            if ($res -eq 'OK') { return @{ path = $d.SelectedPath } }
            return @{ path = $null }
        }

        'install' {
            $what = if ($b.what -eq 'gh') { 'gh' } else { 'git' }
            $ok = Install-Dep $what
            if (-not $ok) { throw "Could not install $what automatically. Install it manually and restart the app." }
            return @{ ok = $true }
        }

        # Browser-based GitHub login (device flow) driven from the app: we start `gh auth login --web`,
        # read the one-time code from its output and show it in the UI; gh opens the browser itself.
        'ghlogin' {
            $gh = Get-Gh; if (-not $gh) { throw 'GitHub CLI (gh) is not installed.' }
            if ($script:LoginProc -and -not $script:LoginProc.HasExited) { try { $script:LoginProc.Kill() } catch { } }
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $gh
            $psi.Arguments = 'auth login --hostname github.com --git-protocol https --web --skip-ssh-key'
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.RedirectStandardInput = $true
            $psi.EnvironmentVariables['GH_PROMPT_DISABLED'] = '1'
            $psi.EnvironmentVariables['GH_NO_UPDATE_NOTIFIER'] = '1'
            $p = [System.Diagnostics.Process]::Start($psi)
            $p.StandardInput.Close()
            $script:LoginProc = $p
            $script:LoginOut = $p.StandardOutput.ReadToEndAsync()
            $errLines = New-Object System.Text.StringBuilder
            $code = $null
            $deadline = (Get-Date).AddSeconds(25)
            while (-not $code -and (Get-Date) -lt $deadline -and -not $p.HasExited) {
                $t = $p.StandardError.ReadLineAsync()
                while (-not $t.Wait(300)) { if ((Get-Date) -gt $deadline -or $p.HasExited) { break } }
                if (-not $t.IsCompleted) { continue }
                if ($null -eq $t.Result) { break }   # end of stream; blank lines are normal and skipped
                [void]$errLines.AppendLine($t.Result)
                if ($t.Result -match '\b([A-Z0-9]{4}-[A-Z0-9]{4})\b') { $code = $Matches[1] }
            }
            if (-not $code) {
                try { if (-not $p.HasExited) { $p.Kill() } } catch { }
                $msg = $errLines.ToString().Trim()
                throw $(if ($msg) { $msg } else { 'Could not start GitHub sign-in.' })
            }
            $script:LoginErr = $p.StandardError.ReadToEndAsync()
            return @{ code = $code; url = 'https://github.com/login/device' }
        }

        'ghloginStatus' {
            $gh = Get-Gh
            $running = [bool]($script:LoginProc -and -not $script:LoginProc.HasExited)
            if ($running) { return @{ state = 'waiting' } }
            if ($script:LoginProc) {
                $ok = ($script:LoginProc.ExitCode -eq 0)
                $err = if ($script:LoginErr) { $script:LoginErr.Result.Trim() } else { '' }
                $script:LoginProc = $null
                if ($ok) {
                    Invoke-Exe $gh $null @('auth', 'setup-git', '--hostname', 'github.com') | Out-Null
                    return @{ state = 'done' }
                }
                return @{ state = 'failed'; error = $err }
            }
            return @{ state = 'idle' }
        }

        'ghloginCancel' {
            if ($script:LoginProc -and -not $script:LoginProc.HasExited) { try { $script:LoginProc.Kill() } catch { } }
            $script:LoginProc = $null
            return @{ ok = $true }
        }

        'ghlogout' {
            $gh = Get-Gh; if (-not $gh) { throw 'GitHub CLI (gh) is not installed.' }
            $r = Invoke-Exe $gh $null @('auth', 'logout', '--hostname', 'github.com')
            if ($r.code -ne 0) { throw ($r.err + $r.out).Trim() }
            return @{ ok = $true }
        }

        'setIdentity' {
            G $null @('config', '--global', 'user.name', [string]$b.name) -Fail | Out-Null
            G $null @('config', '--global', 'user.email', [string]$b.email) -Fail | Out-Null
            return @{ ok = $true }
        }

        'addRepo' {
            Require-Repo $repo
            return @{ path = (Add-Repo $repo) }
        }

        'removeRepo' {
            Save-Repos @(Load-Repos | Where-Object { $_ -ne $repo })
            return @{ ok = $true }
        }

        'init' {
            New-Item -ItemType Directory -Force $repo | Out-Null
            G $repo @('init', '-b', 'main') -Fail | Out-Null
            return @{ path = (Add-Repo $repo) }
        }

        'clone' {
            $url = ([string]$b.url).Trim()
            if (-not $url) { throw 'Enter a repository URL.' }
            $short = $url
            if ($short -notmatch '^(https?://|git@|ssh://|[A-Za-z]:\\|\\\\|/)') { $short = "https://github.com/$url"; $url = $short }   # "owner/repo" shorthand
            $leaf = (($short.TrimEnd('/') -split '[/\\:]')[-1]) -replace '\.git$', ''
            if (-not $leaf) { throw 'Could not determine repository name from URL.' }
            $parent = [string]$b.parent
            New-Item -ItemType Directory -Force $parent | Out-Null
            $dest = Join-Path $parent $leaf
            if (Test-Path -LiteralPath $dest) { throw "Folder already exists: $dest" }
            G $parent @('clone', $url, $dest) -Fail | Out-Null
            return @{ path = (Add-Repo $dest) }
        }

        'status' {
            Require-Repo $repo
            $r = G $repo @('status', '--porcelain=v1', '-z', '--untracked-files=all') -Fail
            $parts = $r.out -split "`0"
            $i = 0; $files = New-Object System.Collections.ArrayList
            while ($i -lt $parts.Count) {
                $e = $parts[$i]; $i++
                if ($e.Length -lt 4) { continue }
                $x = [string]$e[0]; $y = [string]$e[1]; $orig = $null
                if (($x -eq 'R') -or ($x -eq 'C') -or ($y -eq 'R') -or ($y -eq 'C')) { $orig = $parts[$i]; $i++ }
                $m = $null   # mtime+size lets the UI notice edits to files that were already modified
                if ($files.Count -lt 500) {
                    $fi = New-Object System.IO.FileInfo (Join-Path $repo $e.Substring(3))
                    if ($fi.Exists) { $m = "$($fi.LastWriteTimeUtc.Ticks)-$($fi.Length)" }
                }
                [void]$files.Add(@{ path = $e.Substring(3); x = $x; y = $y; orig = $orig; m = $m })
            }
            $br = G $repo @('symbolic-ref', '--short', '-q', 'HEAD')
            $branch = if ($br.code -eq 0) { $br.out.Trim() } else { '(detached HEAD)' }
            $has = ((G $repo @('rev-parse', '--verify', '-q', 'HEAD')).code -eq 0)
            $up = G $repo @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
            $upstream = if ($up.code -eq 0) { $up.out.Trim() } else { $null }
            $ahead = 0; $behind = 0
            if ($upstream) {
                $c = (G $repo @('rev-list', '--left-right', '--count', 'HEAD...@{u}')).out.Trim() -split '\s+'
                if ($c.Count -ge 2) { $ahead = [int]$c[0]; $behind = [int]$c[1] }
            }
            $remote = (G $repo @('remote')).out.Trim() -split "`r?`n" | Where-Object { $_ } | Select-Object -First 1
            if (-not $remote) { $remote = $null }  # avoid AutomationNull serializing as {} (truthy in JS)
            $rurl = if ($remote) { (G $repo @('remote', 'get-url', $remote)).out.Trim() } else { $null }
            $last = if ($has) { (G $repo @('log', '-1', '--format=%s')).out.Trim() } else { $null }
            $merging = Test-Path -LiteralPath (Join-Path $repo '.git\MERGE_HEAD')
            $headSha = if ($has) { (G $repo @('rev-parse', 'HEAD')).out.Trim() } else { '' }
            return @{ branch = $branch; head = $headSha; hasCommits = $has; upstream = $upstream; ahead = $ahead; behind = $behind
                      remote = $remote; remoteUrl = (Mask $rurl); webUrl = (To-WebUrl $rurl); lastSubject = $last; merging = $merging; files = $files.ToArray() }
        }

        'stage' {
            $p = [string[]]@($b.paths)
            G $repo (@('add', '--') + $p) -Fail | Out-Null
            return @{ ok = $true }
        }
        'unstage' {
            $p = [string[]]@($b.paths)
            $has = ((G $repo @('rev-parse', '--verify', '-q', 'HEAD')).code -eq 0)
            if ($has) { G $repo (@('restore', '--staged', '--') + $p) -Fail | Out-Null }
            else      { G $repo (@('rm', '--cached', '-r', '-q', '--') + $p) -Fail | Out-Null }
            return @{ ok = $true }
        }
        'stageAll'   { G $repo @('add', '-A') -Fail | Out-Null; return @{ ok = $true } }
        'unstageAll' {
            $has = ((G $repo @('rev-parse', '--verify', '-q', 'HEAD')).code -eq 0)
            if ($has) { G $repo @('reset', '-q') -Fail | Out-Null } else { G $repo @('rm', '--cached', '-r', '-q', '.') -Fail | Out-Null }
            return @{ ok = $true }
        }

        'discard' {
            foreach ($f in @($b.files)) {
                $p = [string]$f.path
                if ($f.untracked) { G $repo @('clean', '-fd', '--', $p) -Fail | Out-Null }
                else {
                    $has = ((G $repo @('rev-parse', '--verify', '-q', 'HEAD')).code -eq 0)
                    if ($has) { G $repo @('restore', '--source=HEAD', '--staged', '--worktree', '--', $p) -Fail | Out-Null }
                    else { G $repo @('rm', '-f', '--cached', '--', $p) | Out-Null; Remove-Item -LiteralPath (Join-Path $repo $p) -Force -ErrorAction SilentlyContinue }
                }
            }
            return @{ ok = $true }
        }

        'diff' {
            $path = [string]$b.path
            if ($b.untracked) {
                $r = G $repo @('diff', '--no-index', '--no-color', '--', '/dev/null', $path)
            } else {
                $has = ((G $repo @('rev-parse', '--verify', '-q', 'HEAD')).code -eq 0)
                if ($has) { $r = G $repo @('diff', 'HEAD', '--no-color', '-M', '--', $path) }
                else      { $r = G $repo @('diff', '--cached', '--no-color', '--', $path) }
            }
            return @{ diff = (Cut $r.out) }
        }

        'commit' {
            $msg = ([string]$b.summary).Trim()
            if (-not $msg) { throw 'Enter a commit summary.' }
            if (([string]$b.description).Trim()) { $msg += "`n`n" + ([string]$b.description).Trim() }
            $a = @('commit', '-F', '-')
            if ($b.amend) { $a += '--amend' }
            $r = G $repo $a $msg -Fail
            return @{ ok = $true; out = $r.out }
        }

        'undoCommit' {
            G $repo @('reset', '--soft', 'HEAD~1') -Fail | Out-Null
            return @{ ok = $true }
        }

        'log' {
            $skip = if ($b.skip) { [int]$b.skip } else { 0 }
            $r = G $repo @('log', '-n', '200', "--skip=$skip", '--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%D')
            $unp = @()
            $up = G $repo @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
            if ($up.code -eq 0) { $unp = @((G $repo @('rev-list', '@{u}..HEAD')).out -split "`r?`n" | Where-Object { $_ }) }
            $commits = @()
            foreach ($line in ($r.out -split "`r?`n")) {
                if (-not $line) { continue }
                $f = $line -split [char]0x1f
                if ($f.Count -lt 5) { continue }
                $commits += @{ sha = $f[0]; short = $f[1]; author = $f[2]; date = $f[3]; subject = $f[4]; refs = $(if ($f.Count -gt 5) { $f[5] } else { '' }) }
            }
            return @{ commits = $commits; unpushed = $unp }
        }

        'show' {
            $sha = [string]$b.sha
            $m = (G $repo @('show', '-s', '--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%B', $sha) -Fail).out
            $f = $m -split [char]0x1f, 5
            $ns = (G $repo @('show', '-m', '--first-parent', '--name-status', '--format=', '-M', $sha) -Fail).out
            $files = @()
            foreach ($line in ($ns -split "`r?`n")) {
                if (-not $line) { continue }
                $t = $line -split "`t"
                $files += @{ status = $t[0].Substring(0, 1); path = $t[$t.Count - 1] }
            }
            return @{ sha = $f[0]; author = $f[1]; email = $f[2]; date = $f[3]; message = $f[4].Trim(); files = $files }
        }

        'showDiff' {
            $r = G $repo @('show', '-m', '--first-parent', '--no-color', '--format=', '-M', [string]$b.sha, '--', [string]$b.path)
            return @{ diff = (Cut $r.out) }
        }

        'branches' {
            $r = G $repo @('for-each-ref', '--format=%(refname:short)%09%(HEAD)%09%(upstream:short)%09%(refname)', 'refs/heads', 'refs/remotes') -Fail
            $local = @(); $remote = @()
            foreach ($line in ($r.out -split "`r?`n")) {
                if (-not $line) { continue }
                $t = $line -split "`t"
                if ($t[3] -like 'refs/heads/*') { $local += @{ name = $t[0]; current = ($t[1] -eq '*'); upstream = $t[2] } }
                elseif ($t[3] -notlike '*/HEAD') { $remote += @{ name = $t[0] } }
            }
            $names = $local | ForEach-Object { $_.name }
            $remote = @($remote | Where-Object { ($_.name -replace '^[^/]+/', '') -notin $names })
            return @{ local = $local; remote = $remote }
        }

        'switch' {
            G $repo @('switch', [string]$b.name) -Fail | Out-Null; return @{ ok = $true }
        }
        'createBranch' {
            G $repo @('switch', '-c', [string]$b.name) -Fail | Out-Null; return @{ ok = $true }
        }
        'deleteBranch' {
            $flag = if ($b.force) { '-D' } else { '-d' }
            G $repo @('branch', $flag, [string]$b.name) -Fail | Out-Null; return @{ ok = $true }
        }
        'merge' {
            $r = G $repo @('merge', '--no-edit', [string]$b.name) -Fail
            return @{ ok = $true; out = $r.out }
        }
        'abortMerge' { G $repo @('merge', '--abort') -Fail | Out-Null; return @{ ok = $true } }

        'stash'    { $r = G $repo @('stash', 'push', '-u', '-m', 'GitHub Helper stash') -Fail; return @{ ok = $true; out = $r.out } }
        'stashPop' { $r = G $repo @('stash', 'pop') -Fail; return @{ ok = $true; out = $r.out } }

        'fetch' { $r = G $repo @('fetch', '--all', '--prune') -Fail; return @{ ok = $true; out = ($r.out + $r.err) } }
        'pull'  { $r = G $repo @('pull', '--no-edit') -Fail; return @{ ok = $true; out = ($r.out + $r.err) } }
        'push'  {
            $up = G $repo @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
            if ($up.code -eq 0) { $r = G $repo @('push') -Fail }
            else {
                $remote = (G $repo @('remote')).out.Trim() -split "`r?`n" | Where-Object { $_ } | Select-Object -First 1
                if (-not $remote) { throw 'No remote configured. Use "Publish repository" first.' }
                $r = G $repo @('push', '-u', $remote, 'HEAD') -Fail
            }
            return @{ ok = $true; out = ($r.out + $r.err) }
        }

        'addRemote' {
            G $repo @('remote', 'add', 'origin', [string]$b.url) -Fail | Out-Null; return @{ ok = $true }
        }

        'publish' {
            $gh = Get-Gh; if (-not $gh) { throw 'GitHub CLI (gh) is needed to create repositories. Install it from the menu.' }
            $vis = if ($b.private) { '--private' } else { '--public' }
            $r = Invoke-Exe $gh $repo @('repo', 'create', [string]$b.name, $vis, '--source', '.', '--remote', 'origin', '--push')
            if ($r.code -ne 0) { throw ($r.err + $r.out).Trim() }
            return @{ ok = $true; out = ($r.out + $r.err) }
        }

        'open' {
            switch ([string]$b.what) {
                'explorer' { Start-Process explorer.exe -ArgumentList (Quote-Arg $repo) }
                'terminal' { Start-Process powershell.exe -WorkingDirectory $repo -ArgumentList '-NoExit' }
                'code'     { if ($repo -match '[&|<>^%"!]') { throw 'Path contains unsupported characters.' }; Start-Process cmd.exe -WindowStyle Hidden -ArgumentList '/c', 'code', (Quote-Arg $repo) }
                'url'      { $u = [string]$b.url; if ($u -notmatch '^https://[A-Za-z0-9.-]+(:\d+)?(/[^\s"<>^|&`]*)?$') { throw 'Refusing to open this URL' }; Start-Process $u }
                default    { throw 'Unknown open target' }
            }
            return @{ ok = $true }
        }

        default { throw "Unknown action: $name" }
    }
}

# ---------- first run: make sure git and GitHub CLI are present ----------
if (-not $SkipInstall) { Ensure-Deps }

# ---------- HTTP server ----------
$token = [Guid]::NewGuid().ToString('N')
$listener = $null
# After an update restart we must come back on the very same port (the open window talks to it), so keep retrying it.
$attempts = if ($Restarted) { 40 } else { 1 }
$candidates = if ($Restarted) { @($Port) } else { @($Port..($Port + 20)) }
for ($i = 0; $i -lt $attempts -and -not $listener; $i++) {
    foreach ($p in $candidates) {
        try {
            $l = New-Object System.Net.HttpListener
            $l.Prefixes.Add("http://127.0.0.1:$p/")
            $l.Start()
            $listener = $l; $Port = $p; break
        } catch { }
    }
    if (-not $listener -and $Restarted) { Start-Sleep -Milliseconds 500 }
}
if (-not $listener) { Write-Host 'Could not open a local port.' -ForegroundColor Red; exit 1 }

$url = "http://127.0.0.1:$Port/"
Write-Host "GitHub Helper is running at $url" -ForegroundColor Green
Write-Host 'Close this window (or press Ctrl+C) to quit.'
if (-not (Get-Git)) { Write-Host 'Git was not found - the app will offer to install it.' -ForegroundColor Yellow }

# Start Menu entry "GitHub Helper" with the app icon (refreshed each start so it keeps pointing at this folder).
if (-not $NoShortcut -and -not $Restarted) { try { & (Join-Path $Root 'create-shortcut.ps1') -Quiet } catch { } }

if (-not $NoBrowser) {
    $edge = Find-Exe 'msedge' @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")
    if ($edge) { Start-Process $edge -ArgumentList "--app=$url", '--window-size=1300,820' } else { Start-Process $url }
}

function Send($ctx, [int]$code, [byte[]]$bytes, [string]$ctype) {
    $ctx.Response.StatusCode = $code
    $ctx.Response.ContentType = $ctype
    $ctx.Response.Headers.Add('Cache-Control', 'no-store')
    $ctx.Response.Headers.Add('X-Content-Type-Options', 'nosniff')
    $ctx.Response.Headers.Add('X-Frame-Options', 'DENY')
    $ctx.Response.Headers.Add('Referrer-Policy', 'no-referrer')
    $ctx.Response.Headers.Add('Cross-Origin-Resource-Policy', 'same-origin')
    $ctx.Response.Headers.Add('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    $ctx.Response.ContentLength64 = $bytes.Length
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $ctx.Response.OutputStream.Close()
}

$utf8 = New-Object System.Text.UTF8Encoding($false)
$lastRequest = Get-Date
$seen = $false
$script:byeAt = $null
$startedAt = Get-Date
try {
    while ($listener.IsListening) {
        if ($script:restart) { Do-Restart; return }
        $task = $listener.GetContextAsync()
        while (-not $task.Wait(1000)) {
            if ($Restarted -and -not $seen -and ((Get-Date) - $startedAt).TotalSeconds -gt 90) { Write-Host 'No window connected after the update - exiting.'; return }
            if ($seen -and ((Get-Date) - $lastRequest).TotalSeconds -gt 30) { Write-Host 'Window closed (no heartbeat for 30 s) - exiting.'; return }
            if (($script:byeAt) -and ((Get-Date) - $script:byeAt).TotalSeconds -gt 2) { Write-Host 'Window closed - exiting.'; return }
        }
        $ctx = $task.Result
        $req = $ctx.Request
        try {
            $hostH = $req.Headers['Host']
            if ($hostH -ne "127.0.0.1:$Port" -and $hostH -ne "localhost:$Port") { Send $ctx 403 $utf8.GetBytes('bad host') 'text/plain'; continue }
            $path = $req.Url.AbsolutePath
            if ($req.HttpMethod -eq 'GET' -and $path -eq '/') {
                $seen = $true; $script:byeAt = $null; $lastRequest = Get-Date
                $html = (Get-Content $UiFile -Raw -Encoding UTF8).Replace('__TOKEN__', $token).Replace('__VERSION__', (Get-LocalVersion))
                Send $ctx 200 $utf8.GetBytes($html) 'text/html; charset=utf-8'
            }
            elseif ($req.HttpMethod -eq 'POST' -and $path -like '/api/*') {
                if ($req.Headers['X-Token'] -ne $token) { Send $ctx 403 $utf8.GetBytes('{"error":"bad token"}') 'application/json'; continue }
                $origin = $req.Headers['Origin']
                if ($origin -and $origin -ne "http://127.0.0.1:$Port" -and $origin -ne "http://localhost:$Port") { Send $ctx 403 $utf8.GetBytes('{"error":"bad origin"}') 'application/json'; continue }
                if ($req.ContentLength64 -gt 5MB) { Send $ctx 413 $utf8.GetBytes('{"error":"too large"}') 'application/json'; continue }
                $lastRequest = Get-Date
                if ($path -eq "/api/bye") { $script:byeAt = Get-Date; Send $ctx 200 $utf8.GetBytes("{}") "application/json"; continue }
                $script:byeAt = $null
                $sr = New-Object System.IO.StreamReader($req.InputStream, $utf8)
                $raw = $sr.ReadToEnd()
                $body = if ($raw) { $raw | ConvertFrom-Json } else { [pscustomobject]@{} }
                $name = $path.Substring(5)
                try {
                    $res = Api $name $body
                    $json = ConvertTo-Json -InputObject $res -Depth 8 -Compress
                    Send $ctx 200 $utf8.GetBytes($json) 'application/json; charset=utf-8'
                } catch {
                    $json = ConvertTo-Json -InputObject @{ error = (Mask $_.Exception.Message) } -Compress
                    Send $ctx 400 $utf8.GetBytes($json) 'application/json; charset=utf-8'
                }
            }
            else { Send $ctx 404 $utf8.GetBytes('not found') 'text/plain' }
        } catch {
            try { Send $ctx 500 $utf8.GetBytes("server error: $($_.Exception.Message)") 'text/plain' } catch { }
        }
    }
} finally {
    $listener.Stop()
}
