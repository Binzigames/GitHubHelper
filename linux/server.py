#!/usr/bin/env python3
"""GitHub Helper - local server for Linux (Python 3.7+, standard library only, plus git / gh).

Serves the shared UI (../ui/index.html) on http://127.0.0.1:<port> and translates UI actions
into git / gh commands. Mirrors windows/server.ps1.
"""
import argparse
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IS_MAC = sys.platform == 'darwin'   # this server runs on both Linux and macOS
ROOT = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(os.path.dirname(ROOT), 'ui', 'index.html')
if IS_MAC:
    DATA_DIR = os.path.expanduser('~/Library/Application Support/GitHubHelper')
else:
    DATA_DIR = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'GitHubHelper')
REPOS_FILE = os.path.join(DATA_DIR, 'repos.json')
LOCAL_BIN = os.path.expanduser('~/.local/bin')

API_LOCK = threading.Lock()
LOGIN = {'proc': None, 'err': [], 'out': []}
STATE_TTL = 30        # seconds the git/gh/identity info is cached
CACHE = {'exe': {}, 'state': None, 'state_at': 0.0, 'remote': {}, 'subject': {}, 'gitdir': {}}


class ApiError(Exception):
    pass


# ---------- tool discovery ----------
def find_exe(name):
    """Resolve an executable once and remember it (shutil.which walks PATH on every call)."""
    c = CACHE['exe'].get(name)
    if c and os.path.isfile(c):
        return c
    p = shutil.which(name)
    if not p:
        for d in (LOCAL_BIN, '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/snap/bin'):
            c = os.path.join(d, name)
            if os.path.isfile(c) and os.access(c, os.X_OK):
                p = c
                break
    if p:
        CACHE['exe'][name] = p
    return p


def get_git():
    return find_exe('git')


def get_gh():
    return find_exe('gh')


# ---------- dependency installer ----------
def test_dep(what):
    return bool(get_gh() if what == 'gh' else get_git())


def priv_prefix():
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        return []
    if shutil.which('pkexec'):
        return ['pkexec']
    if shutil.which('sudo'):
        return ['sudo', '-n']
    return None


def pm_commands(what):
    """Return the list of commands (without privilege prefix) that install the package, or None."""
    pkg = 'gh' if what == 'gh' else 'git'
    if IS_MAC:
        brew = find_exe('brew')
        return [[brew, 'install', pkg]] if brew else None
    if shutil.which('apt-get'):
        return [['apt-get', 'update'], ['apt-get', 'install', '-y', pkg]]
    if shutil.which('dnf'):
        return [['dnf', 'install', '-y', pkg]]
    if shutil.which('pacman'):
        return [['pacman', '-S', '--noconfirm', 'github-cli' if what == 'gh' else 'git']]
    if shutil.which('zypper'):
        return [['zypper', '--non-interactive', 'install', pkg]]
    if shutil.which('apk'):
        return [['apk', 'add', pkg]]
    return None


# ---------- progress reporting ----------
# The window polls the 'progress' endpoint while a long operation runs; operations publish their state here.
PROG = {'active': False, 'op': '', 'what': '', 'phase': '', 'pct': -1, 'detail': '', 'bytes': 0, 'total': 0}


def prog_start(op, what=''):
    PROG.update({'active': True, 'op': op, 'what': what, 'phase': '', 'pct': -1, 'detail': '', 'bytes': 0, 'total': 0})


def prog_end():
    PROG['active'] = False


# git writes e.g. "Receiving objects:  45% (450/1000), 1.2 MiB | 2.0 MiB/s" (lines separated by \r).
GIT_PROGRESS = re.compile(r'^(?:remote:\s*)?([A-Za-z][A-Za-z ]*?):\s+(\d+)%\s*(.*)$')


def parse_progress(line):
    m = GIT_PROGRESS.match(line)
    if m:
        PROG['phase'], PROG['pct'] = m.group(1).strip(), int(m.group(2))
        PROG['detail'] = m.group(3).strip().rstrip(',. ')


def download(url, dest):
    """Download url to dest in chunks, publishing bytes / percent for the window's progress bar."""
    req = urllib.request.Request(url, headers={'User-Agent': 'GitHubHelper'})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        got, last = 0, 0.0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if time.time() - last > 0.15:
                last = time.time()
                PROG['bytes'], PROG['total'] = got, total
                PROG['pct'] = int(100 * got / total) if total else -1
        PROG['bytes'], PROG['total'], PROG['pct'] = got, total, 100


# ---------- private tools (no root needed) ----------
def install_direct_gh():
    """Download the official GitHub CLI release into ~/.local/bin (checksum verified)."""
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64',
            'i386': '386', 'i686': '386', 'armv6l': 'armv6', 'armv7l': 'armv6'}.get(platform.machine().lower())
    if not arch:
        raise ApiError('Unsupported CPU architecture: ' + platform.machine())
    prog_start('download', 'gh')
    tmp = tempfile.mkdtemp(prefix='ghh-dl-')
    try:
        req = urllib.request.Request('https://api.github.com/repos/cli/cli/releases/latest', headers={'User-Agent': 'GitHubHelper'})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.loads(r.read().decode('utf-8'))
        assets = rel.get('assets', [])
        pattern = r'_macOS_%s\.(zip|tar\.gz)$' if IS_MAC else r'_linux_%s\.tar\.gz$'
        asset = next((a for a in assets if re.search(pattern % arch, a['name'])), None)
        if not asset:
            raise ApiError('No download found for GitHub CLI on this system.')
        archive = os.path.join(tmp, asset['name'])
        download(asset['browser_download_url'], archive)
        sum_asset = next((a for a in assets if a['name'].endswith('checksums.txt')), None)
        if sum_asset:
            req = urllib.request.Request(sum_asset['browser_download_url'], headers={'User-Agent': 'GitHubHelper'})
            with urllib.request.urlopen(req, timeout=30) as r:
                sums = r.read().decode('utf-8', 'replace')
            expected = None
            for line in sums.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip('*') == asset['name']:
                    expected = parts[0].lower()
            h = hashlib.sha256()
            with open(archive, 'rb') as f:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    h.update(chunk)
            if not expected or h.hexdigest() != expected:
                raise ApiError('Checksum mismatch - download discarded.')
        PROG['op'], PROG['pct'] = 'extract', -1
        os.makedirs(LOCAL_BIN, exist_ok=True)
        dest = os.path.join(LOCAL_BIN, 'gh')
        if archive.endswith('.zip'):
            import zipfile
            with zipfile.ZipFile(archive) as z:
                name = next((n for n in z.namelist() if n.endswith('/bin/gh')), None)
                if not name:
                    raise ApiError('gh binary not found in the downloaded archive.')
                with z.open(name) as src, open(dest + '.tmp', 'wb') as out:
                    shutil.copyfileobj(src, out)
        else:
            with tarfile.open(archive) as t:
                member = next((m for m in t.getmembers() if m.isfile() and m.name.endswith('/bin/gh')), None)
                if not member:
                    raise ApiError('gh binary not found in the downloaded archive.')
                src = t.extractfile(member)
                with open(dest + '.tmp', 'wb') as out:
                    shutil.copyfileobj(src, out)
        os.chmod(dest + '.tmp', 0o755)
        os.replace(dest + '.tmp', dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        prog_end()
    CACHE['exe'].pop('gh', None)


def install_dep(what):
    """gh: private download (no root). git: the system package manager (git cannot be bundled on Linux / macOS)."""
    CACHE['state'] = None
    if what == 'gh':
        try:
            install_direct_gh()
        except Exception as e:  # noqa: BLE001
            print('Direct download failed: %s' % e)
        if test_dep('gh'):
            return True
    cmds = pm_commands(what)
    prefix = [] if IS_MAC else priv_prefix()   # Homebrew must not run as root / via sudo
    shown = ' && '.join(('' if IS_MAC else 'sudo ') + ' '.join(c) for c in cmds) if cmds else None
    if cmds and prefix is not None:
        print('Installing %s with the system package manager...' % what)
        prog_start('install', what)   # no byte counts from package managers: indeterminate bar
        try:
            ok = True
            for c in cmds:
                r = subprocess.run(prefix + c, stdin=subprocess.DEVNULL)
                if r.returncode != 0:
                    ok = False
                    break
        finally:
            prog_end()
        CACHE['exe'].pop(what, None)
        if ok and test_dep(what):
            return True
    if what == 'gh':
        return test_dep('gh')
    if IS_MAC and not shown:
        raise ApiError('Git is not installed. Run "xcode-select --install" (or install Homebrew and "brew install git"), then restart the app.')
    hint = ' Run in a terminal: ' + shown if shown else ''
    raise ApiError('Could not install git automatically.%s' % hint)


# ---------- security helpers ----------
def mask(s):
    if not s:
        return s
    return re.sub(r'(?i)(https?://)[^/\s@]+@', r'\1***@', s)


def check_ref(v):
    v = '' if v is None else str(v)
    if not v.strip() or v.startswith('-') or re.search(r'[\x00-\x1f]', v):
        raise ApiError('Invalid name.')


def check_url(v):
    v = ('' if v is None else str(v)).strip()
    if not v or v.startswith('-') or re.match(r'^(ext|fd)::', v) or re.search(r'[\x00-\x1f]', v):
        raise ApiError('Invalid URL.')


# ---------- process helpers ----------
class Res:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err


def invoke_exe(exe, cwd, args, stdin=None):
    env = dict(os.environ)
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_EDITOR'] = 'true'
    env['GCM_INTERACTIVE'] = 'auto'
    env['GH_PROMPT_DISABLED'] = '1'
    env['GH_NO_UPDATE_NOTIFIER'] = '1'
    p = subprocess.run([exe] + list(args), cwd=cwd or None, env=env,
                       input=stdin.encode('utf-8') if stdin else b'',
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return Res(p.returncode, p.stdout.decode('utf-8', 'replace'), p.stderr.decode('utf-8', 'replace'))


def invoke_live(exe, cwd, args, op):
    """Like invoke_exe, but publishes git's progress lines while the process runs (clone / fetch / pull / push)."""
    env = dict(os.environ)
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_EDITOR'] = 'true'
    env['GCM_INTERACTIVE'] = 'auto'
    env['GH_PROMPT_DISABLED'] = '1'
    env['GH_NO_UPDATE_NOTIFIER'] = '1'
    prog_start(op)
    try:
        p = subprocess.Popen([exe] + list(args), cwd=cwd or None, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out_chunks, err_chunks = [], []

        def read_out():
            out_chunks.append(p.stdout.read())

        t = threading.Thread(target=read_out, daemon=True)
        t.start()
        line = b''
        while True:
            chunk = os.read(p.stderr.fileno(), 2048)
            if not chunk:
                break
            err_chunks.append(chunk)
            for ch in chunk:
                if ch in (10, 13):   # \n or \r: git redraws progress lines with \r
                    if line:
                        parse_progress(line.decode('utf-8', 'replace'))
                        line = b''
                else:
                    line += bytes((ch,))
        p.wait()
        t.join(5)
        return Res(p.returncode, b''.join(out_chunks).decode('utf-8', 'replace'), b''.join(err_chunks).decode('utf-8', 'replace'))
    finally:
        prog_end()


def G(repo, git_args, stdin=None, fail=False, live=''):
    git = get_git()
    if not git:
        raise ApiError('Git is not installed. Use the "Install Git" button in the app.')
    allargs = ['-c', 'core.quotepath=false', '-c', 'color.ui=false', '-c', 'core.safecrlf=false',
               '-c', 'protocol.ext.allow=never', '-c', 'protocol.file.allow=user'] + list(git_args)
    r = invoke_live(git, repo, allargs, live) if live else invoke_exe(git, repo, allargs, stdin)
    if fail and r.code != 0:
        raise ApiError(r.err.strip() or r.out.strip())
    return r


def require_repo(repo):
    if not repo or not os.path.isdir(repo):
        raise ApiError('Folder not found: %s' % repo)


def has_head(repo):
    return G(repo, ['rev-parse', '--verify', '-q', 'HEAD']).code == 0


def upstream_of(repo):
    r = G(repo, ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'])
    return r.out.strip() if r.code == 0 else None


# ---------- repo list ----------
def load_repos():
    if not os.path.exists(REPOS_FILE):
        return []
    try:
        with open(REPOS_FILE, 'r', encoding='utf-8') as f:
            raw = f.read().strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, str):
            data = [data]
        return [str(x) for x in data]
    except (ValueError, OSError):
        return []


def save_repos(lst):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REPOS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(lst), f, ensure_ascii=False, indent=2)


def add_repo(path):
    r = G(path, ['rev-parse', '--show-toplevel'], fail=True)
    top = os.path.normpath(r.out.strip())
    lst = [x for x in load_repos() if x != top]
    save_repos([top] + lst)
    return top


def to_web_url(u):
    if not u:
        return None
    u = u.strip()
    m = re.match(r'^git@([^:]+):(.+?)(\.git)?$', u)
    if m:
        return 'https://%s/%s' % (m.group(1), m.group(2))
    m = re.match(r'^ssh://git@([^/]+)/(.+?)(\.git)?$', u)
    if m:
        return 'https://%s/%s' % (m.group(1), m.group(2))
    m = re.match(r'^https?://(?:[^@/]+@)?([^/]+)/(.+?)(\.git)?/?$', u)
    if m:
        return 'https://%s/%s' % (m.group(1), m.group(2))
    return None


def cut(s, mx=600000):
    return s[:mx] + '\n... (output truncated)' if len(s) > mx else s


def docs_dir():
    d = os.path.expanduser('~/Documents')
    return d if os.path.isdir(d) else os.path.expanduser('~')


def first_line(s):
    for line in s.splitlines():
        if line.strip():
            return line.strip()
    return None


# ---------- desktop helpers ----------
def spawn(cmd, cwd=None):
    subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def opener():
    """The "open this file/folder/URL with the default app" command of the platform."""
    return shutil.which('open' if IS_MAC else 'xdg-open')


def open_url(url):
    op = opener()
    if op:
        spawn([op, url])
    else:
        webbrowser.open(url)


def open_terminal(cwd):
    if IS_MAC:
        spawn(['open', '-a', 'Terminal', cwd])
        return
    candidates = [
        ('x-terminal-emulator', []),
        ('gnome-terminal', ['--working-directory=' + cwd]),
        ('konsole', ['--workdir', cwd]),
        ('xfce4-terminal', ['--working-directory=' + cwd]),
        ('mate-terminal', ['--working-directory=' + cwd]),
        ('tilix', ['--working-directory=' + cwd]),
        ('alacritty', ['--working-directory', cwd]),
        ('kitty', ['--directory', cwd]),
        ('xterm', []),
    ]
    for exe, args in candidates:
        p = shutil.which(exe)
        if p:
            spawn([p] + args, cwd=cwd)
            return
    raise ApiError('No terminal emulator found.')


def pick_folder(start):
    start = start if start and os.path.isdir(start) else os.path.expanduser('~')
    if IS_MAC:
        script = ('on run argv\nPOSIX path of (choose folder with prompt "GitHub Helper" default location (POSIX file (item 1 of argv)))\nend run')
        r = subprocess.run(['osascript', '-e', script, start], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        path = r.stdout.decode('utf-8', 'replace').strip() if r.returncode == 0 else ''
        return (path.rstrip('/') or '/') if path else None
    zen = shutil.which('zenity')
    if zen:
        r = subprocess.run([zen, '--file-selection', '--directory', '--filename=' + start.rstrip('/') + '/'],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return r.stdout.decode('utf-8', 'replace').strip() or None if r.returncode == 0 else None
    kd = shutil.which('kdialog')
    if kd:
        r = subprocess.run([kd, '--getexistingdirectory', start], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return r.stdout.decode('utf-8', 'replace').strip() or None if r.returncode == 0 else None
    code = ('import sys, tkinter\nfrom tkinter import filedialog\nr = tkinter.Tk(); r.withdraw()\n'
            'sys.stdout.write(filedialog.askdirectory(initialdir=sys.argv[1]) or "")')
    r = subprocess.run([sys.executable, '-c', code, start], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        raise ApiError('No folder picker found (install zenity or kdialog) - type the path instead.')
    return r.stdout.decode('utf-8', 'replace').strip() or None


# ---------- gh login (device flow) ----------
def gh_login():
    gh = get_gh()
    if not gh:
        raise ApiError('GitHub CLI (gh) is not installed.')
    gh_login_cancel()
    env = dict(os.environ)
    env['GH_PROMPT_DISABLED'] = '1'
    env['GH_NO_UPDATE_NOTIFIER'] = '1'
    p = subprocess.Popen([gh, 'auth', 'login', '--hostname', 'github.com', '--git-protocol', 'https', '--web', '--skip-ssh-key'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    err_lines, out_chunks = [], []
    found = queue.Queue()

    def read_err():
        for raw in iter(p.stderr.readline, b''):
            line = raw.decode('utf-8', 'replace')
            err_lines.append(line)
            m = re.search(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', line)
            if m:
                found.put(m.group(1))

    def read_out():
        out_chunks.append(p.stdout.read())

    threading.Thread(target=read_err, daemon=True).start()
    threading.Thread(target=read_out, daemon=True).start()
    LOGIN['proc'], LOGIN['err'] = p, err_lines
    code = None
    deadline = time.time() + 25
    while code is None and time.time() < deadline:
        try:
            code = found.get(timeout=0.3)
        except queue.Empty:
            if p.poll() is not None:
                try:
                    code = found.get(timeout=0.5)
                except queue.Empty:
                    pass
                break
    if not code:
        try:
            if p.poll() is None:
                p.kill()
        except OSError:
            pass
        LOGIN['proc'] = None
        raise ApiError(''.join(err_lines).strip() or 'Could not start GitHub sign-in.')
    return {'code': code, 'url': 'https://github.com/login/device'}


def gh_login_cancel():
    p = LOGIN.get('proc')
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except OSError:
            pass
    LOGIN['proc'] = None


def gh_login_status():
    p = LOGIN.get('proc')
    if p is None:
        return {'state': 'idle'}
    if p.poll() is None:
        return {'state': 'waiting'}
    ok = p.returncode == 0
    err = ''.join(LOGIN['err']).strip()
    LOGIN['proc'] = None
    if ok:
        gh = get_gh()
        if gh:
            invoke_exe(gh, None, ['auth', 'setup-git', '--hostname', 'github.com'])
        CACHE['state'] = None
        return {'state': 'done'}
    return {'state': 'failed', 'error': err}


# ---------- macOS .app bundle ----------
def build_icns(png_bytes):
    """Minimal .icns holding one 512x512 PNG ('ic09'); macOS scales it for every other size."""
    import struct
    body = b'ic09' + struct.pack('>I', len(png_bytes) + 8) + png_bytes
    return b'icns' + struct.pack('>I', len(body) + 8) + body


def build_mac_app(dest_dir, standalone=False):
    """Create "GitHub Helper.app" in dest_dir.

    standalone=False: the bundle launches this very checkout (a git pull updates the app).
    standalone=True : the bundle carries its own copy of the program (for distributing a zip).
    """
    import shlex
    app_root = os.path.dirname(ROOT)
    app = os.path.join(dest_dir, 'GitHub Helper.app')
    macos, res = os.path.join(app, 'Contents', 'MacOS'), os.path.join(app, 'Contents', 'Resources')
    shutil.rmtree(app, ignore_errors=True)
    os.makedirs(macos)
    os.makedirs(res)
    if standalone:
        inner = os.path.join(res, 'app')
        for rel in ('VERSION', 'ui', 'assets/icon.png', 'assets/icon.svg', 'linux/server.py'):
            src, dst = os.path.join(app_root, *rel.split('/')), os.path.join(inner, *rel.split('/'))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        server = '"$(cd "$(dirname "$0")/../Resources/app/linux" && pwd)/server.py"'
    else:
        server = shlex.quote(os.path.join(ROOT, 'server.py'))
    launcher = os.path.join(macos, 'GitHubHelper')
    with open(launcher, 'w', encoding='utf-8') as f:
        f.write('#!/bin/bash\n'
                'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"\n'
                'exec /usr/bin/env python3 %s --no-shortcut "$@"\n' % server)
    os.chmod(launcher, 0o755)
    version = '1.0.0'
    try:
        with open(os.path.join(app_root, 'VERSION'), 'r', encoding='utf-8') as f:
            version = f.read().strip() or version
    except OSError:
        pass
    with open(os.path.join(app, 'Contents', 'Info.plist'), 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '<key>CFBundleName</key><string>GitHub Helper</string>\n'
                '<key>CFBundleDisplayName</key><string>GitHub Helper</string>\n'
                '<key>CFBundleIdentifier</key><string>com.binzigames.githubhelper</string>\n'
                '<key>CFBundleExecutable</key><string>GitHubHelper</string>\n'
                '<key>CFBundleIconFile</key><string>icon</string>\n'
                '<key>CFBundlePackageType</key><string>APPL</string>\n'
                '<key>CFBundleShortVersionString</key><string>%s</string>\n'
                '<key>CFBundleVersion</key><string>%s</string>\n'
                '<key>LSMinimumSystemVersion</key><string>10.13</string>\n'
                '<key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>\n'
                '<key>NSHighResolutionCapable</key><true/>\n'
                '</dict></plist>\n' % (version, version))
    try:
        with open(os.path.join(app_root, 'assets', 'icon.png'), 'rb') as f:
            icns = build_icns(f.read())
        with open(os.path.join(res, 'icon.icns'), 'wb') as f:
            f.write(icns)
    except OSError:
        pass
    return app


def ensure_mac_app():
    """Put "GitHub Helper.app" into ~/Applications (Launchpad / Spotlight) pointing at this checkout."""
    dest = os.path.expanduser('~/Applications')
    try:
        launcher = os.path.join(dest, 'GitHub Helper.app', 'Contents', 'MacOS', 'GitHubHelper')
        if os.path.exists(launcher):
            with open(launcher, 'r', encoding='utf-8') as f:
                if os.path.join(ROOT, 'server.py') in f.read():
                    return
        os.makedirs(dest, exist_ok=True)
        build_mac_app(dest)
    except OSError:
        pass


# ---------- application menu entry ----------
def ensure_desktop_entry():
    """Register "GitHub Helper" (with its icon) in the application menu: ~/.local/share/applications/githubhelper.desktop."""
    if IS_MAC:
        return ensure_mac_app()
    app_root = os.path.dirname(ROOT)
    icon = os.path.join(app_root, 'assets', 'icon.png')
    launcher = os.path.join(ROOT, 'start.sh')
    data_home = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    target = os.path.join(data_home, 'applications', 'githubhelper.desktop')
    content = (
        '[Desktop Entry]\n'
        'Type=Application\n'
        'Name=GitHub Helper\n'
        'GenericName=Git client\n'
        'Comment=Commits, branches and GitHub sync without the command line\n'
        'Exec="%s"\n'
        'Icon=%s\n'
        'Terminal=false\n'
        'Categories=Development;RevisionControl;\n'
        'Keywords=git;github;commit;branch;\n'
    ) % (launcher, icon)
    try:
        if os.path.exists(target):
            with open(target, 'r', encoding='utf-8') as f:
                if f.read() == content:
                    return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as f:
            f.write(content)
        os.chmod(target, 0o755)
    except OSError:
        pass


# ---------- auto-update ----------
APP_ROOT = os.path.dirname(ROOT)
UPDATE_REPO = 'Binzigames/GitHubHelper'   # where new versions are published (branch main, file VERSION)
OPTIONS = {'no_update': False}


def local_version():
    try:
        with open(os.path.join(APP_ROOT, 'VERSION'), 'r', encoding='utf-8') as f:
            return f.read().strip() or '0.0.0'
    except OSError:
        return '0.0.0'


def parse_ver(s):
    if not re.match(r'^\d+(\.\d+){0,3}$', s or ''):
        return None
    p = [int(x) for x in s.split('.')]
    return tuple(p + [0] * (4 - len(p)))


def check_update():
    """Compare the local VERSION with the one on GitHub. Never raises: offline just means "no update"."""
    cur = local_version()
    res = {'current': cur, 'latest': cur, 'available': False}
    if OPTIONS['no_update']:
        return res
    try:
        req = urllib.request.Request('https://raw.githubusercontent.com/%s/main/VERSION' % UPDATE_REPO,
                                     headers={'User-Agent': 'GitHubHelper', 'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=6) as r:
            latest = r.read(64).decode('utf-8', 'replace').strip()
        a, b = parse_ver(cur), parse_ver(latest)
        if a and b:
            res['latest'] = latest
            res['available'] = b > a
    except Exception:  # noqa: BLE001
        pass
    # A git checkout with local changes is never auto-updated: report it as "manual" instead of failing later.
    if res['available'] and os.path.exists(os.path.join(APP_ROOT, '.git')):
        try:
            dirty = bool(G(APP_ROOT, ['status', '--porcelain'], fail=True).out.strip())
        except Exception:  # noqa: BLE001
            dirty = True
        if dirty:
            res['available'] = False
            res['manual'] = True
    return res


def install_update():
    """Git checkouts get a fast-forward pull (local changes are never touched); plain downloads get the new zip."""
    if OPTIONS['no_update']:
        raise ApiError('Updates are disabled.')
    before = parse_ver(local_version())
    if os.path.exists(os.path.join(APP_ROOT, '.git')):
        if G(APP_ROOT, ['status', '--porcelain'], fail=True).out.strip():
            raise ApiError('This copy is a git checkout with local changes - update it manually with "git pull".')
        G(APP_ROOT, ['pull', '--ff-only', '--progress'], fail=True, live='update')
    else:
        import zipfile
        tmp = tempfile.mkdtemp(prefix='ghh-up-')
        prog_start('update')
        try:
            archive = os.path.join(tmp, 'update.zip')
            download('https://github.com/%s/archive/refs/heads/main.zip' % UPDATE_REPO, archive)
            PROG['op'], PROG['what'], PROG['pct'] = 'extract', 'update', 0
            with zipfile.ZipFile(archive) as z:
                files = {}
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    parts = info.filename.replace('\\', '/').split('/')[1:]   # drop the "GitHubHelper-main/" prefix
                    if not parts or any(p in ('', '..') for p in parts) or parts[0] == '.git':
                        continue
                    files['/'.join(parts)] = info
                for need in ('VERSION', 'ui/index.html', 'windows/server.ps1', 'linux/server.py'):
                    if need not in files:
                        raise ApiError('Downloaded update is incomplete.')
                for n, rel in enumerate(sorted(files, key=lambda x: x == 'VERSION')):   # VERSION last, so a half-finished copy is retried
                    PROG['pct'] = int(100 * n / max(1, len(files)))
                    dest = os.path.join(APP_ROOT, *rel.split('/'))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(files[rel]) as src, open(dest + '.new', 'wb') as out:
                        shutil.copyfileobj(src, out)
                    os.replace(dest + '.new', dest)
            for rel in ('start.cmd', 'linux/start.sh', 'linux/server.py', 'mac/start.command', 'mac/build-app.sh'):
                try:
                    os.chmod(os.path.join(APP_ROOT, *rel.split('/')), 0o755)
                except OSError:
                    pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            prog_end()
    after = parse_ver(local_version())
    if not after or (before and after <= before):
        raise ApiError('The update did not change the version.')
    State.restart = True


# ---------- API ----------
def api(name, b):
    repo = str(b.get('repo') or '')

    # Only operate on repositories the user has added in the app.
    exempt = ('state', 'pickfolder', 'install', 'ghlogin', 'ghloginStatus', 'ghloginCancel', 'ghlogout',
              'setIdentity', 'addRepo', 'removeRepo', 'init', 'clone', 'updateCheck', 'updateInstall', 'ping', 'progress', 'ensureTools')
    if name not in exempt and not (name == 'open' and str(b.get('what')) == 'url'):
        if repo not in load_repos():
            raise ApiError('Unknown repository.')
    if name in ('switch', 'createBranch', 'deleteBranch', 'merge', 'publish'):
        check_ref(b.get('name'))
    if name in ('show', 'showDiff'):
        check_ref(b.get('sha'))
    if name in ('addRemote', 'clone'):
        check_url(b.get('url'))

    if name == 'ping':   # cheap heartbeat; the window calls it every few seconds
        return {'ok': True}
    if name == 'progress':   # live state of the running download / git operation
        return dict(PROG)

    if name == 'ensureTools':
        # Called by the window right after the title screen: fetches a private gh when the machine has none.
        # (git cannot be bundled on Linux / macOS: the banner offers the system package manager for it.)
        res = {'ok': True, 'installed': [], 'error': None}
        if not test_dep('gh'):
            try:
                if install_dep('gh'):
                    res['installed'].append('gh')
            except Exception as e:  # noqa: BLE001
                res['ok'], res['error'] = False, mask(str(e))
        CACHE['state'] = None
        return res

    if name == 'state':
        # git/gh/identity info needs several processes (and a network call for `gh api user`): cache it for STATE_TTL.
        si = CACHE['state']
        if si is None or time.time() - CACHE['state_at'] > STATE_TTL:
            gh = get_gh()
            authed, gh_user = False, ''
            if gh:
                authed = invoke_exe(gh, None, ['auth', 'status', '--hostname', 'github.com']).code == 0
                if authed:
                    gh_user = invoke_exe(gh, None, ['api', 'user', '--jq', '.login']).out.strip()
            uname, email = '', ''
            if get_git():
                uname = G(None, ['config', '--global', 'user.name']).out.strip()
                email = G(None, ['config', '--global', 'user.email']).out.strip()
            si = {'git': bool(get_git()), 'gh': bool(gh), 'ghAuthed': authed, 'ghUser': gh_user,
                  'userName': uname, 'userEmail': email}
            CACHE['state'], CACHE['state_at'] = si, time.time()
        lst = [r for r in load_repos() if os.path.exists(r)]
        out = dict(si)
        out.update({'repos': lst, 'docs': docs_dir(), 'sep': '/', 'os': 'mac' if IS_MAC else 'linux'})
        return out

    if name == 'updateCheck':
        return check_update()
    if name == 'updateInstall':
        install_update()
        return {'ok': True}

    if name == 'pickfolder':
        return {'path': pick_folder(str(b.get('start') or ''))}

    if name == 'install':
        what = 'gh' if b.get('what') == 'gh' else 'git'
        if not install_dep(what):
            raise ApiError('Could not install %s automatically. Install it manually and restart the app.' % what)
        return {'ok': True}

    if name == 'ghlogin':
        return gh_login()
    if name == 'ghloginStatus':
        return gh_login_status()
    if name == 'ghloginCancel':
        gh_login_cancel()
        return {'ok': True}

    if name in ('ghlogout', 'setIdentity'):
        CACHE['state'] = None
    if name in ('addRemote', 'publish'):
        CACHE['remote'].clear()
    if name == 'ghlogout':
        gh = get_gh()
        if not gh:
            raise ApiError('GitHub CLI (gh) is not installed.')
        r = invoke_exe(gh, None, ['auth', 'logout', '--hostname', 'github.com'])
        if r.code != 0:
            raise ApiError((r.err + r.out).strip())
        return {'ok': True}

    if name == 'setIdentity':
        G(None, ['config', '--global', 'user.name', str(b.get('name') or '')], fail=True)
        G(None, ['config', '--global', 'user.email', str(b.get('email') or '')], fail=True)
        return {'ok': True}

    if name == 'addRepo':
        repo = os.path.expanduser(repo)
        require_repo(repo)
        return {'path': add_repo(repo)}

    if name == 'removeRepo':
        save_repos([r for r in load_repos() if r != repo])
        return {'ok': True}

    if name == 'init':
        repo = os.path.expanduser(repo)
        if not repo:
            raise ApiError('Enter a folder.')
        os.makedirs(repo, exist_ok=True)
        G(repo, ['init', '-b', 'main'], fail=True)
        return {'path': add_repo(repo)}

    if name == 'clone':
        url = str(b.get('url') or '').strip()
        if not url:
            raise ApiError('Enter a repository URL.')
        short = url
        if not re.match(r'^(https?://|git@|ssh://|/)', short):
            short = 'https://github.com/' + url   # "owner/repo" shorthand
            url = short
        leaf = re.sub(r'\.git$', '', re.split(r'[/\\:]', short.rstrip('/'))[-1])
        if not leaf:
            raise ApiError('Could not determine repository name from URL.')
        parent = os.path.expanduser(str(b.get('parent') or '').strip())
        if not parent:
            raise ApiError('Choose a destination folder.')
        os.makedirs(parent, exist_ok=True)
        dest = os.path.join(parent, leaf)
        if os.path.exists(dest):
            raise ApiError('Folder already exists: %s' % dest)
        G(parent, ['clone', '--progress', url, dest], fail=True, live='clone')
        return {'path': add_repo(dest)}

    if name == 'status':
        require_repo(repo)
        # One git call gives branch, upstream, ahead/behind, HEAD and every changed file (porcelain v2).
        r = G(repo, ['status', '--porcelain=v2', '--branch', '-z', '--untracked-files=all'], fail=True)
        parts = r.out.split('\0')
        i, files = 0, []
        oid, branch, upstream, ahead, behind = '', '(detached HEAD)', None, 0, 0
        while i < len(parts):
            e = parts[i]
            i += 1
            if len(e) < 3:
                continue
            if e[0] == '#':
                if e.startswith('# branch.oid '):
                    oid = e[13:]
                elif e.startswith('# branch.head '):
                    if e[14:] != '(detached)':
                        branch = e[14:]
                elif e.startswith('# branch.upstream '):
                    upstream = e[18:]
                elif e.startswith('# branch.ab '):
                    m = re.match(r'# branch\.ab \+(\d+) -(\d+)', e)
                    if m:
                        ahead, behind = int(m.group(1)), int(m.group(2))
                continue
            kind, xy, path, orig = e[0], None, None, None
            if kind == '1':
                f = e.split(' ', 8)
                xy, path = f[1], f[8]
            elif kind == '2':
                f = e.split(' ', 9)
                xy, path = f[1], f[9]
                orig = parts[i] if i < len(parts) else None
                i += 1
            elif kind == 'u':
                f = e.split(' ', 10)
                xy, path = f[1], f[10]
            elif kind == '?':
                xy, path = '??', e[2:]
            if not path:
                continue
            m = None
            if len(files) < 500:  # mtime+size lets the UI notice edits to files that were already modified
                try:
                    stt = os.stat(os.path.join(repo, path))
                    m = '%d-%d' % (stt.st_mtime_ns // 1000, stt.st_size)
                except OSError:
                    pass
            files.append({'path': path, 'x': xy[0].replace('.', ' '), 'y': xy[1].replace('.', ' '), 'orig': orig, 'm': m})
        has = bool(oid) and oid != '(initial)'
        head = oid if has else ''

        # Remote URL and last commit subject change rarely: cache them instead of spawning git on every poll.
        rc = CACHE['remote'].get(repo)
        if rc is None or time.time() - rc[0] > 20:
            remote = first_line(G(repo, ['remote']).out)
            rurl = G(repo, ['remote', 'get-url', remote]).out.strip() if remote else None
            rc = (time.time(), remote, rurl)
            CACHE['remote'][repo] = rc
        sc = CACHE['subject'].get(repo)
        if sc is None or sc[0] != head:
            sc = (head, G(repo, ['log', '-1', '--format=%s']).out.strip() if has else None)
            CACHE['subject'][repo] = sc
        gd = CACHE['gitdir'].get(repo)
        if gd is None:
            gd = G(repo, ['rev-parse', '--absolute-git-dir']).out.strip()
            CACHE['gitdir'][repo] = gd
        merging = bool(gd) and os.path.exists(os.path.join(gd, 'MERGE_HEAD'))
        return {'branch': branch, 'head': head, 'hasCommits': has, 'upstream': upstream, 'ahead': ahead, 'behind': behind,
                'remote': rc[1], 'remoteUrl': mask(rc[2]), 'webUrl': to_web_url(rc[2]), 'lastSubject': sc[1],
                'merging': merging, 'files': files}

    if name == 'stage':
        G(repo, ['add', '--'] + [str(p) for p in (b.get('paths') or [])], fail=True)
        return {'ok': True}

    if name == 'unstage':
        p = [str(x) for x in (b.get('paths') or [])]
        if has_head(repo):
            G(repo, ['restore', '--staged', '--'] + p, fail=True)
        else:
            G(repo, ['rm', '--cached', '-r', '-q', '--'] + p, fail=True)
        return {'ok': True}

    if name == 'stageAll':
        G(repo, ['add', '-A'], fail=True)
        return {'ok': True}

    if name == 'unstageAll':
        if has_head(repo):
            G(repo, ['reset', '-q'], fail=True)
        else:
            G(repo, ['rm', '--cached', '-r', '-q', '.'], fail=True)
        return {'ok': True}

    if name == 'discard':
        head = has_head(repo)
        for f in (b.get('files') or []):
            p = str(f.get('path'))
            if f.get('untracked'):
                G(repo, ['clean', '-fd', '--', p], fail=True)
            elif head:
                G(repo, ['restore', '--source=HEAD', '--staged', '--worktree', '--', p], fail=True)
            else:
                G(repo, ['rm', '-f', '--cached', '--', p])
                full = os.path.realpath(os.path.join(repo, p))
                if full.startswith(os.path.realpath(repo) + os.sep) and os.path.isfile(full):
                    os.remove(full)
        return {'ok': True}

    if name == 'diff':
        path = str(b.get('path'))
        if b.get('untracked'):
            r = G(repo, ['diff', '--no-index', '--no-color', '--', '/dev/null', path])
        elif has_head(repo):
            r = G(repo, ['diff', 'HEAD', '--no-color', '-M', '--', path])
        else:
            r = G(repo, ['diff', '--cached', '--no-color', '--', path])
        return {'diff': cut(r.out)}

    if name == 'commit':
        msg = str(b.get('summary') or '').strip()
        if not msg:
            raise ApiError('Enter a commit summary.')
        desc = str(b.get('description') or '').strip()
        if desc:
            msg += '\n\n' + desc
        a = ['commit', '-F', '-']
        if b.get('amend'):
            a.append('--amend')
        r = G(repo, a, msg, fail=True)
        return {'ok': True, 'out': r.out}

    if name == 'undoCommit':
        G(repo, ['reset', '--soft', 'HEAD~1'], fail=True)
        return {'ok': True}

    if name == 'log':
        skip = int(b.get('skip') or 0)
        r = G(repo, ['log', '-n', '200', '--skip=%d' % skip, '--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%D'])
        unp = []
        if upstream_of(repo):
            unp = [x for x in G(repo, ['rev-list', '@{u}..HEAD']).out.splitlines() if x]
        commits = []
        for line in r.out.splitlines():
            if not line:
                continue
            f = line.split('\x1f')
            if len(f) < 5:
                continue
            commits.append({'sha': f[0], 'short': f[1], 'author': f[2], 'date': f[3], 'subject': f[4],
                            'refs': f[5] if len(f) > 5 else ''})
        return {'commits': commits, 'unpushed': unp}

    if name == 'show':
        sha = str(b.get('sha'))
        m = G(repo, ['show', '-s', '--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%B', sha], fail=True).out
        f = m.split('\x1f', 4)
        ns = G(repo, ['show', '-m', '--first-parent', '--name-status', '--format=', '-M', sha], fail=True).out
        files = []
        for line in ns.splitlines():
            if not line:
                continue
            t = line.split('\t')
            files.append({'status': t[0][:1], 'path': t[-1]})
        return {'sha': f[0], 'author': f[1], 'email': f[2], 'date': f[3], 'message': f[4].strip(), 'files': files}

    if name == 'showDiff':
        r = G(repo, ['show', '-m', '--first-parent', '--no-color', '--format=', '-M', str(b.get('sha')), '--', str(b.get('path'))])
        return {'diff': cut(r.out)}

    if name == 'branches':
        r = G(repo, ['for-each-ref', '--format=%(refname:short)%09%(HEAD)%09%(upstream:short)%09%(refname)',
                     'refs/heads', 'refs/remotes'], fail=True)
        local, remote = [], []
        for line in r.out.splitlines():
            if not line:
                continue
            t = line.split('\t')
            if len(t) < 4:
                continue
            if t[3].startswith('refs/heads/'):
                local.append({'name': t[0], 'current': t[1] == '*', 'upstream': t[2]})
            elif not t[3].endswith('/HEAD'):
                remote.append({'name': t[0]})
        names = {x['name'] for x in local}
        remote = [x for x in remote if re.sub(r'^[^/]+/', '', x['name']) not in names]
        return {'local': local, 'remote': remote}

    if name == 'switch':
        G(repo, ['switch', str(b.get('name'))], fail=True)
        return {'ok': True}
    if name == 'createBranch':
        G(repo, ['switch', '-c', str(b.get('name'))], fail=True)
        return {'ok': True}
    if name == 'deleteBranch':
        G(repo, ['branch', '-D' if b.get('force') else '-d', str(b.get('name'))], fail=True)
        return {'ok': True}
    if name == 'merge':
        r = G(repo, ['merge', '--no-edit', str(b.get('name'))], fail=True)
        return {'ok': True, 'out': r.out}
    if name == 'abortMerge':
        G(repo, ['merge', '--abort'], fail=True)
        return {'ok': True}

    if name == 'stash':
        r = G(repo, ['stash', 'push', '-u', '-m', 'GitHub Helper stash'], fail=True)
        return {'ok': True, 'out': r.out}
    if name == 'stashPop':
        r = G(repo, ['stash', 'pop'], fail=True)
        return {'ok': True, 'out': r.out}

    if name == 'fetch':
        r = G(repo, ['fetch', '--all', '--prune', '--progress'], fail=True, live='' if b.get('quiet') else 'fetch')
        return {'ok': True, 'out': r.out + r.err}
    if name == 'pull':
        r = G(repo, ['pull', '--no-edit', '--progress'], fail=True, live='pull')
        return {'ok': True, 'out': r.out + r.err}
    if name == 'push':
        if upstream_of(repo):
            r = G(repo, ['push', '--progress'], fail=True, live='push')
        else:
            remote = first_line(G(repo, ['remote']).out)
            if not remote:
                raise ApiError('No remote configured. Use "Publish repository" first.')
            r = G(repo, ['push', '--progress', '-u', remote, 'HEAD'], fail=True, live='push')
        return {'ok': True, 'out': r.out + r.err}

    if name == 'addRemote':
        G(repo, ['remote', 'add', 'origin', str(b.get('url'))], fail=True)
        return {'ok': True}

    if name == 'publish':
        gh = get_gh()
        if not gh:
            raise ApiError('GitHub CLI (gh) is needed to create repositories. Install it from the menu.')
        vis = '--private' if b.get('private') else '--public'
        r = invoke_live(gh, repo, ['repo', 'create', str(b.get('name')), vis, '--source', '.', '--remote', 'origin', '--push'], 'publish')
        if r.code != 0:
            raise ApiError((r.err + r.out).strip())
        return {'ok': True, 'out': r.out + r.err}

    if name == 'open':
        what = str(b.get('what'))
        if what == 'explorer':
            op = opener()
            if not op:
                raise ApiError('%s was not found.' % ('open' if IS_MAC else 'xdg-open'))
            spawn([op, repo])
        elif what == 'terminal':
            open_terminal(repo)
        elif what == 'code':
            code = find_exe('code') or shutil.which('codium')
            if code:
                spawn([code, repo])
            elif IS_MAC and os.path.isdir('/Applications/Visual Studio Code.app'):
                spawn(['open', '-a', 'Visual Studio Code', repo])
            else:
                raise ApiError('VS Code ("code") was not found in PATH.')
        elif what == 'url':
            u = str(b.get('url') or '')
            if not re.match(r'^https://[A-Za-z0-9.-]+(:\d+)?(/[^\s"<>^|&`]*)?$', u):
                raise ApiError('Refusing to open this URL')
            open_url(u)
        else:
            raise ApiError('Unknown open target')
        return {'ok': True}

    raise ApiError('Unknown action: %s' % name)


# ---------- HTTP server ----------
class State:
    token = uuid.uuid4().hex
    port = 0
    last_request = time.time()
    seen = False
    bye_at = None
    restart = False


SEC_HEADERS = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Cross-Origin-Resource-Policy': 'same-origin',
    'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; "
                              "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


class Handler(BaseHTTPRequestHandler):
    server_version = 'GitHubHelper'

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def send_bytes(self, code, data, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj):
        self.send_bytes(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

    def host_ok(self):
        return self.headers.get('Host') in ('127.0.0.1:%d' % State.port, 'localhost:%d' % State.port)

    def do_GET(self):
        if not self.host_ok():
            return self.send_bytes(403, b'bad host', 'text/plain')
        if self.path.split('?', 1)[0] == '/':
            State.seen = True
            State.bye_at = None
            State.last_request = time.time()
            with open(UI_FILE, 'r', encoding='utf-8') as f:
                html = f.read().replace('__TOKEN__', State.token).replace('__VERSION__', local_version())
            return self.send_bytes(200, html.encode('utf-8'), 'text/html; charset=utf-8')
        self.send_bytes(404, b'not found', 'text/plain')

    def do_POST(self):
        if not self.host_ok():
            return self.send_bytes(403, b'bad host', 'text/plain')
        path = self.path.split('?', 1)[0]
        if not path.startswith('/api/'):
            return self.send_bytes(404, b'not found', 'text/plain')
        if self.headers.get('X-Token') != State.token:
            return self.send_json(403, {'error': 'bad token'})
        origin = self.headers.get('Origin')
        if origin and origin not in ('http://127.0.0.1:%d' % State.port, 'http://localhost:%d' % State.port):
            return self.send_json(403, {'error': 'bad origin'})
        length = int(self.headers.get('Content-Length') or 0)
        if length > 5 * 1024 * 1024:
            return self.send_json(413, {'error': 'too large'})
        State.last_request = time.time()
        raw = self.rfile.read(length) if length else b''
        if path == '/api/bye':
            State.bye_at = time.time()
            return self.send_json(200, {})
        State.bye_at = None
        try:
            body = json.loads(raw.decode('utf-8')) if raw.strip() else {}
            if not isinstance(body, dict):
                body = {}
            name = path[5:]
            if name in ('progress', 'ping'):   # never wait behind a long operation: they report on it
                res = api(name, body)
            else:
                with API_LOCK:
                    res = api(name, body)
            self.send_json(200, res)
        except ApiError as e:
            self.send_json(400, {'error': mask(str(e))})
        except Exception as e:  # noqa: BLE001
            self.send_json(400, {'error': mask(str(e))})


MAC_BROWSERS = (
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
)


def launch_window(url):
    if IS_MAC:
        # Chromium-based browsers can open a chrome-less "app" window; otherwise use the default browser.
        for p in MAC_BROWSERS:
            if os.path.exists(p):
                spawn([p, '--app=' + url, '--window-size=1300,820'])
                return
        open_url(url)
        return
    for exe in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge',
                'microsoft-edge-stable', 'brave-browser'):
        p = shutil.which(exe)
        if p:
            spawn([p, '--app=' + url, '--window-size=1300,820'])
            return
    open_url(url)


def main():
    ap = argparse.ArgumentParser(description='GitHub Helper (Linux / macOS)')
    ap.add_argument('--build-app', nargs='?', const='dist', metavar='DIR', help='macOS: build a self-contained "GitHub Helper.app" into DIR (default: dist) and exit')
    ap.add_argument('--port', type=int, default=47321, help='port for the local server (default 47321)')
    ap.add_argument('--no-browser', action='store_true', help='do not open a window automatically')
    ap.add_argument('--no-autoexit', action='store_true', help='keep running after the window is closed')
    ap.add_argument('--foreground', action='store_true', help='stay attached to the terminal (default: detach once the window is open)')
    ap.add_argument('--no-update', action='store_true', help='do not check GitHub for a newer version')
    ap.add_argument('--no-shortcut', action='store_true', help='do not add GitHub Helper to the application menu')
    ap.add_argument('--repo', default='', help='open the UI straight on this folder (used by the Unity package)')
    ap.add_argument('--restarted', action='store_true', help=argparse.SUPPRESS)   # set by the updater
    args = ap.parse_args()
    if args.build_app:
        os.makedirs(args.build_app, exist_ok=True)
        print('Built ' + build_mac_app(os.path.abspath(args.build_app), standalone=True))
        return 0
    OPTIONS['no_update'] = args.no_update
    if not args.no_shortcut and not args.restarted:
        ensure_desktop_entry()

    # After an update restart we must come back on the very same port (the open window talks to it), so keep retrying it.
    httpd = None
    attempts = 40 if args.restarted else 1
    ports = [args.port] if args.restarted else list(range(args.port, args.port + 21))
    for attempt in range(attempts):
        for p in ports:
            try:
                httpd = ThreadingHTTPServer(('127.0.0.1', p), Handler)
                State.port = p
                break
            except OSError:
                continue
        if httpd is not None:
            break
        time.sleep(0.5)
    if httpd is None:
        print('Could not open a local port.', file=sys.stderr)
        return 1
    httpd.daemon_threads = True

    url = 'http://127.0.0.1:%d/' % State.port
    print('GitHub Helper is running at ' + url)
    print('Close the window (or press Ctrl+C) to quit.')
    if not get_git():
        print('Git was not found - the app will offer to install it.')

    started = time.time()

    def watchdog():
        while True:
            time.sleep(1)
            now = time.time()
            if State.restart:
                break
            if args.restarted and not State.seen and now - started > 90:
                print('No window connected after the update - exiting.')
                break
            if args.no_autoexit:
                continue
            if State.seen and now - State.last_request > 30:
                print('Window closed (no heartbeat for 30 s) - exiting.')
                break
            if State.bye_at and now - State.bye_at > 2:
                print('Window closed - exiting.')
                break
        httpd.shutdown()

    if not args.no_browser and not args.restarted:
        if args.repo:
            from urllib.parse import quote
            url += '?repo=' + quote(args.repo, safe='')
        launch_window(url)

    # Once the window is up, detach from the terminal so it can be closed / reused (output goes to a log file).
    if not args.foreground and hasattr(os, 'fork'):
        print('Running in the background (log: %s). Close the app window to quit.' % os.path.join(DATA_DIR, 'server.log'))
        sys.stdout.flush()
        if os.fork() > 0:
            os._exit(0)
        os.setsid()
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            log_fd = os.open(os.path.join(DATA_DIR, 'server.log'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError:
            log_fd = os.open(os.devnull, os.O_WRONLY)
        null_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null_fd, 0)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        sys.stdout = os.fdopen(1, 'w', buffering=1)
        sys.stderr = os.fdopen(2, 'w', buffering=1)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gh_login_cancel()
        httpd.server_close()
    if State.restart:
        # Replace this process with the freshly updated copy (same port, same detached state, log file kept).
        argv = [sys.executable, os.path.abspath(__file__), '--port', str(State.port), '--no-browser', '--restarted', '--foreground']
        if args.no_update:
            argv.append('--no-update')
        sys.stdout.flush()
        os.execv(sys.executable, argv)
    return 0


if __name__ == '__main__':
    sys.exit(main())
