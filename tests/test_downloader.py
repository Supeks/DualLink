import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from duallink import downloader as dl

DATA = os.urandom(6 * 1024 * 1024 + 12345)
CHUNK = 512 * 1024
BOTH = {"ethernet": "127.0.0.1", "tether": "127.0.0.1"}


def make_server(ranges=True, delay=0.0, max_conn=None):
    busy = {"n": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            with lock:
                over = max_conn is not None and busy["n"] >= max_conn
                if not over:
                    busy["n"] += 1
            if over:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                self._serve()
            finally:
                with lock:
                    busy["n"] -= 1

        def _serve(self):
            m = re.match(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
            if ranges and m:
                start, end = int(m.group(1)), min(int(m.group(2)), len(DATA) - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
            else:
                start, end = 0, len(DATA) - 1
                self.send_response(200)
            body = DATA[start:end + 1]
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="big file.bin"')
            self.end_headers()
            for i in range(0, len(body), 64 * 1024):
                if delay:
                    time.sleep(delay)
                try:
                    self.wfile.write(body[i:i + 64 * 1024])
                except (ConnectionError, OSError):
                    return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/files/big%20file.bin"


@pytest.fixture
def server():
    s, url = make_server()
    yield url
    s.shutdown()


def read(path):
    with open(path, "rb") as f:
        return f.read()


def test_split_download_uses_both_links(server, tmp_path):
    d = dl.Download(server, str(tmp_path), lambda: BOTH, chunk_size=CHUNK,
                    workers={"ethernet": 2, "tether": 2})
    d.run()
    assert d.status == dl.DONE, d.error
    assert d.split is True
    assert os.path.basename(d.path) == "big file.bin"
    assert read(d.path) == DATA
    assert d.link_bytes["ethernet"] > 0 and d.link_bytes["tether"] > 0
    assert not os.path.exists(d.path + dl.PART_SUFFIX)
    assert not os.path.exists(d.path + dl.META_SUFFIX)


def test_three_links_all_take_part(server, tmp_path):
    three = {"ethernet": "127.0.0.1", "tether": "127.0.0.1", "tether 2": "127.0.0.1"}
    d = dl.Download(server, str(tmp_path), lambda: three, chunk_size=CHUNK,
                    workers={"ethernet": 2, "tether": 2})  # "tether 2" uses the tether count
    d.run()
    assert d.status == dl.DONE, d.error
    assert read(d.path) == DATA
    assert set(d.link_bytes) == set(three) and all(v > 0 for v in d.link_bytes.values())


def test_link_plugged_in_mid_download_joins(tmp_path):
    s, url = make_server(delay=0.05)  # slow enough to still be running when it joins
    sources = {"ethernet": "127.0.0.1", "tether": "127.0.0.1"}
    try:
        d = dl.Download(url, str(tmp_path), lambda: dict(sources), chunk_size=CHUNK,
                        workers={"ethernet": 1, "tether": 1})
        t = threading.Thread(target=d.run)
        t.start()
        while d.done_bytes < CHUNK:
            time.sleep(0.02)
        sources["tether 2"] = "127.0.0.1"  # second phone plugged in
        t.join(60)
    finally:
        s.shutdown()
    assert d.status == dl.DONE, d.error
    assert read(d.path) == DATA
    assert d.link_bytes.get("tether 2", 0) > 0


QUOTA_PAGE = (b"<!DOCTYPE html><html><head><title>Google Drive - Quota exceeded</title>"
              b"</head><body>Too many users have downloaded this file recently.</body></html>")


def make_quota_server(blocked=lambda client_ip, n: True, max_per_response=None):
    """Serves DATA, except requests where blocked(client ip, request number) is true
    get a 200 HTML quota page, like Google Drive does. max_per_response cuts each
    response short, forcing the client to make new requests."""
    count = {"n": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            with lock:
                count["n"] += 1
                n = count["n"]
            if blocked(self.client_address[0], n):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(QUOTA_PAGE)))
                self.end_headers()
                self.wfile.write(QUOTA_PAGE)
                return
            m = re.match(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
            start, end = (int(m.group(1)), min(int(m.group(2)), len(DATA) - 1)) if m else (0, len(DATA) - 1)
            self.send_response(206 if m else 200)
            if m:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Disposition", 'attachment; filename="video.mp4"')
            self.end_headers()
            stop = end + 1 if max_per_response is None else min(end + 1, start + max_per_response)
            try:
                for i in range(start, stop, 64 * 1024):
                    time.sleep(0.003)
                    self.wfile.write(DATA[i:min(i + 64 * 1024, stop)])
            except (ConnectionError, OSError):
                return
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/download?id=abc"


def test_quota_page_is_not_saved_as_the_file(tmp_path):
    s, url = make_quota_server()
    try:
        d = dl.Download(url, str(tmp_path), lambda: BOTH, chunk_size=CHUNK)
        d.run()
    finally:
        s.shutdown()
    assert d.status == dl.FAILED
    assert "web page instead of the file" in d.error
    assert "Google Drive - Quota exceeded" in d.error
    assert list(tmp_path.iterdir()) == []


def test_link_that_gets_quota_page_is_set_aside(tmp_path):
    # Google blocked one link's address; the other link finishes the download.
    s, url = make_quota_server(blocked=lambda ip, n: ip == "127.0.0.2")
    try:
        sources = lambda: {"ethernet": "127.0.0.1", "tether": "127.0.0.2"}
        d = dl.Download(url, str(tmp_path), sources, chunk_size=CHUNK,
                        workers={"ethernet": 2, "tether": 2})
        d.run()
    finally:
        s.shutdown()
    assert d.status == dl.DONE, d.error
    assert read(d.path) == DATA
    assert d.link_bytes.get("tether", 0) == 0


def test_quota_mid_download_fails_with_reason_and_keeps_progress(tmp_path):
    # Every link blocked after 4 requests; short responses force new requests.
    s, url = make_quota_server(blocked=lambda ip, n: n > 4, max_per_response=1 << 20)
    try:
        d = dl.Download(url, str(tmp_path), lambda: BOTH, chunk_size=CHUNK,
                        workers={"ethernet": 1, "tether": 1})
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        t.join(180)
    finally:
        s.shutdown()
    assert not t.is_alive(), "download hung"
    assert d.status == dl.FAILED
    assert "Quota exceeded" in d.error
    assert os.path.exists(d.path + dl.META_SUFFIX)  # resumes later from where it stopped


def test_html_file_offered_as_attachment_still_downloads(tmp_path):
    page = b"<html><head><title>My saved page</title></head></html>"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Disposition", 'attachment; filename="page.html"')
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        d = dl.Download(f"http://127.0.0.1:{server.server_port}/p", str(tmp_path), lambda: BOTH)
        d.run()
    finally:
        server.shutdown()
    assert d.status == dl.DONE, d.error
    assert read(d.path) == page


def test_server_without_ranges_uses_one_link(tmp_path):
    s, url = make_server(ranges=False)
    try:
        d = dl.Download(url, str(tmp_path), lambda: BOTH, chunk_size=CHUNK)
        d.run()
    finally:
        s.shutdown()
    assert d.status == dl.DONE, d.error
    assert d.split is False
    assert read(d.path) == DATA


def test_pause_then_resume_keeps_finished_pieces(tmp_path):
    s, url = make_server(delay=0.01)
    try:
        d = dl.Download(url, str(tmp_path), lambda: BOTH, chunk_size=CHUNK,
                        workers={"ethernet": 1, "tether": 1})
        t = threading.Thread(target=d.run)
        t.start()
        while d.done_bytes < 2 * CHUNK:
            time.sleep(0.02)
        d.request_stop("pause")
        t.join(10)
        assert d.status == dl.PAUSED
        assert os.path.exists(d.path + dl.META_SUFFIX)
        kept = d.done_bytes
        assert 0 < kept < len(DATA)

        d._stop_reason = None  # what the manager does before running it again
        d.run()
    finally:
        s.shutdown()
    assert d.status == dl.DONE, d.error
    assert read(d.path) == DATA


def test_broken_link_is_benched_and_other_link_finishes(server, tmp_path):
    # 192.0.2.1 is a documentation address this PC doesn't own, so binding fails.
    sources = lambda: {"ethernet": "192.0.2.1", "tether": "127.0.0.1"}
    d = dl.Download(server, str(tmp_path), sources, chunk_size=CHUNK,
                    workers={"ethernet": 2, "tether": 2})
    d.run()
    assert d.status == dl.DONE, d.error
    assert read(d.path) == DATA
    assert d.link_bytes.get("ethernet", 0) == 0


def test_server_connection_limit_shrinks_connections_instead_of_failing(tmp_path):
    # Like the real server that answered 429 to the 5th and 6th connection.
    s, url = make_server(delay=0.002, max_conn=3)
    try:
        d = dl.Download(url, str(tmp_path), lambda: BOTH, chunk_size=CHUNK,
                        workers={"ethernet": 3, "tether": 6})
        d.run()
    finally:
        s.shutdown()
    assert d.status == dl.DONE, d.error
    assert read(d.path) == DATA


def test_only_link_broken_fails_instead_of_hanging(server, tmp_path):
    # Ethernet offline and the tether can't connect: must end with an error.
    sources = lambda: {"ethernet": None, "tether": "192.0.2.1"}
    d = dl.Download(server, str(tmp_path), sources, chunk_size=CHUNK,
                    workers={"ethernet": 2, "tether": 2})
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    t.join(120)
    assert not t.is_alive(), "download hung"
    assert d.status == dl.FAILED


def test_long_ranges_not_one_request_per_piece(tmp_path):
    # The real server waited ~0.7 s before every response, so the number of
    # requests matters. 9 connections should need only a few dozen requests.
    requests_seen = []
    s, url = make_server()
    orig = s.RequestHandlerClass.do_GET

    def counting(handler):
        requests_seen.append(handler.headers.get("Range"))
        orig(handler)
    s.RequestHandlerClass.do_GET = counting
    try:
        d = dl.Download(url, str(tmp_path), lambda: BOTH, chunk_size=CHUNK,
                        workers={"ethernet": 3, "tether": 6})
        d.run()
    finally:
        s.shutdown()
    assert d.status == dl.DONE, d.error
    assert read(d.path) == DATA
    pieces_of_old_design = len(DATA) // CHUNK
    assert len(requests_seen) < pieces_of_old_design


def test_part_file_is_sparse(tmp_path):
    s, url = make_server(delay=0.01)
    try:
        d = dl.Download(url, str(tmp_path), lambda: BOTH, chunk_size=CHUNK,
                        workers={"ethernet": 1, "tether": 1})
        t = threading.Thread(target=d.run)
        t.start()
        while d.done_bytes == 0:
            time.sleep(0.02)
        attrs = os.stat(d.path + dl.PART_SUFFIX).st_file_attributes
        d.request_stop("cancel")
        t.join(10)
    finally:
        s.shutdown()
    FILE_ATTRIBUTE_SPARSE_FILE = 0x200
    assert attrs & FILE_ATTRIBUTE_SPARSE_FILE


def test_huge_file_starts_right_away(tmp_path):
    # Regression: pre-sizing a 25 GB file wrote 25 GB of zeros before downloading.
    size = 50_000_000_000

    class Huge(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            m = re.match(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
            start, end = int(m.group(1)), min(int(m.group(2)), size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Disposition", 'attachment; filename="huge.bin"')
            self.end_headers()
            zeros = bytes(64 * 1024)
            try:
                for _ in range(-(-(end - start + 1) // len(zeros))):
                    self.wfile.write(zeros)
            except (ConnectionError, OSError):
                return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Huge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        d = dl.Download(f"http://127.0.0.1:{server.server_port}/huge.bin", str(tmp_path),
                        lambda: BOTH, workers={"ethernet": 1, "tether": 2})
        t = threading.Thread(target=d.run, daemon=True)
        t.start()
        deadline = time.time() + 5
        while d.done_bytes < 20_000_000 and time.time() < deadline:
            time.sleep(0.05)
        progressed = d.done_bytes
        d.request_stop("cancel")
        t.join(15)
    finally:
        server.shutdown()
    assert progressed >= 20_000_000, "download didn't start promptly"
    assert d.status == dl.CANCELLED
    assert not os.path.exists(d.path + dl.PART_SUFFIX)


def test_resumes_from_old_progress_format(server, tmp_path):
    # A partial download saved by the earlier DualLink (fixed pieces) still resumes.
    d = dl.Download(server, str(tmp_path), lambda: BOTH, chunk_size=CHUNK)
    d.path = str(tmp_path / "big file.bin")
    part = d.path + dl.PART_SUFFIX
    with open(part, "wb") as f:
        f.truncate(len(DATA))
        f.seek(0)
        f.write(DATA[:2 * CHUNK])
    dl._write_json(d.path + dl.META_SUFFIX, {"url": server, "size": len(DATA),
                                             "chunk": CHUNK, "done": [0, 1]})
    d.run()
    assert d.status == dl.DONE, d.error
    assert read(d.path) == DATA
    assert sum(d.link_bytes.values()) == len(DATA) - 2 * CHUNK


def test_delete_removes_finished_file_and_partials(server, tmp_path):
    cfg = {"download_dir": str(tmp_path), "chunk_mb": 1,
           "download_workers": {"ethernet": 1, "tether": 1}}
    m = dl.DownloadManager(cfg, lambda: BOTH)
    m.set_allowed(True)
    d = m.add(server)
    deadline = time.time() + 20
    while d.status != dl.DONE and time.time() < deadline:
        time.sleep(0.1)
    assert os.path.exists(d.path)
    assert m.delete(d) == ""
    assert not os.path.exists(d.path)
    assert d not in m.items

    m.set_allowed(False)
    p = m.add(server)
    p.path = str(tmp_path / "partial.bin")
    open(p.path + dl.PART_SUFFIX, "wb").close()
    assert m.delete(p) == ""
    assert not os.path.exists(p.path + dl.PART_SUFFIX)


def test_existing_file_is_not_overwritten(server, tmp_path):
    (tmp_path / "big file.bin").write_bytes(b"keep me")
    d = dl.Download(server, str(tmp_path), lambda: BOTH, chunk_size=CHUNK)
    d.run()
    assert d.status == dl.DONE, d.error
    assert os.path.basename(d.path) == "big file (1).bin"
    assert (tmp_path / "big file.bin").read_bytes() == b"keep me"


def test_manager_holds_downloads_while_live(server, tmp_path):
    cfg = {"download_dir": str(tmp_path), "chunk_mb": 1,
           "download_workers": {"ethernet": 2, "tether": 2}}
    m = dl.DownloadManager(cfg, lambda: BOTH)
    d = m.add(server)
    time.sleep(0.5)
    assert d.status == dl.QUEUED  # not allowed yet (Live)
    m.set_allowed(True)
    deadline = time.time() + 20
    while d.status != dl.DONE and time.time() < deadline:
        time.sleep(0.1)
    assert d.status == dl.DONE, d.error
    assert read(d.path) == DATA


@pytest.mark.parametrize("url,header,expected", [
    ("http://x/a/file.zip", "", "file.zip"),
    ("http://x/a/my%20file.zip?x=1", "", "my file.zip"),
    ("http://x/dl", 'attachment; filename="report.pdf"', "report.pdf"),
    ("http://x/dl", "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf", "résumé.pdf"),
    ("http://x/", "", "download"),
    ("http://x/a:b?.txt", "", "a_b"),
])
def test_filename_for(url, header, expected):
    headers = {"Content-Disposition": header} if header else {}
    assert dl.filename_for(url, headers) == expected
