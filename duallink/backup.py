"""Google Drive backup through rclone, with the upload spread over both links.

DualLink works out which files are new or changed, groups them into batches,
and keeps one rclone process per link taking batches from a shared queue. Each
process is bound to its link's IP. Biggest batches go first so both links
finish at about the same time. A single file always travels over one link.
"""
import datetime
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import deque

CREATE_NO_WINDOW = 0x08000000
BATCH_BYTES = 512 << 20
BATCH_FILES = 200
MTIME_SLACK = 1.0  # seconds
MAX_BATCH_TRIES = 3
OFFLINE_LIMIT = 120  # seconds with neither link before giving up


class BackupError(Exception):
    pass


class Batch:
    def __init__(self, src, dest, files, size):
        self.src, self.dest, self.files, self.bytes = src, dest, files, size
        self.tries = 0


def scan_local(root):
    """{relative/path: (size, mtime)} for every regular file under root."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue  # rclone skips links too
            try:
                st = os.stat(full)
            except OSError:
                continue
            out[os.path.relpath(full, root).replace(os.sep, "/")] = (st.st_size, st.st_mtime)
    return out


def parse_rclone_time(text):
    m = re.match(r"(.*?\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", text)
    if not m:
        raise ValueError(f"Unexpected time from rclone: {text}")
    base, frac, tz = m.groups()
    frac = (frac or "")[:7]  # Python takes at most microseconds
    tz = "+00:00" if tz in (None, "Z") else tz
    return datetime.datetime.fromisoformat(base + frac + tz).timestamp()


def changed_files(local, remote):
    """Files missing from Drive or differing in size or modified time.
    That's the same test rclone copy uses, without hashing every file."""
    out = []
    for rel, (size, mtime) in sorted(local.items()):
        r = remote.get(rel)
        if r is None or r[0] != size or abs(r[1] - mtime) > MTIME_SLACK:
            out.append((rel, size))
    return out


def make_batches(src, dest, files):
    batches, cur, cur_bytes = [], [], 0
    for rel, size in files:
        if cur and (cur_bytes + size > BATCH_BYTES or len(cur) >= BATCH_FILES):
            batches.append(Batch(src, dest, cur, cur_bytes))
            cur, cur_bytes = [], 0
        cur.append(rel)
        cur_bytes += size
    if cur:
        batches.append(Batch(src, dest, cur, cur_bytes))
    return batches


def remote_path(remote, name):
    remote = remote.rstrip("/")
    return remote + name if remote.endswith(":") else f"{remote}/{name}"


def parse_log_line(line):
    """rclone --use-json-log line -> (stats dict or None, error message or None)."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None, None
    if not isinstance(obj, dict):
        return None, None
    err = obj.get("msg") if obj.get("level") in ("error", "critical") else None
    return obj.get("stats"), err


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        raise BackupError("rclone isn't installed (or rclone_path in the settings is wrong).")


def list_remote(rclone, dest):
    proc = _run([rclone, "lsjson", "-R", "--files-only", "--no-mimetype", dest])
    if proc.returncode == 3:  # folder doesn't exist on Drive yet
        return {}
    if proc.returncode != 0:
        raise BackupError(_last_line(proc.stderr) or f"rclone lsjson exited {proc.returncode}")
    return {e["Path"]: (e["Size"], parse_rclone_time(e["ModTime"]))
            for e in json.loads(proc.stdout or "[]")}


def _last_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


class BackupJob(threading.Thread):
    def __init__(self, cfg, sources):
        super().__init__(daemon=True, name="Backup")
        self.cfg = cfg
        self.sources = sources  # callable -> {link id: ip or None}
        self.rclone = cfg["rclone_path"]
        self.status = "Starting"
        self.phase = ""
        self.total_bytes = 0
        self.done_bytes = 0
        self.files_total = 0
        self.files_done = 0
        self.live_bytes = {}  # link id -> bytes of the batch uploading there now
        self.speed = {}  # link id -> bytes/s
        self.problems = []
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._procs = set()

    @property
    def finished(self):
        return not self.is_alive() and self.status != "Starting"

    def progress_bytes(self):
        with self._lock:
            return self.done_bytes + sum(self.live_bytes.values())

    def stop(self):
        self._halt.set()
        with self._lock:
            for p in list(self._procs):
                p.terminate()

    def run(self):
        try:
            self._run()
        except BackupError as e:
            self.status = "Failed"
            self.problems.append(str(e))
        except Exception as e:  # keep the app alive; show what happened
            self.status = "Failed"
            self.problems.append(f"Unexpected error: {e!r}")

    def _run(self):
        self.status = "Checking"
        self._check_remote()
        batches = []
        for src in self.cfg["backup_sources"]:
            if self._halt.is_set():
                break
            if not os.path.isdir(src):
                self.problems.append(f"Skipped missing folder: {src}")
                continue
            name = os.path.basename(os.path.normpath(src)) or "Drive"
            dest = remote_path(self.cfg["backup_remote"], name)
            self.phase = f"Comparing {name} with Google Drive..."
            changed = changed_files(scan_local(src), list_remote(self.rclone, dest))
            batches += make_batches(src, dest, changed)
        if self._halt.is_set():
            self.status, self.phase = "Stopped", ""
            return
        batches.sort(key=lambda b: b.bytes, reverse=True)
        self.total_bytes = sum(b.bytes for b in batches)
        self.files_total = sum(len(b.files) for b in batches)
        if not batches:
            self.status, self.phase = "Up to date", "Nothing new to upload."
            return
        self.status, self.phase = "Uploading", ""
        queue = deque(batches)
        state = {"in_flight": 0, "offline_since": None}
        if not self.sources():
            raise BackupError("No network links found.")
        # One rclone at a time per link; a link plugged in mid-backup joins in.
        threads = {}
        while True:
            if not self._halt.is_set():
                for link, ip in self.sources().items():
                    if link not in threads and (ip or not threads):
                        t = threading.Thread(target=self._worker, args=(link, queue, state),
                                             daemon=True)
                        threads[link] = t
                        t.start()
            if threads and not any(t.is_alive() for t in threads.values()):
                break
            time.sleep(0.5)
        if self._halt.is_set():
            self.status = "Stopped"
        elif self.problems:
            self.status = "Finished with problems"
        else:
            self.status = "Done"

    def _check_remote(self):
        proc = _run([self.rclone, "listremotes"])
        if proc.returncode != 0:
            raise BackupError(_last_line(proc.stderr) or "rclone listremotes failed")
        name = self.cfg["backup_remote"].split(":", 1)[0] + ":"
        if name not in proc.stdout.split():
            raise BackupError(f"Google Drive isn't connected (no rclone remote called {name}). "
                              "Click Set up Google Drive at the top of the Backup tab.")

    def _worker(self, role, queue, state):
        while not self._halt.is_set():
            with self._lock:
                if not queue and state["in_flight"] == 0:
                    return
                ip = self.sources().get(role)
                b = queue.popleft() if queue and ip else None
                if b:
                    state["in_flight"] += 1
            if b is None:
                if any(self.sources().values()):
                    state["offline_since"] = None
                elif state["offline_since"] is None:
                    state["offline_since"] = time.monotonic()
                elif time.monotonic() - state["offline_since"] > OFFLINE_LIMIT:
                    self.problems.append("Both links were offline for two minutes; stopped.")
                    self._halt.set()
                time.sleep(0.5)
                continue
            ok, error = self._upload(role, ip, b)
            with self._lock:
                state["in_flight"] -= 1
                if ok:
                    self.done_bytes += b.bytes
                    self.files_done += len(b.files)
                elif self._halt.is_set():
                    pass
                elif self.sources().get(role) != ip and b.tries < MAX_BATCH_TRIES:
                    b.tries += 1
                    queue.append(b)  # the link dropped; let either link redo it
                else:
                    self.problems.append(
                        f"{len(b.files)} file(s) from {os.path.basename(b.src)} hit an error "
                        f"({error or 'rclone failed'}). Run the backup again to retry them.")

    def _upload(self, role, ip, b):
        fd, listfile = tempfile.mkstemp(prefix="duallink-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(b.files) + "\n")
        cmd = [self.rclone, "copy", b.src, b.dest,
               "--files-from-raw", listfile, "--no-traverse", "--bind", ip,
               "--transfers", str(self.cfg.get("rclone_transfers", 4)),
               "--drive-chunk-size", "32M", "--retries", "3",
               "--use-json-log", "--stats", "1s", "-v"]
        error = None
        try:
            with self._lock:
                if self._halt.is_set():
                    return False, None
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=CREATE_NO_WINDOW)
                self._procs.add(proc)
            for line in proc.stderr:
                stats, err = parse_log_line(line)
                if stats:
                    with self._lock:
                        self.live_bytes[role] = min(int(stats.get("bytes", 0)), b.bytes)
                        self.speed[role] = float(stats.get("speed") or 0)
                if err:
                    error = err
            rc = proc.wait()
        except OSError as e:
            return False, f"couldn't start rclone: {e}"
        finally:
            with self._lock:
                self._procs = {p for p in self._procs if p.poll() is None}
                self.live_bytes[role] = 0
                self.speed[role] = 0.0
            try:
                os.remove(listfile)
            except OSError:
                pass
        return rc == 0, error
