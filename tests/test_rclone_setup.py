import hashlib
import io
import os
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from duallink import rclone_setup

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = "v1.75.2"
ZIP_NAME = f"rclone-{TAG}-windows-amd64.zip"
EXE = b"MZ fake rclone executable"


def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"rclone-{TAG}-windows-amd64/rclone.exe", EXE)
        z.writestr(f"rclone-{TAG}-windows-amd64/README.txt", "hi")
    return buf.getvalue()


def serve(files):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = files.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def downloads_site(zip_bytes, published_hash=None):
    published_hash = published_hash or hashlib.sha256(zip_bytes).hexdigest()
    sums = ("-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA1\n\n"
            f"{'0' * 64}  rclone-{TAG}-linux-amd64.zip\n"
            f"{published_hash}  {ZIP_NAME}\n"
            "-----BEGIN PGP SIGNATURE-----\nxyz\n-----END PGP SIGNATURE-----\n")
    return serve({"/version.txt": f"rclone {TAG}\n".encode(),
                  f"/{TAG}/SHA256SUMS": sums.encode(),
                  f"/{TAG}/{ZIP_NAME}": zip_bytes})


def test_install_downloads_verifies_and_unpacks(tmp_path):
    server, base = downloads_site(make_zip())
    seen = []
    try:
        path = rclone_setup.install(lambda done, total: seen.append((done, total)),
                                    base_url=base, dest=tmp_path / "rclone" / "rclone.exe")
    finally:
        server.shutdown()
    assert open(path, "rb").read() == EXE
    assert seen and seen[-1][0] == seen[-1][1]
    assert sorted(os.listdir(tmp_path / "rclone")) == ["rclone.exe"]  # no zip left behind


def test_install_refuses_download_that_fails_checksum(tmp_path):
    server, base = downloads_site(make_zip(), published_hash="a" * 64)
    try:
        with pytest.raises(rclone_setup.SetupError, match="checksum"):
            rclone_setup.install(base_url=base, dest=tmp_path / "rclone" / "rclone.exe")
    finally:
        server.shutdown()
    assert os.listdir(tmp_path / "rclone") == []


def test_find_rclone(tmp_path, monkeypatch):
    monkeypatch.setattr(rclone_setup.shutil, "which", lambda p: None)
    monkeypatch.setattr(rclone_setup, "APP_RCLONE", tmp_path / "app" / "rclone.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert rclone_setup.find_rclone({"rclone_path": "rclone"}) is None
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "rclone.exe").write_bytes(b"x")
    assert rclone_setup.find_rclone({"rclone_path": "rclone"}) == str(tmp_path / "app" / "rclone.exe")
    own = tmp_path / "mine.exe"
    own.write_bytes(b"x")
    assert rclone_setup.find_rclone({"rclone_path": str(own)}) == str(own)


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    cmd = tmp_path / "rclone.cmd"
    cmd.write_text(f'@"{sys.executable}" "{os.path.join(HERE, "fake_rclone.py")}" %*\n')
    monkeypatch.setenv("FAKE_DRIVE_ROOT", str(tmp_path))
    (tmp_path / "remotes.txt").write_text("")  # nothing configured yet
    return str(cmd)


def test_connect_then_about(fake_rclone, tmp_path):
    assert rclone_setup.remotes(fake_rclone) == []
    rclone_setup.connect_drive(fake_rclone, "gdrive")
    assert rclone_setup.remotes(fake_rclone) == ["gdrive"]
    assert (tmp_path / "created.log").read_text().split() == ["gdrive", "drive", "scope=drive"]
    info = rclone_setup.about(fake_rclone, "gdrive")
    assert info["free"] == 1_500_000_000_000


def test_reconnect_replaces_the_remote(fake_rclone):
    rclone_setup.connect_drive(fake_rclone, "gdrive")
    rclone_setup.connect_drive(fake_rclone, "gdrive")
    assert rclone_setup.remotes(fake_rclone) == ["gdrive"]


def test_cancelled_signin_is_reported(fake_rclone, monkeypatch):
    monkeypatch.setenv("FAKE_SIGNIN", "fail")
    with pytest.raises(rclone_setup.SetupError, match="auth cancelled"):
        rclone_setup.connect_drive(fake_rclone, "gdrive")


def test_about_unconnected_remote_is_an_error(fake_rclone):
    with pytest.raises(rclone_setup.SetupError):
        rclone_setup.about(fake_rclone, "gdrive")


def test_remote_name():
    assert rclone_setup.remote_name("gdrive:PC Backup") == "gdrive"
