"""Split HTTP downloads that pull from both links at once.

Each connection streams one long byte range of the file, bound to its link's
IP. When a connection finishes its range it takes over the back half of the
biggest range still in progress, so the faster link ends up doing more and all
connections finish together. Long ranges matter: some servers wait most of a
second before answering each new request, so many small requests waste most
of the time.

Progress (the ranges still missing) is saved next to the file, so a paused or
interrupted download resumes where it stopped.
"""
import html
import json
import os
import re
import shutil
import threading
import time
import urllib.parse

import requests
from requests.adapters import HTTPAdapter

QUEUED, RUNNING, PAUSED, DONE, FAILED, CANCELLED, DELETED = (
    "Queued", "Downloading", "Paused", "Done", "Failed", "Cancelled", "Deleted")
DEFAULT_WORKERS = {"ethernet": 3, "tether": 6, "wifi": 4}
PART_SUFFIX = ".duallink-part"
META_SUFFIX = ".duallink.json"
MAX_FAIL_STREAK = 12  # failures in a row, on any link, before giving up
MAX_LINK_STRIKES = 4  # failures in a row on one link before it sits out this download


class BoundAdapter(HTTPAdapter):
    def __init__(self, source_ip, **kwargs):
        self.source_ip = source_ip  # must be set before HTTPAdapter builds its pool
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = (self.source_ip, 0)
        super().init_poolmanager(*args, **kwargs)


def bound_session(ip):
    s = requests.Session()
    s.trust_env = False  # a system proxy would bypass the chosen link
    adapter = BoundAdapter(ip)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers["User-Agent"] = "DualLink/1.0"
    return s


def filename_for(url, headers):
    cd = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I)
    name = urllib.parse.unquote(m.group(1).strip().strip('"')) if m else None
    if not name:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        name = m.group(1).strip() if m else None
    if not name:
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return name or "download"


def unique_path(path):
    """A path that isn't taken by a finished file. An unfinished download of the
    same name keeps its name so it can resume."""
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


def _web_page_error(r):
    """A message if the server answered with a web page (a quota, sign-in or
    error page) instead of the file, else None. A page sent as an attachment is
    a real download and passes."""
    ctype = r.headers.get("Content-Type", "").lower()
    if "text/html" not in ctype or "attachment" in r.headers.get("Content-Disposition", "").lower():
        return None
    try:
        text = r.raw.read(65536, decode_content=True).decode("utf-8", "replace")
    except Exception:
        text = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title = " ".join(html.unescape(m.group(1)).split()) if m else ""
    return ("The server sent a web page instead of the file"
            + (f' ("{title}")' if title else "")
            + ". It may be a download limit, sign-in or error page.")


class _Stopped(Exception):
    pass


class _Busy(Exception):
    """Server said too many connections (HTTP 429/503)."""


class _WebPage(IOError):
    """Server sent a web page (quota, sign-in, error) instead of file data.
    Unlike a dropped connection, retrying the same link won't help soon."""


class _Range:
    """Bytes [pos, end) of the file still to fetch. `owner` is the worker
    streaming it, or None when it's free to take."""
    __slots__ = ("pos", "end", "owner")

    def __init__(self, pos, end):
        self.pos, self.end, self.owner = pos, end, None

    @property
    def left(self):
        return self.end - self.pos


def ranges_from_meta(meta, size):
    """Missing ranges from a saved progress file, or None if it can't be used."""
    if "remaining" in meta:
        return [_Range(a, b) for a, b in meta["remaining"] if 0 <= a < b <= size]
    if "done" in meta and "chunk" in meta:  # older DualLink: finished fixed-size pieces
        chunk, done, out = int(meta["chunk"]), set(meta["done"]), []
        for i in range(-(-size // chunk)):
            if i in done:
                continue
            a, b = i * chunk, min(size, (i + 1) * chunk)
            if out and out[-1].end == a:
                out[-1].end = b
            else:
                out.append(_Range(a, b))
        return out
    return None


class Download:
    def __init__(self, url, dest_dir, sources, chunk_size=4 << 20, workers=None):
        self.url = url
        self.dest_dir = dest_dir
        # callable -> {link id: ip or None}, e.g. {"ethernet": ..., "tether": ..., "tether 2": ...}
        self.sources = sources
        # Smallest range worth a new request; a range is only split in two when
        # both halves would be at least this big.
        self.chunk_size = chunk_size
        # Connections per link, by link id ("tether 2") or kind ("tether").
        self.workers = workers or DEFAULT_WORKERS
        self.path = None
        self.size = None
        self.split = None  # True when the server allows ranges over both links
        self.status = QUEUED
        self.error = ""
        self.done_bytes = 0
        self.link_bytes = {}  # link id -> bytes so far, for speed display
        self._stop_reason = None  # "pause" | "hold" | "cancel"
        self._lock = threading.Lock()

    @property
    def name(self):
        return os.path.basename(self.path) if self.path else self.url

    def request_stop(self, reason):
        self._stop_reason = reason

    def _stopping(self):
        return self._stop_reason is not None

    def _count(self, link, n):
        with self._lock:
            self.done_bytes += n
            self.link_bytes[link] = self.link_bytes.get(link, 0) + n

    def _workers_for(self, link):
        w = self.workers
        n = w.get(link, w.get(link.split()[0], DEFAULT_WORKERS.get(link.split()[0], 4)))
        return max(1, int(n))

    def _probe_any(self):
        """Ask about the file over each connected link in turn, ethernet last (it's
        the slowest), until one gets a real answer. A server may block one link's
        address (Google Drive's "Quota exceeded") while the others still work."""
        ips = self.sources()
        error = None
        for link in sorted(ips, key=lambda k: k == "ethernet"):
            if not ips[link]:
                continue
            try:
                return (link, ips[link]) + self._probe(ips[link])
            except (requests.RequestException, IOError, OSError) as e:
                error = e
        raise error or IOError("No link has an internet address right now.")

    def run(self):
        """Blocking. Leaves status as DONE, FAILED, PAUSED, QUEUED (held) or CANCELLED."""
        self.status, self.error = RUNNING, ""
        try:
            link, ip, final_url, ranged = self._probe_any()
            if ranged and self.size >= 2 * self.chunk_size:
                self.split = True
                self._run_split(final_url)
            else:
                self.split = False
                self._run_single(final_url, link, ip)
        except _Stopped:
            pass
        except (requests.RequestException, IOError, OSError) as e:
            self.status, self.error = FAILED, str(e)
            return
        reason = self._stop_reason
        if reason == "cancel":
            self._remove_partial()
            self.status = CANCELLED
        elif reason == "pause":
            self.status = PAUSED
        elif reason == "hold":
            self.status = QUEUED
        elif self.status == RUNNING:
            self.status = DONE

    def _probe(self, ip):
        with bound_session(ip) as s:
            r = s.get(self.url, headers={"Range": "bytes=0-0"}, stream=True,
                      timeout=(10, 30), allow_redirects=True)
            with r:
                page = _web_page_error(r)
                if page:
                    raise IOError(page)
                r.raise_for_status()
                total = None
                ranged = False
                m = re.match(r"bytes \d+-\d+/(\d+)", r.headers.get("Content-Range", ""))
                if r.status_code == 206 and m:
                    total, ranged = int(m.group(1)), True
                elif r.headers.get("Content-Length", "").isdigit():
                    total = int(r.headers["Content-Length"])
                if self.path is None:
                    os.makedirs(self.dest_dir, exist_ok=True)
                    self.path = unique_path(os.path.join(self.dest_dir, filename_for(r.url, r.headers)))
                self.size = total
                return r.url, ranged

    def _run_single(self, url, link, ip):
        """Server can't send ranges: one connection over the faster link, no resume."""
        part = self.path + PART_SUFFIX
        self.done_bytes = 0
        with bound_session(ip) as s, s.get(url, stream=True, timeout=(10, 30)) as r:
            page = _web_page_error(r)
            if page:
                raise IOError(page)
            r.raise_for_status()
            with open(part, "wb") as f:
                for block in r.iter_content(256 * 1024):
                    if self._stopping():
                        f.close()
                        os.remove(part)
                        self.done_bytes = 0
                        raise _Stopped()
                    f.write(block)
                    self._count(link, len(block))
        if self.size is not None and self.done_bytes != self.size:
            raise IOError(f"Connection ended early ({self.done_bytes} of {self.size} bytes).")
        os.replace(part, self.path)

    def _run_split(self, url):
        size = self.size
        part, meta_path = self.path + PART_SUFFIX, self.path + META_SUFFIX
        ranges = None
        meta = _read_json(meta_path)
        if meta and meta.get("url") == self.url and meta.get("size") == size and os.path.exists(part):
            ranges = ranges_from_meta(meta, size)
        if ranges is None:
            # No truncate(size): on Windows that writes zeros for the whole file.
            # Writes at any offset just extend the (sparse) file.
            with open(part, "wb") as f:
                _make_sparse(f)
            ranges = [_Range(0, size)]
        need = sum(r.left for r in ranges)
        free = shutil.disk_usage(os.path.dirname(part)).free
        if free < need:
            raise IOError(f"Not enough disk space: needs {need / 1e9:.1f} GB more, "
                          f"{free / 1e9:.1f} GB free.")

        lock = threading.Lock()
        state = {"fatal": None, "saved": time.monotonic(), "stuck_since": None,
                 "busy_streak": 0, "fail_streak": 0}
        link_ids = []  # grows if a link is plugged in mid-download
        strikes = {}
        benched = set()
        active = {}
        fh = open(part, "r+b")

        def left():
            return sum(r.left for r in ranges)

        self.done_bytes = size - left()

        def usable(link):
            # Caller holds lock.
            return link not in benched and active[link] > 0 and bool(self.sources().get(link))

        def save_meta(force=False):
            # Caller holds lock.
            if force or time.monotonic() - state["saved"] > 2:
                fh.flush()
                _write_json(meta_path, {"url": self.url, "size": size,
                                        "remaining": [[r.pos, r.end] for r in ranges if r.left]})
                state["saved"] = time.monotonic()

        def claim(me):
            # Caller holds lock. A free range if there is one, else the back half
            # of the biggest range another connection is still working on.
            ranges[:] = [r for r in ranges if r.left or r.owner]
            free = [r for r in ranges if r.owner is None and r.left]
            if free:
                r = max(free, key=lambda r: r.left)
                r.owner = me
                return r
            busy = [r for r in ranges if r.left >= 2 * self.chunk_size]
            if not busy:
                return None
            victim = max(busy, key=lambda r: r.left)
            mid = victim.pos + victim.left // 2
            mine = _Range(mid, victim.end)
            mine.owner = me
            victim.end = mid  # its owner stops when it reaches here
            ranges.append(mine)
            return mine

        def worker(link):
            me = object()
            session, session_ip = None, None
            retired = False  # True once this worker has taken itself out of `active`
            try:
                while not self._stopping() and not state["fatal"]:
                    with lock:
                        if link in benched or not left():
                            return
                        ip = self.sources().get(link)
                        rng = claim(me) if ip else None
                        if rng is None:
                            # Nothing can make progress (links offline or set aside):
                            # give up after a minute rather than wait forever.
                            if any(usable(r) for r in link_ids):
                                state["stuck_since"] = None
                            elif state["stuck_since"] is None:
                                state["stuck_since"] = time.monotonic()
                            elif time.monotonic() - state["stuck_since"] > 60:
                                state["fatal"] = "No link has been usable for a minute."
                    if rng is None:
                        time.sleep(0.3)
                        continue
                    if ip != session_ip:
                        if session:
                            session.close()
                        session, session_ip = bound_session(ip), ip
                    before = rng.pos
                    try:
                        self._stream(session, url, rng, fh, lock, link)
                    except _Stopped:
                        with lock:
                            rng.owner = None
                        return
                    except _Busy:
                        # The server caps connections per address. That's not the
                        # link's fault: this connection steps aside unless it's the
                        # download's last one, and the rest carry on.
                        with lock:
                            rng.owner = None
                            state["busy_streak"] += 1
                            if state["busy_streak"] > 40:
                                state["fatal"] = "The server kept refusing connections (too many requests)."
                            elif sum(active.values()) > 1:
                                active[link] -= 1
                                retired = True
                                return
                        time.sleep(min(2 ** min(state["busy_streak"], 4), 10))
                        continue
                    except _WebPage as e:
                        # This link's address is blocked (e.g. a quota page): take
                        # it out of this download now; stop if no link is left.
                        with lock:
                            rng.owner = None
                            benched.add(link)
                            if not any(usable(r) for r in link_ids):
                                state["fatal"] = str(e)
                            save_meta()
                        continue
                    except (requests.RequestException, IOError, OSError) as e:
                        with lock:
                            rng.owner = None  # bytes already written are kept
                            if rng.pos > before:
                                state["fail_streak"] = 0
                                strikes[link] = 0
                            state["fail_streak"] += 1
                            strikes[link] += 1
                            if state["fail_streak"] >= MAX_FAIL_STREAK:
                                state["fatal"] = f"Kept failing: {e}"
                            elif (strikes[link] >= MAX_LINK_STRIKES
                                  and any(usable(r) for r in link_ids if r != link)):
                                # Only set a link aside while the other one can take over.
                                benched.add(link)
                            save_meta()
                        time.sleep(min(2 ** strikes[link], 10))
                        continue
                    with lock:
                        rng.owner = None
                        state["busy_streak"] = state["fail_streak"] = strikes[link] = 0
                        save_meta()
            finally:
                if not retired:
                    with lock:
                        active[link] -= 1
                if session:
                    session.close()

        threads = []

        def add_link(link):
            with lock:
                link_ids.append(link)
                strikes[link] = 0
                active[link] = self._workers_for(link)
                new = [threading.Thread(target=worker, args=(link,), daemon=True)
                       for _ in range(active[link])]
            threads.extend(new)
            for t in new:
                t.start()

        try:
            for link in self.sources():
                add_link(link)
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
                if self._stopping() or state["fatal"]:
                    continue
                for link, ip in self.sources().items():
                    if ip and link not in link_ids and left():
                        add_link(link)  # e.g. a second phone just plugged in
            with lock:
                save_meta(force=True)
        finally:
            fh.close()
        if self._stopping():
            raise _Stopped()
        if state["fatal"]:
            raise IOError(state["fatal"])
        if left():
            raise IOError("Download stopped before every part arrived.")
        os.replace(part, self.path)
        os.remove(meta_path)

    def _stream(self, session, url, rng, fh, lock, link):
        """Stream rng until it's complete. Another connection may shorten rng.end
        meanwhile, so it's re-read under the lock for every block."""
        with lock:
            start, end = rng.pos, rng.end
        if start >= end:
            return
        with session.get(url, headers={"Range": f"bytes={start}-{end - 1}"},
                         stream=True, timeout=(10, 30)) as r:
            if r.status_code in (429, 503):
                raise _Busy()
            if r.status_code != 206:
                # e.g. Google Drive's "Quota exceeded" page for this link's address;
                # the link is set aside and the other links carry on.
                page = _web_page_error(r)
                if page:
                    raise _WebPage(page)
                raise IOError(f"Server answered {r.status_code} to a range request.")
            if not r.headers.get("Content-Range", "").startswith(f"bytes {start}-"):
                raise IOError("Server sent the wrong part of the file.")
            for block in r.iter_content(256 * 1024):
                if self._stopping():
                    raise _Stopped()
                with lock:
                    n = min(len(block), rng.end - rng.pos)
                    if n > 0:
                        fh.seek(rng.pos)
                        fh.write(block[:n] if n < len(block) else block)
                        rng.pos += n
                    finished = rng.pos >= rng.end
                if n > 0:
                    self._count(link, n)
                if finished:
                    return  # closing the response drops the rest of this request
        with lock:
            if rng.pos < rng.end:
                raise IOError("Connection ended early.")

    def _remove_partial(self):
        if not self.path:
            return
        for p in (self.path + PART_SUFFIX, self.path + META_SUFFIX):
            try:
                os.remove(p)
            except OSError:
                pass


def _make_sparse(f):
    """Mark a new file sparse. Connections write far into the file from the
    start, and without this NTFS would first zero-fill everything before that
    point (gigabytes of extra disk writes). Not fatal if it fails."""
    if os.name != "nt":
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    FSCTL_SET_SPARSE = 0x900C4
    returned = wintypes.DWORD()
    ctypes.windll.kernel32.DeviceIoControl(
        wintypes.HANDLE(msvcrt.get_osfhandle(f.fileno())), FSCTL_SET_SPARSE,
        None, 0, None, 0, ctypes.byref(returned), None)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class DownloadManager:
    """Runs downloads one at a time, so each one gets both links in full."""

    def __init__(self, cfg, sources):
        self.cfg = cfg
        self.sources = sources
        self.items = []
        self.current = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._allowed = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="Downloads").start()

    def add(self, url, dest_dir=None):
        d = Download(url, dest_dir or self.cfg["download_dir"], self.sources,
                     chunk_size=int(self.cfg["chunk_mb"]) << 20,
                     workers=self.cfg["download_workers"])
        with self._lock:
            self.items.append(d)
        self._wake.set()
        return d

    def set_allowed(self, allowed):
        """Off-air: downloads run. Live: the running one is held and resumes later."""
        with self._lock:
            if allowed:
                self._allowed.set()
                self._wake.set()
            else:
                self._allowed.clear()
                if self.current:
                    self.current.request_stop("hold")

    def pause(self, d):
        with self._lock:
            if d is self.current:
                d.request_stop("pause")
            elif d.status == QUEUED:
                d.status = PAUSED

    def resume(self, d):
        with self._lock:
            if d.status in (PAUSED, FAILED):
                d.status, d.error = QUEUED, ""
        self._wake.set()

    def cancel(self, d):
        with self._lock:
            if d is self.current:
                d.request_stop("cancel")
                return
            if d.status not in (QUEUED, PAUSED, FAILED):
                return
            d.status = CANCELLED
        d._remove_partial()

    def delete(self, d):
        """Stop the download if needed, delete its file (finished or partial) and
        drop it from the list. Returns an error message, or "" on success."""
        with self._lock:
            if d in self.items:
                self.items.remove(d)
            if d is self.current:
                d.request_stop("cancel")  # run() removes the partial file
                return ""
            finished = d.status == DONE
            d.status = DELETED
        if finished and d.path:
            try:
                os.remove(d.path)
            except FileNotFoundError:
                pass
            except OSError as e:
                return f"Couldn't delete {d.name}: {e.strerror or e}"
        d._remove_partial()
        return ""

    def clear_finished(self):
        with self._lock:
            self.items = [d for d in self.items if d.status not in (DONE, CANCELLED)]

    def _loop(self):
        while True:
            self._allowed.wait()
            with self._lock:
                nxt = None
                if self._allowed.is_set():
                    nxt = next((d for d in self.items if d.status == QUEUED), None)
                    if nxt:
                        # Cleared here, under the lock, so a Live switch that
                        # lands right now still stops this download.
                        nxt._stop_reason = None
                        self.current = nxt
            if nxt is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                nxt.run()
            finally:
                with self._lock:
                    self.current = None
