"""Getting rclone and Google Drive ready without a terminal.

install() downloads the official rclone build for Windows from
downloads.rclone.org, checks it against the SHA256SUMS file published next to
it, and unpacks rclone.exe into %APPDATA%\\DualLink\\rclone.

connect_drive() runs rclone's own Google sign-in: it opens your browser, you
log in to Google there and allow access, and Google hands rclone a token that
rclone keeps in its config file (%APPDATA%\\rclone\\rclone.conf). DualLink never
sees your password.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import requests

from . import config

BASE_URL = "https://downloads.rclone.org"
APP_RCLONE = config.APP_DIR / "rclone" / "rclone.exe"
CREATE_NO_WINDOW = 0x08000000


class SetupError(Exception):
    pass


def find_rclone(cfg):
    """Path to a working rclone.exe, or None. Checks the configured path, PATH,
    DualLink's own copy, then winget's install folder (which a running app's
    PATH doesn't pick up until it restarts)."""
    wanted = cfg.get("rclone_path") or "rclone"
    if os.path.isfile(wanted):
        return wanted
    found = shutil.which(wanted)
    if found:
        return found
    if APP_RCLONE.exists():
        return str(APP_RCLONE)
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "rclone.exe"
    return str(winget) if winget.exists() else None


def install(progress=None, base_url=BASE_URL, dest=APP_RCLONE):
    """Download and verify the latest rclone for 64-bit Windows. progress(done, total)
    is called while downloading. Returns the path to rclone.exe."""
    try:
        version = requests.get(f"{base_url}/version.txt", timeout=20).text.strip()
        m = re.fullmatch(r"rclone (v\d+\.\d+\.\d+)", version)
        if not m:
            raise SetupError(f"Unexpected rclone version info: {version[:60]!r}")
        tag = m.group(1)
        name = f"rclone-{tag}-windows-amd64.zip"
        sums = requests.get(f"{base_url}/{tag}/SHA256SUMS", timeout=20).text
        expected = next((line.split()[0].lower() for line in sums.splitlines()
                         if line.strip().endswith("  " + name) or line.strip().endswith(" " + name)), None)
        if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise SetupError(f"No checksum published for {name}.")

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, zip_path = tempfile.mkstemp(suffix=".zip", dir=dest.parent)
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as f, requests.get(f"{base_url}/{tag}/{name}",
                                                        stream=True, timeout=(15, 60)) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                for block in r.iter_content(256 * 1024):
                    f.write(block)
                    digest.update(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
            if digest.hexdigest() != expected:
                raise SetupError("The rclone download didn't match its published checksum, "
                                 "so it wasn't installed. Try again later.")
            member = f"rclone-{tag}-windows-amd64/rclone.exe"
            tmp_exe = str(dest) + ".new"
            with zipfile.ZipFile(zip_path) as z:
                if member not in z.namelist():
                    raise SetupError("rclone.exe wasn't in the download.")
                with z.open(member) as src, open(tmp_exe, "wb") as out:
                    shutil.copyfileobj(src, out)
            os.replace(tmp_exe, dest)
        finally:
            try:
                os.remove(zip_path)
            except OSError:
                pass
    except requests.RequestException as e:
        raise SetupError(f"Couldn't download rclone: {e}")
    return str(dest)


def _run(args, timeout=60):
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        raise SetupError("rclone isn't installed.")
    except subprocess.TimeoutExpired:
        raise SetupError("rclone took too long to answer.")


def _last_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def remote_name(backup_remote):
    """"gdrive:PC Backup" -> "gdrive"."""
    return backup_remote.split(":", 1)[0]


def remotes(rclone):
    proc = _run([rclone, "listremotes"])
    if proc.returncode != 0:
        raise SetupError(_last_line(proc.stderr) or "rclone listremotes failed")
    return [r.rstrip(":") for r in proc.stdout.split()]


def connect_drive(rclone, name, timeout=600):
    """Create (or recreate) a Google Drive remote. rclone opens the browser for
    the Google sign-in and waits until it's done."""
    if name in remotes(rclone):
        _run([rclone, "config", "delete", name])
    proc = _run([rclone, "config", "create", name, "drive", "scope=drive"], timeout=timeout)
    if proc.returncode != 0 or name not in remotes(rclone):
        raise SetupError(_last_line(proc.stderr) or "Google sign-in didn't finish.")


def about(rclone, name):
    """{"total", "used", "free"} in bytes (any may be missing) for the remote."""
    proc = _run([rclone, "about", f"{name}:", "--json"])
    if proc.returncode != 0:
        raise SetupError(_last_line(proc.stderr) or "Couldn't reach Google Drive.")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise SetupError("Unexpected answer from rclone about.")
