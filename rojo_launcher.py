#!/usr/bin/env python3
"""
Rojo Launcher: pick one of your GitHub repos, pull the latest, start Rojo.

The window is a small web page served from this script on 127.0.0.1, shown in a
native window (pywebview, used by the .exe build) or in an Edge/Chrome app window.
Only the Python standard library is required. Needs git and rojo.
Settings live in ~/.rojo-launcher.json
"""

import base64
import collections
import http.server
import json
import os
import io
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_VERSION = "3.2"
UPDATE_REPO = "ChocoBasta/RojoLauncher"   # where new versions of this app are published
CONFIG_PATH = Path.home() / ".rojo-launcher.json"
APP_HOME = Path.home() / ".rojo-launcher"
FROZEN = bool(getattr(sys, "frozen", False))   # running as the built app, not from the .py file
MAC_APP = FROZEN and sys.platform == "darwin"  # the Mac "Rojo Launcher.app"
DEFAULTS = {
    "github_user": "ChocoBasta",
    "workspace": str(Path.home() / "RobloxProjects"),
    "token": "",            # optional: lets the app list (and pull) your PRIVATE repos
    "port": 34872,
    "last_repo": "",
    "favorites": [],
    "repo_cache": [],       # last fetched repo list, so the window fills instantly
    "auto_start": False,    # pull + start Rojo for the last repo when the app opens
    "size": [1000, 660],
    "banner_dismissed": False,
    "move_banner_dismissed": False,  # Mac app: the "Move to Applications" note was closed
    "extra_repos": [],      # repos added with the + button (anyone's), shown as favorites
    "resume_rojo": "",      # restart Rojo for this repo after an app update
}


# ---------------------------------------------------------------- config

def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------- helpers

LOG_PATH = APP_HOME / "launcher.log"   # what the Activity drawer showed, kept after the app closes
_log_lock = threading.Lock()


def log_file(line):
    try:
        with _log_lock, open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s [%d] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), line))
    except OSError:
        pass


def start_log_file():
    """Keep a small log on disk (the last few runs), with any crash in it, for when something goes wrong."""
    try:
        APP_HOME.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
            LOG_PATH.write_bytes(LOG_PATH.read_bytes()[-128 * 1024:])
    except OSError:
        pass

    def crashed(kind, exc_type, exc, tb):
        import traceback
        log_file("!! %s crashed:\n%s" % (kind, "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()))
    sys.excepthook = lambda t, e, tb: crashed("the app", t, e, tb)
    threading.excepthook = lambda a: crashed("a background task", a.exc_type, a.exc_value, a.exc_traceback)
    log_file("── Rojo Launcher %s started from %s" % (APP_VERSION, sys.executable if FROZEN else __file__))

def _no_window():
    # Stop a console window flashing up for every command on Windows.
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _fix_path():
    # Apps started from Finder/Start menu get a short PATH; add the usual tool folders
    # so git, rojo (Aftman/Rokit/Foreman) and VS Code are still found.
    home = Path.home()
    extra = [home / ".aftman" / "bin", home / ".rokit" / "bin", home / ".foreman" / "bin",
             home / ".cargo" / "bin", home / ".local" / "bin", Path("/opt/homebrew/bin"),
             Path("/usr/local/bin"), Path("/opt/local/bin")]
    if sys.platform == "win32":
        la = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        extra += [Path("C:/Program Files/Git/cmd"), la / "Programs" / "Microsoft VS Code" / "bin"]
    if sys.platform == "darwin":
        extra.append(Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin"))
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for p in extra:
        if p.is_dir() and str(p) not in parts:
            parts.append(str(p))
    os.environ["PATH"] = os.pathsep.join(parts)


SHELL_PATH_READY = threading.Event()


def _merge_shell_path():
    """Mac: an app opened from Finder or the Dock doesn't see the PATH your Terminal has, so ask
    your login shell for it once. Then rojo and git are found wherever Terminal finds them."""
    try:
        if sys.platform != "darwin":
            return
        import pwd
        shell = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/zsh"
        mark = "__ROJO_LAUNCHER_PATH__"
        p = subprocess.run([shell, "-ilc", 'printf "\\n%s%%s%s\\n" "$PATH"' % (mark, mark)],
                           capture_output=True, timeout=6, stdin=subprocess.DEVNULL, **TEXT)
        m = re.search(re.escape(mark) + r"(.*?)" + re.escape(mark), p.stdout or "")
        if not m:
            return
        have = os.environ.get("PATH", "").split(os.pathsep)
        mine = [d for d in m.group(1).split(":") if d and os.path.isabs(d)]
        merged = list(dict.fromkeys(mine + have))  # your shell's order first, nothing lost
        os.environ["PATH"] = os.pathsep.join(merged)
    except Exception:
        pass
    finally:
        SHELL_PATH_READY.set()


QUIET_GIT = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_OPTIONAL_LOCKS": "0"}
TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def _env(extra=None, quiet=False):
    if not extra and not quiet:
        return None
    e = dict(os.environ)
    if quiet:
        e.update(QUIET_GIT)  # background checks must never pop up a sign-in window
    if extra:
        e.update(extra)
    return e


def git_out(args, cwd, env=None, quiet=True):
    """Run a quiet git command and return stdout ('' on failure)."""
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=60,
                           env=_env(env, quiet), **TEXT, **_no_window())
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def git_ok(args, cwd, env=None, quiet=True):
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=60,
                              env=_env(env, quiet), **_no_window()).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)


def parse_repo_spec(spec):
    """'owner/repo', 'repo' or any GitHub URL -> (owner or None, repo), or None."""
    s = spec.strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    for pre in ("https://github.com/", "http://github.com/", "https://www.github.com/", "github.com/",
                "git@github.com:"):
        if s.lower().startswith(pre):
            s = s[len(pre):]
            break
    parts = [p for p in s.split("/") if p]
    ok = lambda p: re.fullmatch(r"[A-Za-z0-9_.-]+", p) is not None  # noqa: E731
    if len(parts) >= 2 and ok(parts[0]) and ok(parts[1]):
        return parts[0], parts[1]
    if len(parts) == 1 and ok(parts[0]):
        return None, parts[0]
    return None


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    # GitHub release downloads redirect to a storage host that rejects our GitHub token.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


def _download(url, token="", accept="application/octet-stream", timeout=120):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "rojo-launcher"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.build_opener(_DropAuthOnRedirect).open(req, timeout=timeout) as r:
        return r.read()


def port_open(port):
    """True if something is already listening on 127.0.0.1:port."""
    try:
        socket.create_connection(("127.0.0.1", port), 0.3).close()
        return True
    except OSError:
        return False


def ago(ts):
    s = max(0, int(time.time() - ts))
    for unit, n in (("year", 31536000), ("month", 2592000), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= n:
            v = s // n
            return "%d %s%s ago" % (v, unit, "" if v == 1 else "s")
    return "just now"


def open_path(path):
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def rojo_version():
    rojo = shutil.which("rojo")
    if not rojo:
        return ""
    try:
        p = subprocess.run([rojo, "--version"], capture_output=True, timeout=15, **TEXT, **_no_window())
        return p.stdout.strip().replace("Rojo ", "").replace("rojo ", "") or "?"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "?"


# ---------------------------------------------------------------- install as an app

def icon_file(kind):
    """Write the embedded icon to ~/.rojo-launcher/icon.<kind> and return its path."""
    APP_HOME.mkdir(parents=True, exist_ok=True)
    f = APP_HOME / ("icon." + kind)
    data = base64.b64decode(ICON_ICO if kind == "ico" else ICON_PNG)
    try:
        if not f.exists() or f.read_bytes() != data:
            f.write_bytes(data)
    except OSError:
        pass
    return f


# ---- the Mac app

def mac_bundle():
    """The 'Rojo Launcher.app' folder this copy runs from (Mac app only), or None."""
    if not MAC_APP:
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def mac_app_folders():
    return [Path("/Applications"), Path.home() / "Applications"]


def mac_in_applications():
    b = mac_bundle()
    return b is not None and b.parent in mac_app_folders()


def mac_move_to_applications():
    """Copy this app into Applications (yours, if the main one can't be written) and return the
    new copy. It's opened from there afterwards, so it shows in Launchpad, Spotlight and the Dock."""
    src = mac_bundle()
    if src is None:
        raise RuntimeError("this isn't the Mac app")
    last = None
    for folder in mac_app_folders():
        dest = folder / src.name
        tmp = folder / (".%s.new" % src.name)
        try:
            folder.mkdir(exist_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)
            p = subprocess.run(["ditto", str(src), str(tmp)], capture_output=True, **TEXT)
            if p.returncode != 0:
                raise OSError(p.stderr.strip() or "copy failed")
            # You already said yes to opening it, so the copy shouldn't ask again.
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(tmp)], capture_output=True)
            if dest.exists() or dest.is_symlink():
                old = folder / (".%s.old" % src.name)
                shutil.rmtree(old, ignore_errors=True)
                os.rename(dest, old)
                os.rename(tmp, dest)
                shutil.rmtree(old, ignore_errors=True)
            else:
                os.rename(tmp, dest)
            return dest
        except OSError as e:
            last = e
            shutil.rmtree(tmp, ignore_errors=True)
    raise RuntimeError("couldn't copy it into Applications (%s)" % last)


def mac_remove_old_shortcut():
    """The Python version's 'Add to Desktop' made ~/Applications/Rojo Launcher.app, a tiny script
    that starts Python. The real app replaces it, so take it away (only if it is that script)."""
    if not MAC_APP:
        return
    old = Path.home() / "Applications" / "Rojo Launcher.app"
    try:
        if mac_bundle() == old.resolve():
            return
        exe = old / "Contents" / "MacOS" / "Rojo Launcher"
        with open(exe, "rb") as f:
            is_script = f.read(2) == b"#!"
        if is_script and exe.stat().st_size < 4096:
            shutil.rmtree(old)
            (APP_HOME / "installed").unlink(missing_ok=True)
    except OSError:
        pass


def is_installed():
    if MAC_APP:
        return mac_in_applications()  # running from Applications, not from Downloads or the .dmg
    try:
        return Path((APP_HOME / "installed").read_text().strip()).exists()
    except OSError:
        return False


def install_app():
    """Put a 'Rojo Launcher' shortcut on the Desktop and in the Start menu / Applications.
    Returns a short message saying where it went."""
    frozen = getattr(sys, "frozen", False)
    APP_HOME.mkdir(parents=True, exist_ok=True)
    if frozen:
        # Copy the exe to a stable place (it may be running from Downloads or inside a zip).
        exe = Path(sys.executable)
        home = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "Rojo Launcher"
        stable = home / exe.name
        if exe.resolve() != stable.resolve():
            home.mkdir(parents=True, exist_ok=True)
            shutil.copy2(exe, stable)
        target, args = stable, ""
    else:
        # Copy the script somewhere stable, so the shortcut keeps working if Downloads is cleaned up.
        script = APP_HOME / ("rojo_launcher.pyw" if sys.platform == "win32" else "rojo_launcher.py")
        script.write_bytes(Path(__file__).read_bytes())
        py = Path(sys.executable)
        if sys.platform == "win32":
            pyw = py.with_name("pythonw.exe")
            py = pyw if pyw.exists() else py
        target, args = py, '"%s"' % script

    if sys.platform == "win32":
        ico = icon_file("ico")
        q = lambda s: str(s).replace("'", "''")  # noqa: E731
        ps = "\n".join([
            "$sh = New-Object -ComObject WScript.Shell",
            "foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {",
            "  $s = $sh.CreateShortcut((Join-Path $d 'Rojo Launcher.lnk'))",
            "  $s.TargetPath = '%s'" % q(target),
            "  $s.Arguments = '%s'" % q(args),
            "  $s.WorkingDirectory = '%s'" % q(APP_HOME),
            "  $s.IconLocation = '%s'" % q(ico),
            "  $s.Description = 'Pull a repo and start Rojo'",
            "  $s.Save()",
            "}",
        ])
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                           capture_output=True, text=True, **_no_window())
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or "PowerShell failed")
        where = "on your Desktop and in the Start menu"
    elif sys.platform == "darwin":
        app = Path.home() / "Applications" / "Rojo Launcher.app"
        macos = app / "Contents" / "MacOS"
        res = app / "Contents" / "Resources"
        macos.mkdir(parents=True, exist_ok=True)
        res.mkdir(parents=True, exist_ok=True)
        exe = macos / "Rojo Launcher"
        exe.write_text("#!/bin/sh\nexec \"%s\" %s\n" % (target, args))
        exe.chmod(0o755)
        subprocess.run(["sips", "-s", "format", "icns", str(icon_file("png")), "--out",
                        str(res / "icon.icns")], capture_output=True)
        (app / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
            "<key>CFBundleName</key><string>Rojo Launcher</string>"
            "<key>CFBundleExecutable</key><string>Rojo Launcher</string>"
            "<key>CFBundleIdentifier</key><string>local.rojo-launcher</string>"
            "<key>CFBundleIconFile</key><string>icon</string>"
            "<key>CFBundlePackageType</key><string>APPL</string>"
            "</dict></plist>\n")
        subprocess.run(["touch", str(app)])
        where = "in Applications"
    else:
        apps = Path.home() / ".local" / "share" / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        (apps / "rojo-launcher.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Rojo Launcher\nExec=\"%s\" %s\nIcon=%s\n"
            "Terminal=false\nCategories=Development;\n" % (target, args, icon_file("png")))
        where = "in your app menu"
    (APP_HOME / "installed").write_text(str(target))
    return where


# ---------------------------------------------------------------- github

def _get_json(url, token=""):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "rojo-launcher",
    })
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def git_saved_token():
    """Reuse the GitHub sign-in Git already saved (e.g. Git Credential Manager), if any."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    try:
        p = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                           capture_output=True, timeout=15, env=env, **TEXT, **_no_window())
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def list_repos(user, token=""):
    """Return [{name, clone_url, html_url, default_branch, private, description}], newest push first."""
    if token:
        return _list_repos_api(user, token)
    gh = shutil.which("gh")
    if gh:
        # The GitHub CLI, if installed and signed in, also sees private repos.
        try:
            out = subprocess.run(
                [gh, "repo", "list", user, "--limit", "200", "--json",
                 "name,url,defaultBranchRef,isPrivate,pushedAt,description"],
                capture_output=True, timeout=30, **TEXT, **_no_window())
            if out.returncode == 0:
                data = json.loads(out.stdout)
                data.sort(key=lambda d: d.get("pushedAt") or "", reverse=True)
                return [{
                    "name": d["name"],
                    "clone_url": d["url"] + ".git",
                    "html_url": d["url"],
                    "default_branch": (d.get("defaultBranchRef") or {}).get("name") or "",
                    "private": d.get("isPrivate", False),
                    "description": d.get("description") or "",
                } for d in data]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    saved = git_saved_token()
    if saved:
        try:
            return _list_repos_api(user, saved)
        except Exception:
            pass  # saved login didn't work for the API; fall back to the public list
    return _list_repos_api(user, "")


def _list_repos_api(user, token):
    repos = []
    page = 1
    while True:
        if token:
            url = ("https://api.github.com/user/repos?per_page=100&sort=pushed"
                   "&affiliation=owner,collaborator,organization_member&page=%d" % page)
        else:
            url = "https://api.github.com/users/%s/repos?per_page=100&sort=pushed&page=%d" % (user, page)
        batch = _get_json(url, token)
        if not batch:
            break
        for d in batch:
            repos.append({
                "name": d["name"] if d["owner"]["login"].lower() == user.lower() else d["full_name"],
                "clone_url": d["clone_url"],
                "html_url": d["html_url"],
                "default_branch": d.get("default_branch") or "",
                "private": d.get("private", False),
                "description": d.get("description") or "",
            })
        if len(batch) < 100:
            break
        page += 1
    return repos


# ---------------------------------------------------------------- git / rojo

def repo_folder(repo, workspace):
    return Path(workspace) / repo["name"].split("/")[-1]


def run(args, cwd, log, env=None):
    """Run git with its output in the log. `env` may carry the token (never put on the command line)."""
    log("$ git " + " ".join(args))
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=600,
                           env=_env(env), **TEXT, **_no_window())
    except subprocess.TimeoutExpired:
        log("~ git took too long and was stopped")
        return 1
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log("~ couldn't run git (%s)" % type(e).__name__)
        return 1
    for line in (p.stdout + p.stderr).splitlines():
        if line.strip():
            log("~ " + line)
    return p.returncode


def default_branch(path, hint):
    if hint:
        return hint
    ref = git_out(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
    if ref:
        return ref.split("/", 1)[-1]
    for b in ("main", "master"):
        if git_out(["rev-parse", "--verify", "--quiet", "origin/" + b], path):
            return b
    return "main"


def find_project_file(folder):
    """None = default.project.json (rojo finds it), '' = none, else the file name."""
    folder = Path(folder)
    if (folder / "default.project.json").exists():
        return None
    found = sorted(folder.glob("*.project.json"))
    return found[0].name if found else ""


def sync_repo(repo, workspace, log, env=None):
    """Clone or pull. Returns {ok, kind, message, count}."""
    folder = repo_folder(repo, workspace)
    try:
        Path(workspace).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "kind": "error", "message": "Can't create your repos folder: %s" % e}

    if not (folder / ".git").exists():
        if folder.exists() and any(folder.iterdir()):
            return {"ok": False, "kind": "error",
                    "message": "A folder called %s is already there and isn't a git repo." % folder.name}
        if run(["clone", repo["clone_url"], str(folder)], workspace, log, env) != 0:
            return {"ok": False, "kind": "error", "message": "Download failed. Open Activity for details."}
        return {"ok": True, "kind": "ok", "message": "Downloaded", "count": -1}

    if run(["fetch", "origin", "--prune"], folder, log, env) != 0:
        return {"ok": False, "kind": "error", "message": "Couldn't reach GitHub. Are you offline?"}
    branch = default_branch(folder, repo.get("default_branch"))
    current = git_out(["rev-parse", "--abbrev-ref", "HEAD"], folder)
    if current != branch and run(["checkout", branch], folder, log) != 0:
        return {"ok": False, "kind": "warn",
                "message": "You have unsaved changes, so it couldn't switch to %s." % branch}
    before = git_out(["rev-parse", "HEAD"], folder)
    if run(["merge", "--ff-only", "origin/" + branch], folder, log) != 0:
        return {"ok": False, "kind": "warn",
                "message": "Your copy has changes that aren't on GitHub, so nothing was pulled."}
    after = git_out(["rev-parse", "HEAD"], folder)
    if before and after and before != after:
        n = git_out(["rev-list", "--count", "%s..%s" % (before, after)], folder)
        n = int(n) if n.isdigit() else 1
        return {"ok": True, "kind": "ok", "count": n,
                "message": "Pulled %d new change%s" % (n, "" if n == 1 else "s")}
    return {"ok": True, "kind": "ok", "count": 0, "message": "Already up to date"}


class _WinJob:
    """Windows job object: killing it kills Rojo and anything it started (Aftman/Rokit shims),
    and Windows kills it by itself if the launcher dies."""

    def __init__(self, proc):
        import ctypes
        from ctypes import wintypes as w
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = w.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.CloseHandle.argtypes = [w.HANDLE]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class Basic(ctypes.Structure):
            _fields_ = [("a", ctypes.c_longlong), ("b", ctypes.c_longlong), ("LimitFlags", w.DWORD),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", w.DWORD),
                        ("f", ctypes.c_size_t), ("g", w.DWORD), ("h", w.DWORD)]

        class Ext(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", IO), ("i", ctypes.c_size_t), ("j", ctypes.c_size_t),
                        ("k", ctypes.c_size_t), ("l", ctypes.c_size_t)]

        self.k = k
        self.h = k.CreateJobObjectW(None, None)
        if not self.h:
            raise OSError("CreateJobObject failed")
        info = Ext()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k.SetInformationJobObject(self.h, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not k.AssignProcessToJobObject(self.h, int(proc._handle)):
            k.CloseHandle(self.h)
            raise OSError("AssignProcessToJobObject failed")

    def kill(self):
        try:
            self.k.TerminateJobObject(self.h, 1)
            self.k.CloseHandle(self.h)
        except Exception:
            pass


BUSY_PORT_TEXT = ("in use", "already", "only one usage", "10048", "addrinuse", "os error 98", "os error 48")


class RojoServer:
    def __init__(self, log, on_change, on_crash):
        self.log = log
        self.on_change = on_change
        self.on_crash = on_crash
        self.proc = None
        self.job = None
        self.repo = None
        self.port = None
        self.ready = False
        self.lock = threading.Lock()   # guards the fields above
        self.life = threading.Lock()   # one start/stop at a time

    def start(self, folder, port, repo_name):
        with self.life:
            rojo = shutil.which("rojo")
            if not rojo:
                return False, "Rojo isn't installed (or isn't on your PATH)."
            project = find_project_file(folder)
            if project == "":
                return False, "This repo has no Rojo project file (.project.json)."
            self._stop()
            if port_open(port):
                return False, ("Port %d is already in use. Is Rojo running somewhere else, "
                               "like the VS Code extension?" % port)
            cmd = [rojo, "serve"] + ([project] if project else []) + ["--port", str(port)]
            self.log("$ rojo " + " ".join(cmd[1:]))
            extra = {} if sys.platform == "win32" else {"start_new_session": True}
            try:
                proc = subprocess.Popen(cmd, cwd=str(folder), stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, bufsize=1,
                                        **TEXT, **extra, **_no_window())
            except OSError as e:
                return False, "Couldn't start Rojo: %s" % e
            job = None
            if sys.platform == "win32":
                try:
                    job = _WinJob(proc)
                except Exception:
                    job = None
            with self.lock:
                self.proc, self.job, self.repo, self.port, self.ready = proc, job, repo_name, port, False
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        threading.Thread(target=self._probe, args=(proc, port), daemon=True).start()
        self.on_change()
        return True, "Rojo is live on port %d" % port

    def _probe(self, proc, port):
        # "Live" means the port really answers, not just that the process started.
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.proc is not proc or proc.poll() is not None:
                return
            if port_open(port):
                break
            time.sleep(0.25)
        if self.proc is proc and proc.poll() is None and not self.ready:
            self.ready = True
            self.on_change()

    def _pump(self, proc):
        tail, busy_port = [], False
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if not line.strip():
                    continue
                low = line.lower()
                if not low.startswith("note: run with"):
                    tail = (tail + [line])[-6:]
                if any(t in low for t in BUSY_PORT_TEXT) and ("port" in low or "address" in low or "socket" in low):
                    busy_port = True
                self.log("> " + line)
        except (OSError, ValueError):
            pass
        code = proc.wait()
        crashed = self.proc is proc
        with self.lock:
            if self.proc is proc:
                self.proc, self.ready = None, False
                if self.job:
                    self.job.kill()
                    self.job = None
        if crashed:
            detail = next((l for l in reversed(tail) if "details" in l.lower() or "error" in l.lower()),
                          tail[-1] if tail else "exit code %s" % code)
            msg = ("Port %s is busy. Is Rojo already running somewhere else?" % self.port if busy_port
                   else "Rojo stopped: " + detail.strip())
            self.log("!! " + msg)
            self.on_crash(msg)
        else:
            self.log("Rojo stopped.")
        self.on_change()

    def running(self):
        p = self.proc
        return p is not None and p.poll() is None

    def stop(self):
        with self.life:
            self._stop()

    def _stop(self):
        with self.lock:
            p, job = self.proc, self.job
            self.proc, self.job, self.ready = None, None, False
        if p is not None:
            if p.poll() is None:
                if sys.platform == "win32":
                    # Kill the whole tree: `rojo` may be a shim that started the real server.
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                                   **_no_window())
                else:
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                    except OSError:
                        p.terminate()
            if job:
                job.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if sys.platform != "win32":
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    else:
                        p.kill()
                except OSError:
                    pass
        self.on_change()


# ---------------------------------------------------------------- app state + actions

class App:
    def __init__(self):
        self.cfg = load_config()
        self.fetched = list(self.cfg.get("repo_cache") or [])
        self.repos = self.merged(self.fetched)
        self.update = None        # {"version", ...} when a newer app version is available
        self.updating = False
        self.lock = threading.RLock()
        self.clients = set()
        self.busy = {}
        self.repo_locks = collections.defaultdict(threading.Lock)
        self.fetch_sem = threading.BoundedSemaphore(2)
        self.infos = {}           # name -> local repo info shown in the UI
        self.loading = False
        self.logs = collections.deque(maxlen=400)
        self.rojo = None          # None = still checking, "" = not found, else version
        self.git = bool(shutil.which("git"))
        self.token = secrets.token_urlsafe(24)
        self.port = None
        self.window = None        # pywebview window, when used
        self.webview = None
        self.started = time.time()
        self.ever_connected = False
        self.last_disconnect = None
        self.quick_exit = False
        self.shutdown_evt = threading.Event()
        self.main_tasks = queue.Queue()
        self.httpd = None
        self.server = RojoServer(self.log, on_change=self.push_state, on_crash=self.rojo_crashed)

    # ---- events

    def log(self, line):
        kind = "text"
        if line.startswith("!! "):
            kind, line = "err", line[3:]
        elif line.startswith("OK "):
            kind, line = "ok", line[3:]
        elif line.startswith("$ "):
            kind = "cmd"
        elif line.startswith("~ "):
            kind, line = "dim", line[2:]
        elif line.startswith("> "):
            kind, line = "out", line[2:]
        elif line.startswith("── "):
            kind = "info"
        ev = {"type": "log", "t": time.strftime("%H:%M:%S"), "text": line, "kind": kind}
        log_file(line)
        with self.lock:
            self.logs.append(ev)
        self.broadcast(ev)

    def broadcast(self, ev):
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            q.put(ev)

    def push_state(self):
        self.broadcast({"type": "state", "state": self.snapshot()})

    def result(self, op, repo, ok, message, kind=None, **extra):
        self.broadcast(dict(extra, type="result", op=op, repo=repo, ok=ok, message=message,
                            kind=kind or ("ok" if ok else "error")))

    def rojo_crashed(self, msg):
        self.result("rojo", None, False, msg)

    # ---- state

    def merged(self, found):
        names = {r["name"] for r in found}
        return list(found) + [e for e in (self.cfg.get("extra_repos") or []) if e.get("name") not in names]

    def find(self, name):
        for r in self.repos:
            if r["name"] == name:
                return r
        return None

    def auth(self):
        """Env vars that make git send the saved token (private repos pull without a login).
        Passed through the environment so the token never appears on a command line or in logs."""
        tok = self.cfg.get("token") or ""
        if not tok:
            return None
        basic = base64.b64encode(("x-access-token:" + tok).encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + basic}

    def rojo_port(self):
        try:
            p = int(self.cfg.get("port") or 34872)
            return p if 0 < p < 65536 else 34872
        except (TypeError, ValueError):
            return 34872

    def snapshot(self):
        favs = set(self.cfg.get("favorites") or [])
        extra = {e.get("name") for e in (self.cfg.get("extra_repos") or [])}
        ws = self.cfg["workspace"]
        with self.lock:
            repos = [{
                "name": r["name"], "description": r.get("description") or "", "private": bool(r.get("private")),
                "cloned": repo_folder(r, ws).joinpath(".git").exists(), "favorite": r["name"] in favs,
                "extra": r["name"] in extra,
            } for r in self.repos]
            busy = dict(self.busy)
            infos = dict(self.infos)
        srv = self.server
        has_token = bool(self.cfg.get("token"))
        return {
            "repos": repos,
            "selected": self.cfg.get("last_repo") or "",
            "serving": {"repo": srv.repo, "port": srv.port, "ready": srv.ready} if srv.running() else None,
            "busy": busy,
            "infos": infos,
            "loading": self.loading,
            "rojo": self.rojo,
            "git": self.git,
            "public_only": bool(repos) and not has_token and not any(r["private"] for r in repos),
            "settings": {"github_user": self.cfg["github_user"], "workspace": ws, "port": self.rojo_port(),
                         "auto_start": bool(self.cfg.get("auto_start")), "has_token": has_token},
            "installed": is_installed(),
            "banner": not is_installed() and not self.cfg.get(self.banner_key()),
            "platform": {"win32": "win", "darwin": "mac"}.get(sys.platform, "linux"),
            "mac_app": MAC_APP,
            "version": APP_VERSION,
            "update": {"version": self.update["version"]} if self.update else None,
            "updating": self.updating,
        }

    def base_info(self, r):
        folder = repo_folder(r, self.cfg["workspace"])
        if not (folder / ".git").exists():
            return {"cloned": False}
        st = git_out(["status", "--porcelain=v2", "--branch"], folder)
        branch, dirty = "", False
        for line in st.splitlines():
            if line.startswith("# branch.head "):
                branch = line[len("# branch.head "):].strip()
            elif line and not line.startswith("#"):
                dirty = True
        default = r.get("default_branch") or default_branch(folder, "")
        return {"cloned": True, "branch": branch, "on_default": not branch or branch == default,
                "dirty": dirty, "project": find_project_file(folder) != ""}

    def check_behind(self, r):
        """git fetch + count new commits. {} when skipped (busy, or too many checks running)."""
        name = r["name"]
        folder = repo_folder(r, self.cfg["workspace"])
        lock = self.repo_locks[name]
        if self.busy.get(name) or not lock.acquire(blocking=False):
            return {}
        try:
            if not self.fetch_sem.acquire(blocking=False):
                return {}
            try:
                if not git_ok(["fetch", "origin", "--quiet"], folder, env=self.auth()):
                    return {"behind": None, "offline": True}
                branch = default_branch(folder, r.get("default_branch"))
                n = git_out(["rev-list", "--count", "HEAD..origin/" + branch], folder)
                return {"behind": int(n) if n.isdigit() else None, "offline": False}
            finally:
                self.fetch_sem.release()
        finally:
            lock.release()

    def update_info(self, name, fetch=False, push=True):
        r = self.find(name)
        if not r:
            return {"cloned": False}
        info = self.base_info(r)
        prev = self.infos.get(name) or {}
        if info.get("cloned"):
            for k in ("behind", "offline"):
                if k in prev:
                    info[k] = prev[k]  # keep the last known answer while re-checking
            if fetch:
                info.update(self.check_behind(r))
                info.setdefault("behind", None)
        with self.lock:
            self.infos[name] = info
        if push:
            self.push_state()
        return info

    def refresh_all_infos(self):
        def work():
            for r in list(self.repos):
                self.update_info(r["name"], push=False)
            self.push_state()
        threading.Thread(target=work, daemon=True).start()

    # ---- actions (called from HTTP threads; slow work goes to background threads)

    def _claim(self, name, what):
        with self.lock:
            if self.busy.get(name):
                return False
            self.busy[name] = what
        self.push_state()
        return True

    def _release(self, name):
        with self.lock:
            self.busy.pop(name, None)

    def pull(self, name, then_start=False):
        r = self.find(name)
        if not r:
            return {"ok": False, "message": "Unknown repo"}
        if not self._claim(name, "pull"):
            return {"ok": False, "message": "Already working on it"}

        def work():
            with self.repo_locks[name]:
                self.log("── %s ──" % name)
                try:
                    res = sync_repo(r, self.cfg["workspace"], self.log, self.auth())
                except Exception as e:  # never leave the button spinning
                    res = {"ok": False, "kind": "error", "message": str(e)}
            try:
                info = self.update_info(name, push=False)
                if res["ok"] and info.get("cloned"):
                    info["behind"], info["offline"] = 0, False  # the pull itself just fetched
            except Exception:
                pass
            self._release(name)
            self.log(("OK " if res["ok"] else "!! ") + res["message"])
            self.result("pull", name, res["ok"], res["message"], res["kind"], count=res.get("count", 0))
            self.push_state()
            if then_start and res["ok"]:
                self.start(name)
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def start(self, name):
        r = self.find(name)
        if not r:
            return {"ok": False, "message": "Unknown repo"}
        if not self._claim(name, "start"):
            return {"ok": False, "message": "Already working on it"}

        def work():
            folder = repo_folder(r, self.cfg["workspace"])
            try:
                if not (folder / ".git").exists():
                    ok, msg = False, "Download this repo first."
                else:
                    self.log("── %s ──" % name)
                    ok, msg = self.server.start(folder, self.rojo_port(), name)
            except Exception as e:
                ok, msg = False, str(e)
            self._release(name)
            self.log(("OK " if ok else "!! ") + msg)
            self.result("start", name, ok, msg)
            self.push_state()
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def stop(self):
        def work():
            self.server.stop()
            self.push_state()
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def refresh_repos(self):
        with self.lock:
            if self.loading:
                return {"ok": True}
            self.loading = True
        self.push_state()

        def work():
            user = self.cfg["github_user"]
            msg = ""
            try:
                found = list_repos(user, self.cfg.get("token", ""))
                with self.lock:
                    self.fetched = found
                    self.repos = self.merged(found)
                self.cfg["repo_cache"] = found
                save_config(self.cfg)
                self.refresh_all_infos()
            except urllib.error.HTTPError as e:
                msg = {401: "GitHub says your token is wrong or expired. Paste a new one in Settings.",
                       403: "GitHub said no (too many requests, or the token can't see your repos). Try again soon.",
                       404: "Couldn't find a GitHub user called %s." % user}.get(e.code, "GitHub error %s" % e.code)
            except Exception as e:
                self.log("!! %s" % e)
                msg = "Couldn't load your repos. Are you offline?"
            with self.lock:
                self.loading = False
            if msg:
                self.log("!! " + msg)
                self.result("refresh", None, False, msg)
            self.push_state()
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def save_settings(self, b):
        old = (self.cfg["github_user"], self.cfg.get("token", ""))
        old_ws = self.cfg["workspace"]
        user = str(b.get("github_user") or "").strip()
        if user:
            self.cfg["github_user"] = user
        ws = str(b.get("workspace") or "").strip()
        if ws:
            self.cfg["workspace"] = os.path.expanduser(ws)
        if "port" in b:
            try:
                p = int(b["port"])
                if 0 < p < 65536:
                    self.cfg["port"] = p
            except (TypeError, ValueError):
                pass
        if "auto_start" in b:
            self.cfg["auto_start"] = bool(b["auto_start"])
        if str(b.get("token") or "").strip():
            self.cfg["token"] = str(b["token"]).strip()
        if b.get("clear_token"):
            self.cfg["token"] = ""
        save_config(self.cfg)
        if self.cfg["workspace"] != old_ws:
            with self.lock:
                self.infos = {}
            self.refresh_all_infos()
        if (self.cfg["github_user"], self.cfg.get("token", "")) != old:
            self.refresh_repos()
        else:
            self.push_state()
        return {"ok": True}

    def add_repo(self, spec):
        parsed = parse_repo_spec(str(spec or ""))
        if not parsed:
            return {"ok": False, "message": "Type owner/repo, or paste the repo's GitHub link."}
        user = self.cfg["github_user"]
        owner, name = parsed
        owner = owner or user
        mine = owner.lower() == user.lower()
        entry = {"name": name if mine else "%s/%s" % (owner, name),
                 "clone_url": "https://github.com/%s/%s.git" % (owner, name),
                 "html_url": "https://github.com/%s/%s" % (owner, name),
                 "default_branch": "", "private": False, "description": ""}
        try:
            d = _get_json("https://api.github.com/repos/%s/%s" % (owner, name), self.cfg.get("token", ""))
            entry.update({"name": d["name"] if d["owner"]["login"].lower() == user.lower() else d["full_name"],
                          "clone_url": d["clone_url"], "html_url": d["html_url"],
                          "default_branch": d.get("default_branch") or "", "private": bool(d.get("private")),
                          "description": d.get("description") or ""})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"ok": False, "message": "Couldn't find %s/%s. If it's private, add a token in Settings first."
                        % (owner, name)}
        except Exception:
            pass  # offline: add it anyway, git will sort it out on the first pull
        n = entry["name"]
        favs = self.cfg.setdefault("favorites", [])
        if n not in favs:
            favs.append(n)
        if not any(r["name"] == n for r in self.fetched):
            self.cfg["extra_repos"] = [e for e in (self.cfg.get("extra_repos") or []) if e.get("name") != n] + [entry]
        save_config(self.cfg)
        with self.lock:
            self.repos = self.merged(self.fetched)
        self.update_info(n, push=False)
        self.push_state()
        return {"ok": True, "name": n}

    def remove_repo(self, name):
        self.cfg["extra_repos"] = [e for e in (self.cfg.get("extra_repos") or []) if e.get("name") != name]
        self.cfg["favorites"] = [f for f in (self.cfg.get("favorites") or []) if f != name]
        save_config(self.cfg)
        with self.lock:
            self.repos = self.merged(self.fetched)
        self.push_state()
        return {"ok": True}

    def latest_version(self):
        tok = self.cfg.get("token", "")
        fake = os.environ.get("ROJO_LAUNCHER_TEST_UPDATE")  # build test: pretend this zip is a new version
        if fake and FROZEN:
            return {"version": "999", "asset": Path(fake).resolve().as_uri(), "page": ""}
        if getattr(sys, "frozen", False):
            rel = _get_json("https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO, tok)
            want = "RojoLauncher-Windows.zip" if sys.platform == "win32" else "RojoLauncher-Mac.zip"
            asset = next((a for a in rel.get("assets") or [] if a.get("name") == want), None)
            return {"version": str(rel.get("tag_name", "0")).lstrip("vV"), "asset": asset and asset.get("url"),
                    "page": rel.get("html_url")}
        src = _download("https://api.github.com/repos/%s/contents/rojo_launcher.py?ref=main" % UPDATE_REPO, tok,
                        accept="application/vnd.github.raw", timeout=30)
        m = re.search(rb'^APP_VERSION = "([^"]+)"', src, re.M)
        if not m:
            raise ValueError("no version in the published file")
        return {"version": m.group(1).decode(), "source": src}

    def check_update(self, manual=False):
        def work():
            msg, ok = "", True
            try:
                info = self.latest_version()
                newer = version_tuple(info["version"]) > version_tuple(APP_VERSION)
                self.update = info if newer else None
                msg = ("Version %s is ready. Press Update." % info["version"] if newer
                       else "You have the newest version (%s)." % APP_VERSION)
            except Exception as e:
                ok, msg = False, "Couldn't check for updates right now."
                if manual:
                    self.log("~ Update check failed: %s" % e)
            self.push_state()
            if manual:
                self.result("update_check", None, ok, msg)
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def apply_update(self):
        upd = self.update
        if not upd:
            return {"ok": False, "message": "There's no update to install."}
        if self.updating:
            return {"ok": True}
        self.updating = True
        self.push_state()

        def work():
            try:
                cmd = self._install_exe(upd) if getattr(sys, "frozen", False) else self._install_script(upd)
            except Exception as e:
                self.updating = False
                self.log("!! Update failed: %s" % e)
                self.result("update", None, False, "The update didn't work: %s" % e)
                self.push_state()
                return
            if cmd is None:
                self.updating = False
                self.result("update", None, True, "The download page for version %s is open in your browser."
                            % upd["version"])
                self.push_state()
                return
            self.log("OK Updated to %s, restarting" % upd["version"])
            self.restart(cmd)
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def _install_script(self, upd):
        src = upd["source"]
        compile(src, "rojo_launcher.py", "exec")  # never install a file that wouldn't run
        targets = {Path(__file__).resolve()}
        installed = APP_HOME / ("rojo_launcher.pyw" if sys.platform == "win32" else "rojo_launcher.py")
        if installed.exists():
            targets.add(installed.resolve())
        for t in targets:
            tmp = t.with_name(t.name + ".new")
            tmp.write_bytes(src)
            os.replace(tmp, t)
        py = Path(sys.executable)
        if sys.platform == "win32" and py.with_name("pythonw.exe").exists():
            py = py.with_name("pythonw.exe")
        return [str(py), str(Path(__file__).resolve())]

    def _install_exe(self, upd):
        if MAC_APP and upd.get("asset") and mac_in_applications() and os.access(mac_bundle().parent, os.W_OK):
            return self._install_mac_app(upd)
        if sys.platform != "win32" or not upd.get("asset"):
            webbrowser.open(upd.get("page") or "https://github.com/%s/releases/latest" % UPDATE_REPO)
            return None  # not in Applications (or can't write there): the download page is open instead
        data = _download(upd["asset"], self.cfg.get("token", ""))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            new = z.read(next(n for n in z.namelist() if n.lower().endswith(".exe")))
        exe = Path(sys.executable)
        old = exe.with_name(exe.stem + ".old.exe")
        try:
            old.unlink()
        except OSError:
            pass
        exe.rename(old)  # Windows lets a running exe be renamed, not overwritten
        try:
            exe.write_bytes(new)
        except Exception:
            old.rename(exe)
            raise
        return [str(exe)]

    def _install_mac_app(self, upd):
        """Download the new .app next to us. The swap happens after this copy has quit: a small
        script waits for it, puts the new app where the old one was, and opens it."""
        work = APP_HOME / "update"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        zpath = work / "RojoLauncher-Mac.zip"
        zpath.write_bytes(_download(upd["asset"], self.cfg.get("token", "")))
        p = subprocess.run(["ditto", "-x", "-k", str(zpath), str(work / "new")], capture_output=True, **TEXT)
        if p.returncode != 0:
            raise RuntimeError("couldn't unpack the update (%s)" % p.stderr.strip())
        new = next((work / "new").glob("*.app"), None)
        if new is None:
            raise RuntimeError("the download has no app in it")
        p = subprocess.run(["codesign", "--verify", "--deep", str(new)], capture_output=True, **TEXT)
        if p.returncode != 0:  # never swap in an app that macOS wouldn't open
            raise RuntimeError("the downloaded app didn't check out (%s)" % p.stderr.strip())
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new)], capture_output=True)
        script = (
            'pid="$1"; app="$2"; new="$3"; work="$4"; i=0\n'
            'while kill -0 "$pid" 2>/dev/null && [ $i -lt 150 ]; do sleep 0.1; i=$((i+1)); done\n'
            'old="$app.old-$$"\n'
            'if mv "$app" "$old"; then\n'
            '  if mv "$new" "$app"; then rm -rf "$old"; else mv "$old" "$app"; fi\n'
            'fi\n'
            'open "$app"\n'
            'rm -rf "$work"\n')
        return ["/bin/sh", "-c", script, "rojo-launcher-update", str(os.getpid()), str(mac_bundle()),
                str(new), str(work)]

    def restart(self, cmd):
        """Start the new version on the same port and token, so an open window reconnects to it."""
        if self.server.running():
            self.cfg["resume_rojo"] = self.server.repo
        save_config(self.cfg)
        self.broadcast({"type": "restarting"})
        time.sleep(0.4)
        self.server.stop()
        try:
            (APP_HOME / "server.json").unlink()
        except OSError:
            pass
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        env = dict(os.environ, ROJO_LAUNCHER_PORT=str(self.port), ROJO_LAUNCHER_TOKEN=self.token,
                   ROJO_LAUNCHER_REUSE="1" if self.window is None else "0")
        if getattr(sys, "frozen", False):
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # the new exe must unpack itself, not reuse our temp folder
        extra = ({"creationflags": 0x00000008 | 0x00000200} if sys.platform == "win32"  # detached
                 else {"start_new_session": True})
        log_file("Reopening: %s" % (cmd[:2] if cmd[:1] == ["/bin/sh"] else cmd))
        try:
            subprocess.Popen(cmd, env=env, cwd=str(APP_HOME), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **extra)
        except OSError as e:
            self.log("!! Couldn't restart: %s" % e)
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass
        self.shutdown()
        # This copy is done (Rojo is stopped, settings saved): make sure it really goes away,
        # even if the window doesn't close, so the new one isn't left waiting on it.
        threading.Timer(4, os._exit, args=(0,)).start()

    def toggle_favorite(self, name):
        favs = self.cfg.setdefault("favorites", [])
        if name in favs:
            favs.remove(name)
        else:
            favs.append(name)
        save_config(self.cfg)
        self.push_state()
        return {"ok": True}

    def open_thing(self, name, what):
        r = self.find(name)
        if not r:
            return {"ok": False, "message": "Unknown repo"}
        if what == "github":
            url = r.get("html_url") or r["clone_url"]
            webbrowser.open(url[:-4] if url.endswith(".git") else url)
            return {"ok": True}
        folder = repo_folder(r, self.cfg["workspace"])
        if not folder.exists():
            return {"ok": False, "message": "Download it first."}
        if what == "folder":
            open_path(folder)
            return {"ok": True}
        if what == "code":
            code = shutil.which("code") or shutil.which("code.cmd")
            if not code:
                return {"ok": False, "message": "Couldn't find VS Code. Run 'Shell Command: Install code' in VS Code."}
            subprocess.Popen([code, str(folder)], **_no_window())
            return {"ok": True}
        return {"ok": False, "message": "?"}

    def browse(self):
        start = self.cfg["workspace"]
        if self.window is not None:
            wv = self.webview
            kind = getattr(getattr(wv, "FileDialog", None), "FOLDER", None)
            if kind is None:
                kind = getattr(wv, "FOLDER_DIALOG", 20)
            res = self.window.create_file_dialog(kind, directory=start)
            return {"path": res[0] if res else ""}
        done, box = threading.Event(), {}

        def ask():
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                box["path"] = filedialog.askdirectory(parent=root, initialdir=start) or ""
                root.destroy()
            except Exception:
                box["path"] = ""
            done.set()
        self.main_tasks.put(ask)  # Tk dialogs must run on the main thread (macOS)
        done.wait(600)
        return {"path": box.get("path", "")}

    def install(self):
        if MAC_APP:
            return self.move_to_applications()
        try:
            where = install_app()
            msg, ok = "Rojo Launcher is now %s" % where, True
        except Exception as e:
            msg, ok = "Couldn't add the shortcut: %s" % e, False
        self.log(("OK " if ok else "!! ") + msg)
        self.push_state()
        return {"ok": ok, "message": msg}

    def banner_key(self):
        return "move_banner_dismissed" if MAC_APP else "banner_dismissed"

    def move_to_applications(self):
        try:
            dest = mac_move_to_applications()
        except Exception as e:
            msg = "Couldn't move it to Applications: %s" % e
            self.log("!! " + msg)
            return {"ok": False, "message": msg}
        self.log("OK Moved to %s, reopening from there" % dest.parent)

        def reopen():
            time.sleep(0.6)  # let this answer reach the window first
            self.restart(["open", "-n", str(dest)])
        threading.Thread(target=reopen, daemon=True).start()
        return {"ok": True, "message": "Moved to Applications. Reopening from there..."}

    def action(self, name, b):
        repo = b.get("repo")
        if name == "pull":
            return self.pull(repo)
        if name == "start":
            return self.start(repo)
        if name == "stop":
            return self.stop()
        if name == "refresh":
            return self.refresh_repos()
        if name == "select":
            if self.find(repo):
                self.cfg["last_repo"] = repo
                save_config(self.cfg)
            return {"ok": True}
        if name == "favorite":
            return self.toggle_favorite(repo)
        if name == "settings":
            return self.save_settings(b)
        if name == "open":
            return self.open_thing(repo, b.get("what"))
        if name == "open_url":
            url = str(b.get("url") or "")
            if url.startswith("https://"):
                webbrowser.open(url)
            return {"ok": True}
        if name == "browse":
            return self.browse()
        if name == "install":
            return self.install()
        if name == "reveal_app":
            b = mac_bundle()
            if b:
                subprocess.Popen(["open", "-R", str(b)])
            return {"ok": bool(b)}
        if name == "dismiss_banner":
            self.cfg[self.banner_key()] = True
            save_config(self.cfg)
            self.push_state()
            return {"ok": True}
        if name == "size":
            try:
                w, h = int(b["w"]), int(b["h"])
                if 600 <= w <= 4000 and 400 <= h <= 3000:
                    self.cfg["size"] = [w, h]
                    save_config(self.cfg)
            except (KeyError, TypeError, ValueError):
                pass
            return {"ok": True}
        if name == "focus":
            self.focus()
            return {"ok": True}
        if name == "add_repo":
            return self.add_repo(b.get("spec"))
        if name == "remove_repo":
            return self.remove_repo(repo)
        if name == "check_update":
            return self.check_update(manual=True)
        if name == "apply_update":
            return self.apply_update()
        return {"ok": False, "message": "unknown action"}

    # ---- lifecycle

    def boot(self):
        def work():
            SHELL_PATH_READY.wait(8)  # Mac: Terminal's PATH first, so rojo is found the same way
            self.git = bool(shutil.which("git"))
            self.rojo = rojo_version()
            if not self.rojo:
                self.log("!! Rojo isn't installed or isn't on your PATH.")
            if not self.git:
                self.log("!! Git isn't installed. Get it from git-scm.com, then reopen this app.")
            self.refresh_all_infos()
            resume = self.cfg.get("resume_rojo") or ""
            if resume:
                self.cfg["resume_rojo"] = ""
                save_config(self.cfg)
            last = self.cfg.get("last_repo")
            r = self.find(last) if last else None
            if resume and self.find(resume):
                self.start(resume)  # Rojo was running before the app updated itself
            elif self.cfg.get("auto_start") and r and self.git:
                self.pull(last, then_start=True)

        def updates():
            time.sleep(3)
            while not self.shutdown_evt.is_set():
                self.check_update()
                self.shutdown_evt.wait(6 * 3600)
        threading.Thread(target=work, daemon=True).start()
        threading.Thread(target=updates, daemon=True).start()
        self.refresh_repos()

    def focus(self):
        w = self.window
        if w is None:
            open_app_window(self.url(), self.cfg.get("size"))
            return
        try:
            w.restore()
            w.show()
            w.on_top = True
            time.sleep(0.2)
            w.on_top = False
        except Exception:
            pass

    def url(self):
        return "http://127.0.0.1:%d/" % self.port

    def client_connected(self, q):
        with self.lock:
            self.clients.add(q)
            self.ever_connected = True
            self.quick_exit = False

    def client_gone(self, q):
        with self.lock:
            self.clients.discard(q)
            if not self.clients:
                self.last_disconnect = time.time()

    def watch_clients(self):
        # Browser-window mode: quit (and stop Rojo) once the window has been closed.
        while not self.shutdown_evt.wait(1):
            if self.window is not None:
                continue
            with self.lock:
                none = not self.clients
                gone_for = time.time() - (self.last_disconnect or time.time())
                grace = 2.5 if self.quick_exit else 12
            if none and self.ever_connected and gone_for > grace:
                self.shutdown()
            elif none and not self.ever_connected and time.time() - self.started > 120:
                self.shutdown()

    def wait_main(self):
        while not self.shutdown_evt.is_set():
            try:
                task = self.main_tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                task()
            except Exception:
                pass

    def shutdown(self):
        if self.shutdown_evt.is_set():
            return
        self.shutdown_evt.set()
        try:
            self.server.stop()
        except Exception:
            pass
        save_config(self.cfg)
        try:
            (APP_HOME / "server.json").unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- HTTP

def make_handler(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "RojoLauncher"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj))

        def _host_ok(self):  # blocks DNS-rebinding tricks from web pages
            return self.headers.get("Host", "") in ("127.0.0.1:%d" % app.port, "localhost:%d" % app.port)

        def _token_ok(self, q=None):
            t = self.headers.get("X-Token") or ((q or {}).get("t") or [""])[0]
            return secrets.compare_digest(t, app.token)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, "forbidden", "text/plain")
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                return self._send(200, PAGE.replace("__TOKEN__", app.token).replace("__VERSION__", APP_VERSION),
                                  "text/html; charset=utf-8")
            if u.path in ("/icon.png", "/favicon.ico"):
                return self._send(200, base64.b64decode(ICON_PNG), "image/png")
            if not self._token_ok(q):
                return self._send(403, "forbidden", "text/plain")
            if u.path == "/api/events":
                return self._events()
            if u.path == "/api/ping":
                return self._json({"ok": True})
            if u.path == "/api/state":
                return self._json(app.snapshot())
            if u.path == "/api/info":
                return self._json(app.update_info((q.get("repo") or [""])[0], (q.get("fetch") or ["0"])[0] == "1"))
            return self._send(404, "not found", "text/plain")

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, "forbidden", "text/plain")
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/bye" and self._token_ok(q):  # sent by the page as it closes
                with app.lock:
                    app.quick_exit = True
                return self._send(204, b"")
            if not self.headers.get("X-Token") or not self._token_ok():
                return self._send(403, "forbidden", "text/plain")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                body = {}
            try:
                res = app.action(u.path.rsplit("/", 1)[-1], body if isinstance(body, dict) else {})
            except Exception as e:
                res = {"ok": False, "message": str(e)}
            return self._json(res)

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = queue.Queue()
            app.client_connected(q)
            try:
                with app.lock:
                    logs = list(app.logs)
                first = [{"type": "state", "state": app.snapshot()}, {"type": "logs", "lines": logs}]
                self.wfile.write(b"retry: 800\n\n")
                for ev in first:
                    self.wfile.write(("data: %s\n\n" % json.dumps(ev)).encode())
                self.wfile.flush()
                while not app.shutdown_evt.is_set():
                    try:
                        ev = q.get(timeout=3)
                        data = "data: %s\n\n" % json.dumps(ev)
                    except queue.Empty:
                        data = ": ping\n\n"
                    self.wfile.write(data.encode())
                    self.wfile.flush()
            except (OSError, ValueError):
                pass
            finally:
                app.client_gone(q)

    return Handler


# ---------------------------------------------------------------- window

def find_app_browser():
    cands = []
    if sys.platform == "win32":
        pf = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for base in filter(None, pf):
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe",
                      Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe"]
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            cands.append(Path("/Applications/%s.app/Contents/MacOS/%s" % (app, app)))
    else:
        for exe in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
            w = shutil.which(exe)
            if w:
                cands.append(Path(w))
    for c in cands:
        if c.exists():
            return c
    return None


def open_app_window(url, size=None):
    w, h = (size or DEFAULTS["size"])[:2]
    exe = find_app_browser()
    if exe:
        try:
            subprocess.Popen([str(exe), "--app=" + url, "--window-size=%d,%d" % (w, h),
                              "--user-data-dir=" + str(APP_HOME / "window"), "--no-first-run",
                              "--no-default-browser-check", "--disable-features=Translate"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_no_window())
            return
        except OSError:
            pass
    webbrowser.open(url)


def close_splash(*_):
    """Close the native splash the .exe shows while it unpacks (PyInstaller --splash)."""
    try:
        import pyi_splash
        pyi_splash.close()
    except Exception:
        pass


def mac_window_look(window):
    """Mac: dark title bar in the page's colour, so the window looks like one piece."""
    try:
        import AppKit
        from PyObjCTools import AppHelper
        AppKit.NSApplication.sharedApplication().setAppearance_(
            AppKit.NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))

        def tint():
            try:
                w = window.native
                w.setTitlebarAppearsTransparent_(True)
                w.contentView().superview().subviews().lastObject().setBackgroundColor_(w.backgroundColor())
            except Exception:
                pass
        window.events.shown += lambda *_: AppHelper.callAfter(tint)
    except Exception:
        pass


def main():
    start_log_file()
    _fix_path()
    threading.Thread(target=_merge_shell_path, daemon=True).start()
    APP_HOME.mkdir(parents=True, exist_ok=True)
    mac_remove_old_shortcut()
    reuse_port = int(os.environ.pop("ROJO_LAUNCHER_PORT", "0") or 0)  # set when restarting after an update
    reuse_token = os.environ.pop("ROJO_LAUNCHER_TOKEN", "")
    reuse_window = os.environ.pop("ROJO_LAUNCHER_REUSE", "") == "1"
    if getattr(sys, "frozen", False):
        try:
            exe = Path(sys.executable)
            exe.with_name(exe.stem + ".old.exe").unlink()
        except OSError:
            pass

    # Already open? Bring that window forward instead of starting a second copy.
    try:
        if reuse_port:
            raise RuntimeError("restarting")
        info = json.loads((APP_HOME / "server.json").read_text())
        req = urllib.request.Request("http://127.0.0.1:%d/api/focus" % info["port"], data=b"{}",
                                     headers={"X-Token": info["token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:
            if json.loads(r.read()).get("ok"):
                close_splash()
                return
    except Exception:
        pass

    app = App()
    httpd = None
    if reuse_port:
        for _ in range(40):  # the old version may need a moment to let go of the port
            try:
                httpd = http.server.ThreadingHTTPServer(("127.0.0.1", reuse_port), make_handler(app))
                if reuse_token:
                    app.token = reuse_token
                break
            except OSError:
                time.sleep(0.15)
    if httpd is None:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        reuse_window = False
    httpd.daemon_threads = True
    app.httpd = httpd
    app.port = httpd.server_address[1]
    try:
        (APP_HOME / "server.json").write_text(json.dumps({"port": app.port, "token": app.token}))
    except OSError:
        pass
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=app.watch_clients, daemon=True).start()
    app.boot()

    w, h = (app.cfg.get("size") or DEFAULTS["size"])[:2]
    if os.environ.get("ROJO_LAUNCHER_HEADLESS"):  # for testing: print the address, no window
        print(app.url(), flush=True)
        app.wait_main()
    elif reuse_window:
        # After an update in browser-window mode, the open window reconnects by itself.
        deadline = time.time() + 8
        while time.time() < deadline and not app.ever_connected:
            time.sleep(0.2)
        if not app.ever_connected:
            open_app_window(app.url(), (w, h))
        app.wait_main()
    else:
        webview = None
        try:
            import webview  # pywebview: a real native window (bundled in the .exe build)
        except Exception:
            webview = None
        if webview is not None:
            try:
                app.webview = webview
                app.window = webview.create_window("Rojo Launcher", app.url(), width=w, height=h,
                                                   min_size=(820, 540), background_color="#0b0d12")
                app.window.events.shown += close_splash
                if sys.platform == "darwin":
                    mac_window_look(app.window)
                webview.start()
                app.shutdown()
            except Exception:
                app.window = None
                webview = None
        if webview is None and not app.shutdown_evt.is_set():
            open_app_window(app.url(), (w, h))
            close_splash()
            app.wait_main()
    app.shutdown()
    os._exit(0)


# ---------------------------------------------------------------- the page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rojo Launcher</title>
<link rel="icon" href="/icon.png">
<style>
:root{
  --bg:#0b0d12;--side:#0e1117;--panel:#141821;--panel2:#1a1f2a;--panel3:#212736;
  --line:rgba(255,255,255,.06);--line2:rgba(255,255,255,.11);
  --text:#eef1f6;--dim:#a2abba;--faint:#636c7d;
  --accent:#ff4f3f;--accent2:#ff7b47;--green:#34d399;--yellow:#fbbf24;--red:#f87171;
  --ease:cubic-bezier(.22,.9,.24,1);--spring:cubic-bezier(.34,1.5,.64,1);
  --side-w:264px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);overflow:hidden;
  font:14px/1.45 "Segoe UI Variable Text","Segoe UI",-apple-system,BlinkMacSystemFont,Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;user-select:none;-webkit-user-select:none;cursor:default}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;outline:none}
button:focus-visible{box-shadow:0 0 0 2px rgba(255,123,71,.6)}
input{font:inherit;color:inherit;user-select:text;-webkit-user-select:text}
svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none;display:block}
[hidden]{display:none!important}

/* ---------- splash */
#splash{position:fixed;inset:0;z-index:60;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:22px;
  background:radial-gradient(600px 400px at 50% 45%,rgba(255,79,63,.08),transparent 70%),var(--bg);
  transition:opacity .5s var(--ease),visibility .5s}
#splash img{width:76px;height:76px;animation:breathe 1.8s var(--ease) infinite}
#splash .bar{width:120px;height:3px;border-radius:3px;background:var(--panel2);overflow:hidden}
#splash .bar i{display:block;width:40%;height:100%;border-radius:3px;background:linear-gradient(90deg,var(--accent),var(--accent2));animation:slide 1.15s var(--ease) infinite}
body.ready #splash{opacity:0;visibility:hidden}
@keyframes breathe{0%,100%{transform:scale(1);filter:drop-shadow(0 0 0 rgba(255,79,63,0))}50%{transform:scale(1.05);filter:drop-shadow(0 10px 28px rgba(255,79,63,.35))}}
@keyframes slide{from{transform:translateX(-110%)}to{transform:translateX(260%)}}

/* ---------- layout */
#app{display:grid;grid-template-columns:var(--side-w) 1fr;height:100%;opacity:0;transform:scale(.985);transition:opacity .55s var(--ease),transform .55s var(--ease)}
body.ready #app{opacity:1;transform:none}

/* ---------- sidebar */
#side{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.brand{display:flex;align-items:center;gap:9px;padding:16px 8px 12px 18px}
.brand img{width:24px;height:24px}
.brand span{flex:1;min-width:0;font-weight:650;font-size:14.5px;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brand .icon-btn{width:30px;margin-left:-6px}
.icon-btn{width:32px;height:32px;display:grid;place-items:center;border-radius:9px;color:var(--faint);transition:background .18s,color .18s,transform .25s var(--spring)}
.icon-btn:hover{background:var(--panel2);color:var(--text)}
.icon-btn:active{transform:scale(.9)}
.icon-btn svg{width:17px;height:17px}
.spinning svg{animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.search{margin:2px 12px 8px;display:flex;align-items:center;gap:8px;height:36px;padding:0 10px 0 11px;border-radius:10px;
  background:var(--panel);border:1px solid var(--line);color:var(--faint);transition:border-color .2s,box-shadow .2s,background .2s}
.search:focus-within{border-color:rgba(255,99,70,.45);box-shadow:0 0 0 3px rgba(255,79,63,.1);background:var(--panel2)}
.search svg{width:16px;height:16px}
.search input{flex:1;min-width:0;background:none;border:0;outline:0}
.search input::placeholder{color:var(--faint)}
.search kbd{font:600 10.5px/1 inherit;font-family:inherit;color:var(--faint);border:1px solid var(--line2);border-radius:5px;padding:3px 5px;transition:opacity .2s}
.search:focus-within kbd{opacity:0}

#list{position:relative;flex:1;overflow-y:auto;overflow-x:hidden;padding:2px 8px 12px;scrollbar-width:thin;scrollbar-color:var(--panel3) transparent}
#list::-webkit-scrollbar{width:8px}
#list::-webkit-scrollbar-thumb{background:var(--panel3);border-radius:8px;border:2px solid var(--side)}
.sec{padding:12px 12px 5px;font-size:10.5px;font-weight:650;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
.row{position:relative;z-index:1;display:flex;align-items:center;gap:10px;width:100%;height:36px;padding:0 12px;border-radius:9px;
  color:var(--dim);text-align:left;transition:color .18s,background-color .18s}
.row:hover{color:var(--text);background:rgba(255,255,255,.03)}
.row.selected{color:var(--text)}
.row .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row.missing .nm{opacity:.55}
.row.selected.missing .nm{opacity:.8}
.row .st{width:7px;height:7px;border-radius:50%;flex:none;transition:background .3s,box-shadow .3s,transform .3s var(--spring);transform:scale(0)}
.row.live .st{transform:scale(1);background:var(--green);box-shadow:0 0 0 3px rgba(52,211,153,.16)}
.row.busy .st{transform:scale(1);width:11px;height:11px;background:none;border:2px solid var(--panel3);border-top-color:var(--text);animation:spin .75s linear infinite}
#ind{position:absolute;z-index:0;left:8px;right:8px;top:0;height:36px;border-radius:9px;background:var(--panel2);
  box-shadow:inset 0 0 0 1px var(--line);opacity:0;transition:transform .32s var(--ease),opacity .2s}
#ind::before{content:"";position:absolute;left:0;top:10px;bottom:10px;width:3px;border-radius:3px;background:linear-gradient(var(--accent),var(--accent2))}
.skel{height:12px;margin:15px 12px;border-radius:6px;background:linear-gradient(90deg,var(--panel) 0%,var(--panel2) 50%,var(--panel) 100%);background-size:200% 100%;animation:shimmer 1.3s linear infinite}
@keyframes shimmer{from{background-position:150% 0}to{background-position:-50% 0}}
.list-empty{padding:30px 16px;text-align:center;color:var(--faint);font-size:13px;white-space:pre-line}

.side-foot{padding:8px 12px 12px;display:flex;flex-direction:column;gap:8px}
.hint-link{font-size:12px;color:var(--faint);padding:4px 6px;text-align:left;border-radius:7px;transition:color .2s}
.hint-link:hover{color:var(--accent2)}
.banner{position:relative;padding:12px 12px 12px 14px;border-radius:12px;font-size:12.5px;color:var(--dim);
  background:linear-gradient(135deg,rgba(255,79,63,.13),rgba(255,123,71,.04));border:1px solid rgba(255,90,60,.22);animation:rise .5s var(--ease) both .4s}
.banner b{display:block;color:var(--text);font-weight:600;margin-bottom:2px}
.banner .acts{display:flex;gap:6px;margin-top:9px}
.banner .x{position:absolute;top:6px;right:6px;width:24px;height:24px}
@keyframes rise{from{opacity:0;transform:translateY(8px)}}

/* ---------- main */
#main{position:relative;display:flex;flex-direction:column;min-width:0;min-height:0;
  background:radial-gradient(800px 500px at 70% 0%,rgba(255,79,63,.045),transparent 70%)}
#stage{flex:1;display:flex;align-items:center;justify-content:center;overflow:auto;padding:28px}
#detail{width:100%;max-width:520px;display:flex;flex-direction:column;gap:14px}
.title{display:flex;align-items:center;gap:4px;min-width:0}
#name{font-size:30px;font-weight:700;letter-spacing:-.025em;line-height:1.15;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#star{color:var(--faint)}
#star.on{color:var(--yellow)}
#star.on svg{fill:currentColor}
#star.pop svg{animation:pop .45s var(--spring)}
@keyframes pop{0%{transform:scale(.6)}60%{transform:scale(1.25)}100%{transform:scale(1)}}
#sub{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:22px;margin:-8px 0 12px;color:var(--dim);font-size:13.5px}
.tag{font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;background:rgba(251,191,36,.12);color:var(--yellow);animation:rise .35s var(--ease)}
.tag.blue{background:rgba(122,167,255,.12);color:#9dbcff}

/* big pull button */
#pull{position:relative;overflow:hidden;isolation:isolate;display:flex;align-items:center;gap:16px;width:100%;height:88px;padding:0 24px 0 20px;border-radius:20px;text-align:left;color:#fff;
  background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);
  box-shadow:0 14px 34px -14px rgba(255,79,63,.7),inset 0 1px 0 rgba(255,255,255,.22),inset 0 -1px 0 rgba(0,0,0,.12);
  transition:transform .3s var(--spring),box-shadow .3s var(--ease),filter .3s}
#pull::before{content:"";position:absolute;inset:0;z-index:-1;background:linear-gradient(135deg,rgba(255,255,255,.14),transparent 55%);opacity:0;transition:opacity .3s}
#pull:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 20px 40px -14px rgba(255,79,63,.8),inset 0 1px 0 rgba(255,255,255,.22)}
#pull:hover:not(:disabled)::before{opacity:1}
#pull:active:not(:disabled){transform:translateY(0) scale(.985);transition-duration:.08s}
#pull:disabled{cursor:default}
#pull:disabled:not(.working):not(.done):not(.fail){filter:saturate(.55) brightness(.8)}
#pull.off{filter:grayscale(.7) brightness(.6)}
#pull .ic{position:relative;width:46px;height:46px;flex:none;border-radius:14px;display:grid;place-items:center;background:rgba(255,255,255,.18);transition:background .3s,color .3s,transform .4s var(--spring)}
#pull .ic svg{width:23px;height:23px;stroke-width:2.4}
#pull .tx{flex:1;min-width:0}
#pull b{display:block;font-size:19px;font-weight:650;letter-spacing:-.01em}
#pull small{display:block;font-size:13px;opacity:.86;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#pull .count{min-width:26px;height:26px;padding:0 8px;border-radius:999px;display:grid;place-items:center;font-size:13px;font-weight:700;color:var(--accent);background:#fff;
  transform:scale(0);transition:transform .35s var(--spring)}
#pull.has-new .count{transform:scale(1)}
#pull .bar{position:absolute;left:0;right:0;bottom:0;height:3px;overflow:hidden;opacity:0;transition:opacity .3s}
#pull .bar i{position:absolute;top:0;bottom:0;width:35%;border-radius:3px;background:rgba(255,255,255,.85)}
#pull.working .bar i{animation:slide 1.1s var(--ease) infinite}
#pull.working .bar{opacity:1}
#pull.working .ic svg{animation:bob .9s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(3px)}}
#pull.done .ic{background:#fff;color:var(--accent);transform:scale(1.06)}
#pull.done .ic svg{animation:pop .5s var(--spring)}
#pull.done::after{content:"";position:absolute;inset:0;z-index:-1;background:linear-gradient(100deg,transparent 30%,rgba(255,255,255,.35) 50%,transparent 70%);animation:shine 1s var(--ease) forwards}
@keyframes shine{from{transform:translateX(-100%)}to{transform:translateX(100%)}}
#pull.fail{animation:shake .45s var(--ease)}
#pull.fail .ic{background:rgba(0,0,0,.2)}
@keyframes shake{20%,60%{transform:translateX(-5px)}40%,80%{transform:translateX(5px)}}

/* rojo card */
#rojo{display:flex;align-items:center;gap:14px;min-height:72px;padding:14px 14px 14px 20px;border-radius:18px;background:var(--panel);border:1px solid var(--line);
  transition:border-color .4s var(--ease),background-color .4s var(--ease),box-shadow .4s}
#rojo .dot{position:relative;width:11px;height:11px;flex:none;border-radius:50%;background:var(--panel3);transition:background .35s}
#rojo.live{border-color:rgba(52,211,153,.28);background:linear-gradient(120deg,rgba(52,211,153,.09),rgba(52,211,153,0) 60%) var(--panel);box-shadow:0 10px 30px -18px rgba(52,211,153,.5)}
#rojo.live .dot{background:var(--green)}
#rojo.live .dot::after{content:"";position:absolute;inset:-6px;border-radius:50%;border:2px solid var(--green);animation:ping 1.9s var(--ease) infinite}
#rojo.starting .dot{background:var(--yellow);animation:blink 1s ease-in-out infinite}
#rojo.other .dot{background:var(--green);opacity:.5}
@keyframes ping{from{transform:scale(.4);opacity:.9}to{transform:scale(1.5);opacity:0}}
@keyframes blink{50%{opacity:.35}}
#rojo .tx{flex:1;min-width:0}
#rojo b{display:block;font-weight:650;font-size:15px}
#rojo small{display:block;color:var(--dim);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pill{position:relative;display:flex;align-items:center;justify-content:center;gap:8px;height:40px;min-width:96px;padding:0 18px;border-radius:12px;font-weight:650;font-size:13.5px;
  background:var(--panel2);box-shadow:inset 0 0 0 1px var(--line2);transition:background .2s,color .2s,box-shadow .2s,transform .25s var(--spring),opacity .2s}
.pill svg{width:15px;height:15px}
.pill:hover:not(:disabled){background:var(--panel3)}
.pill:active:not(:disabled){transform:scale(.95)}
.pill:disabled{opacity:.45;cursor:default}
.pill.go{background:var(--green);color:#052b1d;box-shadow:0 8px 20px -10px rgba(52,211,153,.8)}
.pill.go svg{fill:currentColor;stroke:none}
.pill.go:hover:not(:disabled){background:#4ee3a8}
.pill.stop svg{fill:currentColor;stroke:none}
.pill.stop:hover:not(:disabled){background:rgba(248,113,113,.12);color:var(--red);box-shadow:inset 0 0 0 1px rgba(248,113,113,.3)}
.pill .spin{width:14px;height:14px;border-radius:50%;border:2px solid rgba(255,255,255,.25);border-top-color:currentColor;animation:spin .7s linear infinite}
#restart{opacity:0;pointer-events:none;transform:scale(.8);transition:opacity .25s,transform .3s var(--spring),background .18s,color .18s}
#rojo.live #restart{opacity:1;pointer-events:auto;transform:none}
#restart:hover svg{animation:spin .6s var(--ease)}

.links{display:flex;justify-content:center;gap:4px;margin-top:6px}
.links button{display:flex;align-items:center;gap:7px;height:32px;padding:0 12px;border-radius:9px;color:var(--faint);font-size:12.5px;font-weight:550;transition:background .18s,color .18s}
.links button:hover{background:var(--panel);color:var(--text)}
.links svg{width:15px;height:15px}

#empty{display:flex;flex-direction:column;align-items:center;gap:10px;color:var(--faint);text-align:center}
#empty img{width:56px;height:56px;opacity:.5;filter:grayscale(1)}

/* activity drawer */
#activity{border-top:1px solid var(--line);background:rgba(14,17,23,.6)}
#act-toggle{display:flex;align-items:center;gap:8px;width:100%;height:36px;padding:0 18px;color:var(--faint);font-size:11px;font-weight:650;letter-spacing:.08em;text-transform:uppercase;transition:color .2s}
#act-toggle:hover{color:var(--dim)}
#act-toggle svg{width:14px;height:14px;transition:transform .35s var(--ease)}
#activity.open #act-toggle svg{transform:rotate(180deg)}
#act-dot{width:6px;height:6px;border-radius:50%;background:var(--red);transform:scale(0);transition:transform .3s var(--spring)}
#activity.unread #act-dot{transform:scale(1)}
#act-toggle .sp{flex:1}
#log-wrap{height:0;overflow:hidden;transition:height .38s var(--ease)}
#activity.open #log-wrap{height:190px}
#log{height:190px;overflow-y:auto;padding:0 18px 12px;font:11.5px/1.65 "Cascadia Mono",Consolas,"SF Mono",Menlo,monospace;color:#c6cdd8;user-select:text;-webkit-user-select:text;cursor:text;
  scrollbar-width:thin;scrollbar-color:var(--panel3) transparent;white-space:pre-wrap;word-break:break-word}
#log .t{color:#3d4554;margin-right:10px}
#log .cmd,#log .dim{color:var(--faint)}
#log .err{color:var(--red)}
#log .ok{color:var(--green)}
#log .info{color:#8fb0ff;margin-top:6px;display:inline-block}

/* toasts */
#toasts{position:fixed;z-index:40;bottom:52px;left:calc(var(--side-w) + (100% - var(--side-w)) / 2);transform:translateX(-50%);display:flex;flex-direction:column;align-items:center;gap:8px;pointer-events:none}
.toast{pointer-events:auto;display:flex;align-items:center;gap:10px;max-width:460px;padding:8px 8px 8px 12px;border-radius:14px;font-size:13.5px;
  background:rgba(30,35,47,.94);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);box-shadow:0 18px 40px -16px rgba(0,0,0,.7),inset 0 0 0 1px var(--line2);animation:tin .42s var(--spring)}
.toast.out{animation:tout .25s var(--ease) forwards}
.toast .ti{width:22px;height:22px;border-radius:50%;flex:none;display:grid;place-items:center}
.toast .ti svg{width:13px;height:13px;stroke-width:3}
.toast.ok .ti{background:rgba(52,211,153,.16);color:var(--green)}
.toast.error .ti{background:rgba(248,113,113,.16);color:var(--red)}
.toast.warn .ti{background:rgba(251,191,36,.16);color:var(--yellow)}
.toast .tm{flex:1;padding-right:4px}
.toast button{padding:5px 9px;border-radius:8px;font-size:12.5px;font-weight:650;color:var(--dim);transition:background .2s,color .2s}
.toast button:hover{background:var(--panel3);color:var(--text)}
.toast button svg{width:13px;height:13px}
@keyframes tin{from{opacity:0;transform:translateY(14px) scale(.95)}}
@keyframes tout{to{opacity:0;transform:translateY(8px) scale(.97)}}

/* settings sheet */
#scrim{position:fixed;inset:0;z-index:30;background:rgba(4,5,8,.5);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);opacity:0;visibility:hidden;transition:opacity .3s var(--ease),visibility .3s}
#sheet{position:fixed;z-index:31;top:0;right:0;bottom:0;width:390px;max-width:92%;display:flex;flex-direction:column;background:var(--panel);border-left:1px solid var(--line2);
  box-shadow:-30px 0 60px -30px rgba(0,0,0,.7);transform:translateX(104%);transition:transform .42s var(--ease);visibility:hidden}
body.settings #scrim{opacity:1;visibility:visible}
body.settings #sheet{transform:none;visibility:visible}
body:not(.settings) #sheet{transition:transform .42s var(--ease),visibility 0s .42s}
.sheet-head{display:flex;align-items:center;padding:18px 14px 6px 24px}
.sheet-head h2{flex:1;font-size:18px;font-weight:700;letter-spacing:-.01em}
.sheet-body{flex:1;overflow-y:auto;padding:10px 24px 24px;display:flex;flex-direction:column;gap:18px}
.field label{display:block;margin-bottom:6px;font-size:12px;font-weight:650;color:var(--dim)}
.field input[type=text],.field input[type=password],.field input[type=number]{width:100%;height:40px;padding:0 12px;border-radius:10px;background:var(--bg);border:1px solid var(--line2);outline:0;transition:border-color .2s,box-shadow .2s}
.field input:focus{border-color:rgba(255,99,70,.5);box-shadow:0 0 0 3px rgba(255,79,63,.1)}
.field .note{margin-top:6px;font-size:12px;color:var(--faint)}
.field .note button{color:var(--accent2);font-size:12px;font-weight:600}
.field .note button:hover{text-decoration:underline}
.inrow{display:flex;gap:8px}
.inrow input{flex:1;min-width:0}
.switch{display:flex;align-items:center;gap:12px;cursor:pointer}
.switch span{flex:1;font-size:13.5px;color:var(--text)}
.switch input{display:none}
.switch i{position:relative;flex:none;width:38px;height:22px;border-radius:999px;background:var(--panel3);transition:background .25s}
.switch i::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:transform .3s var(--spring)}
.switch input:checked+i{background:var(--green)}
.switch input:checked+i::after{transform:translateX(16px)}
.sheet-sep{height:1px;background:var(--line)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:38px;padding:0 16px;border-radius:10px;font-weight:650;font-size:13.5px;background:var(--panel2);box-shadow:inset 0 0 0 1px var(--line2);transition:background .2s,transform .25s var(--spring),filter .2s}
.btn:hover{background:var(--panel3)}
.btn:active{transform:scale(.96)}
.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;box-shadow:0 8px 20px -10px rgba(255,79,63,.8)}
.btn.primary:hover{filter:brightness(1.07)}
.btn.small{height:30px;padding:0 12px;font-size:12.5px;border-radius:8px}
.btn svg{width:15px;height:15px}
.sheet-foot{display:flex;align-items:center;gap:10px;padding:14px 24px;border-top:1px solid var(--line)}
.sheet-foot .ver{flex:1;font-size:12px;color:var(--faint);display:flex;flex-direction:column;gap:1px}
.sheet-foot .ver button{align-self:flex-start;font-size:12px;font-weight:600;color:var(--accent2)}
.sheet-foot .ver button:hover{text-decoration:underline}
.sheet-foot .ver button:disabled{color:var(--faint);text-decoration:none;cursor:default}

/* update card (sidebar) */
.upd{display:flex;align-items:center;gap:10px;padding:10px 10px 10px 12px;border-radius:12px;font-size:12.5px;color:var(--dim);
  background:linear-gradient(135deg,rgba(52,211,153,.13),rgba(52,211,153,.03));border:1px solid rgba(52,211,153,.25);animation:rise .5s var(--ease) both}
.upd .ui{width:28px;height:28px;border-radius:50%;flex:none;display:grid;place-items:center;background:rgba(52,211,153,.16);color:var(--green)}
.upd .ui svg{width:15px;height:15px;stroke-width:2.4}
.upd .ut{flex:1;min-width:0;line-height:1.3}
.upd .ut b{display:block;color:var(--text);font-weight:600}
.upd .btn{background:var(--green);color:#06281c;box-shadow:0 8px 20px -10px rgba(52,211,153,.8)}
.upd .btn:hover{filter:brightness(1.08);background:var(--green)}

/* add-a-repo dialog */
#add{position:fixed;inset:0;z-index:35;display:grid;place-items:center;padding:20px;background:rgba(4,5,8,.55);
  backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);opacity:0;visibility:hidden;transition:opacity .25s var(--ease),visibility 0s .25s}
body.adding #add{opacity:1;visibility:visible;transition:opacity .25s var(--ease)}
.dialog{width:420px;max-width:100%;padding:22px 22px 18px;border-radius:18px;background:var(--panel);border:1px solid var(--line2);
  box-shadow:0 30px 70px -30px rgba(0,0,0,.85);transform:translateY(10px) scale(.96);transition:transform .38s var(--spring)}
body.adding .dialog{transform:none}
.dialog h2{font-size:17px;font-weight:700;letter-spacing:-.01em}
.dialog p{margin:4px 0 14px;font-size:13px;color:var(--dim)}
.dialog input{width:100%;height:42px;padding:0 13px;border-radius:11px;background:var(--bg);border:1px solid var(--line2);outline:0;transition:border-color .2s,box-shadow .2s}
.dialog input:focus{border-color:rgba(255,99,70,.5);box-shadow:0 0 0 3px rgba(255,79,63,.1)}
.dialog .err{min-height:18px;margin-top:7px;font-size:12.5px;color:var(--red)}
.dialog .acts{display:flex;justify-content:flex-end;gap:8px;margin-top:6px}
.dialog .btn .spin{width:14px;height:14px;border-radius:50%;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;animation:spin .7s linear infinite}
.shake{animation:shake .38s var(--ease)}
@keyframes shake{20%{transform:translateX(-6px)}40%{transform:translateX(5px)}60%{transform:translateX(-3px)}80%{transform:translateX(2px)}}

/* updating overlay */
#updating{position:fixed;inset:0;z-index:70;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;
  background:radial-gradient(600px 400px at 50% 45%,rgba(52,211,153,.07),transparent 70%),var(--bg);
  opacity:0;visibility:hidden;transition:opacity .4s var(--ease),visibility 0s .4s}
body.updating #updating{opacity:1;visibility:visible;transition:opacity .4s var(--ease)}
#updating img{width:64px;height:64px}
body.updating #updating img{animation:breathe 1.8s var(--ease) infinite}
#updating b{font-size:15px;font-weight:650}
#updating small{font-size:12.5px;color:var(--faint);margin-top:-10px}
#updating .bar{width:120px;height:3px;border-radius:3px;background:var(--panel2);overflow:hidden}
#updating .bar i{display:block;width:40%;height:100%;border-radius:3px;background:var(--green)}
body.updating #updating .bar i{animation:slide 1.15s var(--ease) infinite}

@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<div id="splash"><img src="/icon.png" alt=""><div class="bar"><i></i></div></div>

<div id="app">
  <aside id="side">
    <div class="brand">
      <img src="/icon.png" alt="">
      <span>Rojo Launcher</span>
      <button class="icon-btn" id="add-btn" title="Add a repo (Ctrl+N)"></button>
      <button class="icon-btn" id="refresh" title="Refresh repos (F5)"></button>
      <button class="icon-btn" id="gear" title="Settings"></button>
    </div>
    <label class="search"><span id="search-ic"></span><input id="q" placeholder="Search" spellcheck="false" autocomplete="off"><kbd>Ctrl F</kbd></label>
    <div id="list"><div id="ind"></div></div>
    <div class="side-foot">
      <div class="upd" id="upd" hidden>
        <span class="ui" id="upd-ic"></span>
        <div class="ut"><b>Update ready</b><span id="upd-ver"></span></div>
        <button class="btn small" id="upd-btn">Update</button>
      </div>
      <button class="hint-link" id="private-hint" hidden>Missing private repos? Add a token</button>
      <div class="banner" id="banner" hidden>
        <button class="icon-btn x" id="banner-x" title="Hide"></button>
        <b id="banner-title">Open it like a normal app</b><span id="banner-text">Add Rojo Launcher to your Desktop and Start menu.</span>
        <div class="acts"><button class="btn small primary" id="banner-add">Add to Desktop</button></div>
      </div>
    </div>
  </aside>

  <main id="main">
    <div id="stage">
      <div id="empty" hidden><img src="/icon.png" alt=""><div>Pick a repo on the left</div></div>
      <section id="detail" hidden>
        <div class="title"><h1 id="name"></h1><button class="icon-btn" id="star" title="Favorite"></button></div>
        <div id="sub"></div>
        <button id="pull" title="Enter">
          <span class="ic" id="pull-ic"></span>
          <span class="tx"><b id="pull-label">Pull</b><small id="pull-sub"></small></span>
          <span class="count" id="pull-count"></span>
          <span class="bar"><i></i></span>
        </button>
        <div id="rojo">
          <span class="dot"></span>
          <div class="tx"><b id="rojo-label">Rojo is off</b><small id="rojo-sub"></small></div>
          <button class="icon-btn" id="restart" title="Restart Rojo"></button>
          <button class="pill" id="rojo-btn" title="Ctrl+Enter"></button>
        </div>
        <div class="links">
          <button data-open="folder" id="l-folder"></button>
          <button data-open="code" id="l-code"></button>
          <button data-open="github" id="l-gh"></button>
          <button id="l-remove" title="Take it off your list (the folder on your computer stays)" hidden></button>
        </div>
      </section>
    </div>
    <div id="activity">
      <button id="act-toggle"><span>Activity</span><span id="act-dot"></span><span class="sp"></span><span id="act-chev"></span></button>
      <div id="log-wrap"><div id="log"></div></div>
    </div>
  </main>
</div>

<div id="toasts"></div>
<div id="add">
  <div class="dialog" role="dialog" aria-labelledby="add-h">
    <h2 id="add-h">Add a repo</h2>
    <p>Any repo on GitHub, yours or someone else's. It goes into your Favorites.</p>
    <input type="text" id="add-in" placeholder="owner/repo or a GitHub link" spellcheck="false" autocomplete="off">
    <div class="err" id="add-err"></div>
    <div class="acts"><button class="btn" id="add-cancel">Cancel</button><button class="btn primary" id="add-ok">Add</button></div>
  </div>
</div>
<div id="updating"><img src="/icon.png" alt=""><b id="upd-title">Updating Rojo Launcher…</b><small id="upd-note">This only takes a few seconds</small><div class="bar"><i></i></div></div>
<div id="scrim"></div>
<aside id="sheet">
  <div class="sheet-head"><h2>Settings</h2><button class="icon-btn" id="sheet-x" title="Close"></button></div>
  <div class="sheet-body">
    <div class="field"><label for="f-user">GitHub user</label><input type="text" id="f-user" spellcheck="false"></div>
    <div class="field"><label for="f-ws">Where repos are saved</label>
      <div class="inrow"><input type="text" id="f-ws" spellcheck="false"><button class="btn" id="f-browse">Browse</button></div></div>
    <div class="field"><label for="f-token">GitHub token</label><input type="password" id="f-token" spellcheck="false" autocomplete="off">
      <div class="note" id="token-note"></div></div>
    <div class="field"><label for="f-port">Rojo port</label><input type="number" id="f-port" min="1" max="65535"></div>
    <label class="switch"><span>Pull and start my last repo when the app opens</span><input type="checkbox" id="f-auto"><i></i></label>
    <div class="sheet-sep"></div>
    <div class="field"><label id="f-install-label">Shortcut</label><button class="btn" id="f-install"></button></div>
  </div>
  <div class="sheet-foot"><span class="ver"><span id="ver"></span><button id="chk">Check for updates</button></span><button class="btn" id="sheet-cancel">Cancel</button><button class="btn primary" id="sheet-save">Save</button></div>
</aside>

<script>
"use strict";
const T = "__TOKEN__";
const PAGE_VERSION = "__VERSION__";
const $ = (s) => document.querySelector(s);
const I = {
  search:'<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  gear:'<svg viewBox="0 0 24 24"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>',
  refresh:'<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/></svg>',
  star:'<svg viewBox="0 0 24 24"><path d="m12 3.5 2.6 5.3 5.9.9-4.25 4.1 1 5.8L12 16.9l-5.25 2.7 1-5.8L3.5 9.7l5.9-.9z"/></svg>',
  down:'<svg viewBox="0 0 24 24"><path d="M12 4v11"/><path d="m7 10.5 5 5 5-5"/><path d="M5 20h14"/></svg>',
  check:'<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7"/></svg>',
  alert:'<svg viewBox="0 0 24 24"><path d="M12 7.5v5.5"/><path d="M12 16.8v.2"/></svg>',
  x:'<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  folder:'<svg viewBox="0 0 24 24"><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5z"/></svg>',
  code:'<svg viewBox="0 0 24 24"><path d="m8 8-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14"/></svg>',
  ext:'<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/></svg>',
  chev:'<svg viewBox="0 0 24 24"><path d="m6 15 6-6 6 6"/></svg>',
  play:'<svg viewBox="0 0 24 24"><path d="M7 4.5v15l12.5-7.5z"/></svg>',
  stop:'<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>',
  monitor:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
  plus:'<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  trash:'<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>',
  up:'<svg viewBox="0 0 24 24"><path d="M12 19V6"/><path d="m6.5 11.5 5.5-5.5 5.5 5.5"/></svg>',
};
const EASE = "cubic-bezier(.22,.9,.24,1)";
const SPIN = '<span class="spin"></span>';
const started = performance.now();
let S = null, sel = null, visible = [], stopping = false, shownRepo = null, quick = false;
let restarting = 0, pendingSelect = null;
const checking = new Set(), fx = {}, hold = {}, pendingStart = {};
const nodes = new Map();

function api(path, body) {
  const opt = { headers: { "X-Token": T } };
  if (!path.startsWith("info")) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body || {}); }
  return fetch("/api/" + path, opt).then(r => r.json()).catch(() => ({ ok: false }));
}
function setHTML(el, html) { if (el._h !== html) { el._h = html; el.innerHTML = html; } }
function setText(el, t) { if (el.textContent !== t) el.textContent = t; }
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function repo(name) { return S && S.repos.find(r => r.name === name); }
function infoOf(name) { return (S && S.infos && S.infos[name]) || {}; }
// Same repo, new state: the text slides in. Different repo: it just changes, nothing moves.
function swapText(el, t) {
  if (el.textContent === t) return;
  el.textContent = t;
  el.getAnimations().forEach(a => a.cancel());
  el.animate([{ opacity: 0, transform: "translateY(4px)" }, { opacity: 1, transform: "none" }], { duration: 220, easing: EASE });
}
function fadeIn(el) {
  el.getAnimations().forEach(a => a.cancel());
  el.animate([{ opacity: 0.15 }, { opacity: 1 }], { duration: 200, easing: "ease-out" });
}

/* ---------- static icons */
setHTML($("#refresh"), I.refresh); setHTML($("#gear"), I.gear); setHTML($("#search-ic"), I.search);
setHTML($("#sheet-x"), I.x); setHTML($("#banner-x"), I.x); setHTML($("#restart"), I.refresh); setHTML($("#act-chev"), I.chev);
setHTML($("#l-folder"), I.folder + "Folder"); setHTML($("#l-code"), I.code + "VS Code"); setHTML($("#l-gh"), I.ext + "GitHub");
setHTML($("#star"), I.star);
setHTML($("#add-btn"), I.plus); setHTML($("#l-remove"), I.trash + "Remove"); setHTML($("#upd-ic"), I.up);
if (/Mac/.test(navigator.platform)) document.querySelector(".search kbd").textContent = "⌘F";

/* ---------- live connection */
function connect() {
  const es = new EventSource("/api/events?t=" + encodeURIComponent(T));
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "state") onState(ev.state);
    else if (ev.type === "log") addLog(ev);
    else if (ev.type === "logs") { $("#log").replaceChildren(); ev.lines.forEach(l => addLog(l, true)); }
    else if (ev.type === "result") onResult(ev);
    else if (ev.type === "restarting") onRestarting();
  };
}

function onState(state) {
  // The app updated itself and this page is from the old version: load the new one.
  if (state.version !== PAGE_VERSION) { location.reload(); return; }
  const first = !S, before = sel;
  S = state;
  if (pendingSelect && repo(pendingSelect)) { sel = pendingSelect; pendingSelect = null; }
  if (!S.serving) stopping = false;
  if (sel && !repo(sel)) sel = null;
  if (!sel) sel = repo(S.selected) ? S.selected : (S.repos[0] && S.repos[0].name) || null;
  render();
  if (sel && (first || sel !== before)) loadInfo(sel);
  if (first) {
    const wait = Math.max(0, 750 - (performance.now() - started));
    setTimeout(() => {
      document.body.classList.add("ready");
      requestAnimationFrame(() => moveInd(false));
      setTimeout(() => $("#splash").remove(), 700);  // stop its animations once it has faded out
    }, wait);
  }
}

function render() { renderList(); renderDetail(); renderChrome(); }

/* ---------- sidebar */
function rowNode(r) {
  let n = nodes.get(r.name);
  if (!n) {
    n = document.createElement("button");
    n.className = "row";
    n.innerHTML = '<span class="nm"></span><span class="st"></span>';
    n.firstChild.textContent = r.name;
    n.addEventListener("click", () => select(r.name));
    n.addEventListener("dblclick", () => { select(r.name); doPull(); });
    nodes.set(r.name, n);
  }
  const live = S.serving && S.serving.repo === r.name;
  n.classList.toggle("live", !!live && !S.busy[r.name]);
  n.classList.toggle("busy", !!S.busy[r.name]);
  n.classList.toggle("missing", !r.cloned);
  n.classList.toggle("selected", r.name === sel);
  n.title = r.description || "";
  return n;
}
function staticNode(key, cls, text) {
  let n = nodes.get(key);
  if (!n) { n = document.createElement("div"); n.className = cls; n.textContent = text; nodes.set(key, n); }
  return n;
}
function renderList() {
  const list = $("#list"), ind = $("#ind");
  const q = $("#q").value.trim().toLowerCase();
  const want = [ind];
  if (!S.repos.length) {
    if (S.loading) for (let i = 0; i < 7; i++) { const s = staticNode("sk" + i, "skel", ""); s.style.width = (55 + (i * 37) % 35) + "%"; want.push(s); }
    else want.push(staticNode("empty", "list-empty", "No repos found.\nCheck your GitHub user in Settings."));
    visible = [];
  } else {
    const hit = r => !q || r.name.toLowerCase().includes(q) || r.description.toLowerCase().includes(q);
    const favs = S.repos.filter(r => r.favorite && hit(r)), rest = S.repos.filter(r => !r.favorite && hit(r));
    if (favs.length && !q) want.push(staticNode("sec-fav", "sec", "Favorites"));
    favs.forEach(r => want.push(rowNode(r)));
    if (favs.length && rest.length && !q) want.push(staticNode("sec-all", "sec", "Repos"));
    rest.forEach(r => want.push(rowNode(r)));
    if (!favs.length && !rest.length) want.push(staticNode("nomatch", "list-empty", "No matches"));
    visible = favs.concat(rest).map(r => r.name);
  }
  const cur = list.children;
  let same = cur.length === want.length;
  for (let i = 0; same && i < want.length; i++) if (cur[i] !== want[i]) same = false;
  if (!same) list.replaceChildren(...want);
  moveInd(true);
}
function moveInd(animate) {
  const ind = $("#ind"), n = sel && nodes.get(sel);
  if (!n || !n.isConnected) { ind.style.opacity = 0; return; }
  const snap = !animate || ind.style.opacity !== "1";
  if (snap) ind.style.transition = "none";
  ind.style.transform = "translateY(" + n.offsetTop + "px)";
  ind.style.opacity = 1;
  if (snap) { ind.offsetHeight; ind.style.transition = ""; }
}

let selTimer = 0, infoTimer = 0;
function select(name, opts = {}) {
  if (!name || !repo(name) || name === sel) return;
  sel = name;
  quick = !!opts.repeat;
  for (const [k, n] of nodes) if (n.classList.contains("row")) n.classList.toggle("selected", k === sel);
  moveInd(true);
  const n = nodes.get(sel);
  if (n && opts.scroll !== false) n.scrollIntoView({ block: "nearest", behavior: opts.repeat ? "auto" : "smooth" });
  renderDetail();
  // Only check GitHub once the user settles on a repo (not for every repo passed over).
  clearTimeout(infoTimer);
  infoTimer = setTimeout(() => { if (sel === name) loadInfo(name); }, opts.repeat ? 400 : 150);
  clearTimeout(selTimer);
  selTimer = setTimeout(() => api("select", { repo: name }), 500);
}
function move(d, repeat) {
  if (!visible.length) return;
  let i = visible.indexOf(sel);
  i = i < 0 ? 0 : Math.max(0, Math.min(visible.length - 1, i + d));
  select(visible[i], { repeat });
}

/* ---------- repo details */
function loadInfo(name) {
  const r = repo(name);
  if (!r || !r.cloned || checking.has(name) || S.busy[name]) return;
  checking.add(name);
  if (name === sel) renderDetail();
  api("info?repo=" + encodeURIComponent(name) + "&fetch=1").then(res => {
    if (res && res.cloned !== undefined && S.infos) S.infos[name] = res;
  }).finally(() => { checking.delete(name); if (name === sel) renderDetail(); });
}

function renderDetail() {
  const r = sel && repo(sel);
  $("#detail").hidden = !r; $("#empty").hidden = !!r || !S.repos.length;
  if (!r) { shownRepo = null; return; }
  const switching = r.name !== shownRepo;
  shownRepo = r.name;
  const put = switching ? setText : swapText;
  if (switching && !quick) { fadeIn($("#name")); fadeIn($("#sub")); }
  const i = infoOf(r.name);
  const busy = S.busy[r.name] || (hold[r.name] > Date.now() ? "pull" : undefined);
  setText($("#name"), r.name);
  $("#star").classList.toggle("on", r.favorite);
  $("#l-remove").hidden = !r.extra;

  // status line: only things that need attention
  let sub = "";
  if (!r.cloned) sub = "<span>Not on your computer yet</span>";
  else {
    if (i.dirty) sub += '<span class="tag">Unsaved changes</span>';
    if (i.on_default === false && i.branch) sub += '<span class="tag blue">on ' + esc(i.branch) + "</span>";
  }
  setHTML($("#sub"), sub);

  // big pull button
  const pull = $("#pull");
  const f = fx[r.name] && fx[r.name].until > Date.now() ? fx[r.name] : null;
  let label, small, icon = I.down, state = "";
  if (busy === "pull") { label = r.cloned ? "Pulling…" : "Downloading…"; small = "Getting the latest from GitHub"; state = "working"; }
  else if (f) { label = f.label; small = f.small; icon = f.ok ? I.check : I.alert; state = f.ok ? "done" : "fail"; }
  else if (!r.cloned) { label = "Download"; small = "Get this repo onto your computer"; }
  else {
    label = "Pull";
    small = i.behind > 0 ? i.behind + " new change" + (i.behind === 1 ? "" : "s") + " on GitHub"
      : i.behind === 0 ? "You're up to date"
      : (i.behind === undefined || checking.has(r.name)) && !i.offline ? "Checking for updates…"
      : "Get the latest from GitHub";
  }
  for (const c of ["working", "done", "fail"]) pull.classList.toggle(c, c === state);
  pull.classList.toggle("has-new", !busy && !f && r.cloned && i.behind > 0);
  setText($("#pull-count"), i.behind > 0 ? String(i.behind) : "");
  setHTML($("#pull-ic"), icon);
  put($("#pull-label"), label);
  put($("#pull-sub"), small);
  pull.disabled = !!busy || !S.git;
  pull.classList.toggle("off", !S.git);
  renderRojo(r, i, busy, put);
}

function renderRojo(r, i, busy, put) {
  const card = $("#rojo"), btn = $("#rojo-btn");
  const sv = S.serving, mine = sv && sv.repo === r.name;
  const known = !r.cloned || i.project !== undefined;
  let cls = "", label, small, bhtml, bcls = "", act = null, dis = false;
  if (S.rojo === "") { label = "Rojo isn't installed"; small = "Install it, then reopen this app"; bhtml = "How to"; act = () => api("open_url", { url: "https://rojo.space/docs/v7/getting-started/installation/" }); }
  else if (busy === "start") { cls = "starting"; label = "Starting Rojo…"; small = "One moment"; bhtml = SPIN; dis = true; }
  else if (mine && stopping) { cls = "starting"; label = "Stopping Rojo…"; small = "One moment"; bhtml = SPIN; dis = true; }
  else if (mine && !sv.ready) { cls = "starting"; label = "Starting Rojo…"; small = "Port " + sv.port; bhtml = I.stop + "Stop"; bcls = "stop"; act = doStop; }
  else if (mine) { cls = "live"; label = "Rojo is live"; small = "Port " + sv.port + " · press Connect in Studio"; bhtml = I.stop + "Stop"; bcls = "stop"; act = doStop; }
  else if (pendingStart[r.name]) { cls = "starting"; label = "Rojo starts after the pull"; small = "One moment"; bhtml = SPIN; dis = true; }
  else if (busy === "pull" && i.project !== false) {
    // Start during a pull: remember it and start as soon as the pull is done.
    cls = sv ? "other" : ""; label = sv ? "Rojo is running " + sv.repo : "Rojo is off";
    small = "Press Start and it runs right after the pull"; bhtml = I.play + (sv ? "Switch" : "Start"); bcls = "go";
    act = () => { pendingStart[r.name] = true; renderDetail(); };
  }
  else if (!r.cloned) { cls = sv ? "other" : ""; label = sv ? "Rojo is running " + sv.repo : "Rojo"; small = "Download this repo first"; bhtml = I.play + "Start"; dis = true; }
  else if (!known) { label = sv ? "Rojo is running " + sv.repo : "Rojo is off"; small = " "; bhtml = I.play + "Start"; dis = true; }
  else if (i.project === false) { label = "No Rojo project here"; small = "This repo has no .project.json file"; bhtml = I.play + "Start"; dis = true; }
  else if (sv) { cls = "other"; label = "Rojo is running " + sv.repo; small = "Switch it to this repo"; bhtml = I.play + "Switch"; bcls = "go"; act = doStart; }
  else { label = "Rojo is off"; small = "Start it, then press Connect in Studio"; bhtml = I.play + "Start"; bcls = "go"; act = doStart; }
  for (const c of ["live", "starting", "other"]) card.classList.toggle(c, c === cls);
  put($("#rojo-label"), label);
  setText($("#rojo-sub"), small);
  setHTML(btn, bhtml);
  const cn = "pill " + bcls;
  if (btn.className !== cn) btn.className = cn;
  btn.disabled = dis || S.rojo === null;
  btn.onclick = act;
}

function renderChrome() {
  $("#refresh").classList.toggle("spinning", !!S.loading);
  $("#banner").hidden = !S.banner;
  if (S.banner) {
    const t = installText();
    $("#banner-title").textContent = t.title; $("#banner-text").textContent = t.text; $("#banner-add").textContent = t.button;
  }
  $("#private-hint").hidden = !S.public_only;
  $("#ver").textContent = "Version " + S.version;
  const u = S.update && !S.updating;
  if ($("#upd").hidden === !!u) $("#upd").hidden = !u;
  if (u) setText($("#upd-ver"), "Version " + S.update.version);
  if (!restarting) showUpdating(!!S.updating);
}

/* ---------- actions */
function doPull() {
  const r = sel && repo(sel);
  if (!r || S.busy[r.name] || !S.git) return;
  S.busy[r.name] = "pull"; delete fx[r.name];
  hold[r.name] = Date.now() + 650;  // keep the progress visible long enough to read, even on instant pulls
  renderDetail(); renderList();
  api("pull", { repo: r.name });
}
function startRepo(name) {
  S.busy[name] = "start";
  if (name === sel) renderDetail();
  api("start", { repo: name });
}
function doStart() {
  const r = sel && repo(sel);
  if (!r) return;
  if (S.busy[r.name] === "pull") { pendingStart[r.name] = true; renderDetail(); return; }
  if (S.busy[r.name]) return;
  startRepo(r.name);
}
function doStop() {
  stopping = true; renderDetail(); api("stop");
  setTimeout(() => { if (stopping) { stopping = false; renderDetail(); } }, 8000);
}
function rojoAction() { const b = $("#rojo-btn"); if (!b.disabled && b.onclick) b.onclick(); }

function onResult(ev) {
  if (ev.op === "pull" && hold[ev.repo] > Date.now()) { setTimeout(() => onResult(ev), hold[ev.repo] - Date.now() + 10); return; }
  if (ev.op === "pull") {
    const label = !ev.ok ? "Couldn't pull" : ev.count === -1 ? "Downloaded" : ev.count > 0 ? "Updated" : "Up to date";
    const small = ev.ok ? (ev.count === -1 ? "Ready to start Rojo" : ev.message) : ev.message;
    fx[ev.repo] = { ok: ev.ok, label, small, until: Date.now() + 2600 };
    delete hold[ev.repo];
    setTimeout(() => { if (sel === ev.repo) renderDetail(); }, 2650);
    const queued = pendingStart[ev.repo];
    delete pendingStart[ev.repo];
    if (ev.ok && queued) startRepo(ev.repo);
    if (sel === ev.repo) renderDetail();
    if (!ev.ok) toast(ev.kind || "error", ev.message, true);
    else if (ev.repo !== sel) toast("ok", ev.repo + ": " + ev.message);
  } else if (ev.op === "start") {
    if (!ev.ok) toast("error", ev.message, true);
  } else if (ev.op === "rojo" || ev.op === "refresh") {
    toast("error", ev.message, true);
  } else if (ev.op === "update_check") {
    $("#chk").disabled = false; setText($("#chk"), "Check for updates");
    toast(ev.ok ? "ok" : "warn", ev.message);
  } else if (ev.op === "update") {
    showUpdating(false);
    toast(ev.ok ? "ok" : "error", ev.message, !ev.ok);
  }
}

/* ---------- updates */
function showUpdating(on) {
  if (document.body.classList.contains("updating") === on) return;
  document.body.classList.toggle("updating", on);
  if (on) { setText($("#upd-title"), "Updating Rojo Launcher…"); setText($("#upd-note"), "This only takes a few seconds"); }
}
function doUpdate() {
  if (!S || !S.update || S.updating) return;
  S.updating = true; renderChrome();
  api("apply_update").then(r => { if (!r.ok) { S.updating = false; renderChrome(); toast("error", r.message || "The update didn't start"); } });
}
function onRestarting() {
  // The old version is closing. The connection comes back by itself once the new one is up, and then the page reloads.
  restarting = Date.now();
  showUpdating(true);
  setText($("#upd-note"), "Almost done, reopening");
  setTimeout(() => {
    if (restarting && Date.now() - restarting >= 29000) {
      setText($("#upd-title"), "Couldn't reopen by itself");
      setText($("#upd-note"), "Close this window and open Rojo Launcher again");
      $("#updating .bar").hidden = true;
    }
  }, 30000);
}
function checkUpdates() {
  const b = $("#chk"); if (b.disabled) return;
  b.disabled = true; setText(b, "Checking…");
  api("check_update").then(r => { if (!r.ok) { b.disabled = false; setText(b, "Check for updates"); } });
}

/* ---------- add / remove repos */
function openAdd() {
  if (document.body.classList.contains("settings")) closeSettings();
  $("#add-in").value = ""; setText($("#add-err"), ""); setAddBusy(false);
  document.body.classList.add("adding");
  setTimeout(() => $("#add-in").focus(), 60);
}
function closeAdd() { document.body.classList.remove("adding"); $("#add-in").blur(); }
function setAddBusy(on) {
  const b = $("#add-ok"); b.disabled = on; $("#add-in").disabled = on;
  setHTML(b, on ? SPIN + "Adding" : "Add");
}
function submitAdd() {
  const spec = $("#add-in").value.trim();
  if ($("#add-ok").disabled) return;
  if (!spec) { failAdd("Type owner/repo, or paste the repo's GitHub link."); return; }
  setAddBusy(true); setText($("#add-err"), "");
  api("add_repo", { spec }).then(r => {
    setAddBusy(false);
    if (!r.ok) { failAdd(r.message || "Couldn't add that repo."); return; }
    closeAdd();
    $("#q").value = "";
    if (repo(r.name)) { renderList(); select(r.name); } else pendingSelect = r.name;
    toast("ok", "Added " + r.name + " to your Favorites");
  });
}
function failAdd(msg) {
  setText($("#add-err"), msg);
  const d = $(".dialog"); d.classList.remove("shake"); void d.offsetWidth; d.classList.add("shake");
  setTimeout(() => $("#add-in").focus(), 30);
}
function removeRepo() {
  const r = repo(sel); if (!r || !r.extra) return;
  api("remove_repo", { repo: r.name }).then(res => { if (res.ok) toast("ok", "Removed " + r.name + " from your list"); });
}

/* ---------- toasts */
function toast(kind, msg, details) {
  const box = $("#toasts");
  while (box.children.length > 2) box.firstChild.remove();
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = '<span class="ti">' + (kind === "ok" ? I.check : kind === "warn" ? I.alert : I.x) + '</span><span class="tm"></span>';
  t.querySelector(".tm").textContent = msg;
  const close = () => { if (t._c) return; t._c = 1; t.classList.add("out"); setTimeout(() => t.remove(), 260); };
  if (details) { const b = document.createElement("button"); b.textContent = "Details"; b.onclick = () => { openActivity(true); close(); }; t.append(b); }
  const x = document.createElement("button"); x.innerHTML = I.x; x.title = "Dismiss"; x.onclick = close; t.append(x);
  box.append(t);
  let timer = setTimeout(close, kind === "ok" ? 2800 : 7000);
  t.onmouseenter = () => clearTimeout(timer);
  t.onmouseleave = () => { timer = setTimeout(close, 2500); };
}

/* ---------- activity */
function addLog(ev, quiet) {
  const log = $("#log");
  const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  const line = document.createElement("div");
  if (ev.kind === "info") line.innerHTML = '<span class="info"></span>';
  else line.innerHTML = '<span class="t">' + esc(ev.t) + '</span><span class="' + ev.kind + '"></span>';
  line.lastChild.textContent = ev.text;
  log.append(line);
  while (log.children.length > 400) log.firstChild.remove();
  if (atEnd) log.scrollTop = log.scrollHeight;
  if (!quiet && ev.kind === "err" && !$("#activity").classList.contains("open")) $("#activity").classList.add("unread");
}
function openActivity(open) {
  const a = $("#activity");
  a.classList.toggle("open", open === undefined ? !a.classList.contains("open") : open);
  a.classList.remove("unread");
  if (a.classList.contains("open")) setTimeout(() => { const l = $("#log"); l.scrollTop = l.scrollHeight; }, 50);
}

/* ---------- settings */
function openSettings() {
  const s = S.settings;
  $("#f-user").value = s.github_user; $("#f-ws").value = s.workspace; $("#f-port").value = s.port;
  $("#f-auto").checked = s.auto_start; $("#f-token").value = "";
  $("#f-token").placeholder = s.has_token ? "Saved ✓  (paste a new one to replace it)" : "Optional, lets you see private repos";
  $("#token-note").innerHTML = s.has_token
    ? '<button id="tok-clear">Remove saved token</button>'
    : 'Classic token with “repo” ticked. <button id="tok-make">Make one</button>';
  const c = $("#tok-clear"), m = $("#tok-make");
  if (c) c.onclick = () => { api("settings", { clear_token: true }); closeSettings(); toast("ok", "Token removed"); };
  if (m) m.onclick = () => api("open_url", { url: "https://github.com/settings/tokens/new?scopes=repo&description=Rojo%20Launcher" });
  const it = installText();
  $("#f-install-label").textContent = it.label;
  setHTML($("#f-install"), I.monitor + it.setting);
  document.body.classList.add("settings");
}
function closeSettings() { document.body.classList.remove("settings"); if (document.activeElement) document.activeElement.blur(); }
function saveSettings() {
  api("settings", { github_user: $("#f-user").value, workspace: $("#f-ws").value, port: $("#f-port").value,
    auto_start: $("#f-auto").checked, token: $("#f-token").value }).then(() => toast("ok", "Settings saved"));
  closeSettings();
}
function installText() {
  if (S.mac_app) return { title: "Move it to Applications", text: "Then it opens from Launchpad, Spotlight and the Dock like any app.",
    button: "Move to Applications", label: "App", setting: S.installed ? "Show in Finder" : "Move to Applications" };
  if (S.platform === "mac") return { title: "Open it like a normal app", text: "Add Rojo Launcher to your Applications.",
    button: "Add to Applications", label: "Shortcut", setting: S.installed ? "Add to Applications again" : "Add to Applications" };
  return { title: "Open it like a normal app", text: "Add Rojo Launcher to your Desktop and Start menu.",
    button: "Add to Desktop", label: "Shortcut", setting: S.installed ? "Add to Desktop again" : "Add to Desktop & Start menu" };
}
function install() {
  if (S.mac_app && S.installed) { api("reveal_app"); return; }
  api("install").then(r => toast(r.ok ? "ok" : "error", r.message || "Done", !r.ok));
}

/* ---------- wiring */
$("#pull").onclick = doPull;
$("#restart").onclick = doStart;
$("#refresh").onclick = () => api("refresh");
$("#gear").onclick = openSettings;
$("#add-btn").onclick = openAdd;
$("#add-ok").onclick = submitAdd;
$("#add-cancel").onclick = closeAdd;
$("#add").addEventListener("mousedown", (e) => { if (e.target.id === "add") closeAdd(); });
$("#l-remove").onclick = removeRepo;
$("#upd-btn").onclick = doUpdate;
$("#chk").onclick = checkUpdates;
$("#sheet-x").onclick = closeSettings; $("#sheet-cancel").onclick = closeSettings; $("#scrim").onclick = closeSettings;
$("#sheet-save").onclick = saveSettings;
$("#f-browse").onclick = () => api("browse").then(r => { if (r.path) $("#f-ws").value = r.path; });
$("#f-install").onclick = install;
$("#banner-add").onclick = install;
$("#banner-x").onclick = () => api("dismiss_banner");
$("#private-hint").onclick = openSettings;
$("#act-toggle").onclick = () => openActivity();
$("#star").onclick = () => {
  const r = repo(sel); if (!r) return;
  r.favorite = !r.favorite; renderDetail(); renderList();
  const s = $("#star"); s.classList.remove("pop"); void s.offsetWidth; s.classList.add("pop");
  api("favorite", { repo: r.name });
};
document.querySelectorAll(".links button[data-open]").forEach(b => b.onclick = () => {
  api("open", { repo: sel, what: b.dataset.open }).then(r => { if (!r.ok && r.message) toast("warn", r.message); });
});
$("#q").addEventListener("input", () => {
  renderList();
  if (visible.length && !visible.includes(sel)) select(visible[0], { scroll: false, repeat: true });
  $("#list").scrollTop = 0;
});
document.addEventListener("keydown", (e) => {
  if (document.body.classList.contains("updating")) { e.preventDefault(); return; }
  if (document.body.classList.contains("adding")) {
    if (e.key === "Escape") closeAdd();
    else if (e.key === "Enter") { e.preventDefault(); submitAdd(); }
    return;
  }
  const inSettings = document.body.classList.contains("settings");
  if (inSettings) {
    if (e.key === "Escape") closeSettings();
    else if (e.key === "Enter" && e.target.tagName === "INPUT") saveSettings();
    return;
  }
  const mod = e.ctrlKey || e.metaKey;
  if ((mod && e.key.toLowerCase() === "f") || (e.key === "/" && e.target.tagName !== "INPUT")) { e.preventDefault(); $("#q").focus(); $("#q").select(); return; }
  if (e.key === "F5" || (mod && e.key.toLowerCase() === "r")) { e.preventDefault(); api("refresh"); return; }
  if (mod && e.key.toLowerCase() === "n") { e.preventDefault(); openAdd(); return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); move(e.key === "ArrowDown" ? 1 : -1, e.repeat); return; }
  if (e.key === "Enter") { e.preventDefault(); if (e.repeat) return; if (mod) rojoAction(); else doPull(); return; }
  if (e.key === "Escape") { if ($("#q").value) { $("#q").value = ""; renderList(); } $("#q").blur(); }
});
document.addEventListener("keyup", (e) => { if ((e.key === "ArrowDown" || e.key === "ArrowUp") && quick) { quick = false; } });
document.addEventListener("contextmenu", (e) => { if (e.target.tagName !== "INPUT" && !e.target.closest("#log")) e.preventDefault(); });
let sizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(sizeTimer);
  sizeTimer = setTimeout(() => api("size", { w: window.outerWidth, h: window.outerHeight }), 700);
  moveInd(false);
});
window.addEventListener("pagehide", () => navigator.sendBeacon("/api/bye?t=" + encodeURIComponent(T)));
setInterval(() => { if (S && sel) loadInfo(sel); }, 120000);
connect();
</script>
</body>
</html>
"""


# App icon (embedded so the app stays a single file)
ICON_PNG = """
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
"""
ICON_ICO = """
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
"""

if __name__ == "__main__":
    main()
