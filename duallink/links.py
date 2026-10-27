"""Find the two uplinks, make Windows prefer the tether, and measure traffic.

Routing model: the tether gets the lowest interface metric, so everything that
doesn't pick a link itself (games, browsers, launchers) uses the tether. OBS is
pinned to ethernet with its own "Bind to IP" setting. DualLink's downloads and
backups bind each connection to a link's IP, which Windows (strong host model)
sends out of the adapter that owns that IP.
"""
import configparser
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import psutil

from . import config

TETHER_HINTS = ("remote ndis", "rndis", "apple mobile device ethernet")
NOT_ETHERNET_HINTS = ("bluetooth", "virtual", "hyper-v", "vpn", "tap-", "wintun",
                      "wireguard", "loopback", "vmware", "virtualbox")
CREATE_NO_WINDOW = 0x08000000
ROLES = ("ethernet", "tether")


def _ps(command):
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"PowerShell exited {proc.returncode}")
    return proc.stdout


def _ps_json(command):
    text = _ps(command + " | ConvertTo-Json -Compress").strip()
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def _quote(alias):
    return "'" + alias.replace("'", "''") + "'"


def list_adapters():
    return _ps_json(
        "Get-NetAdapter | Select-Object Name, InterfaceDescription, "
        "@{n='Status';e={\"$($_.Status)\"}}, @{n='MediaType';e={\"$($_.MediaType)\"}}"
    )


def detect(adapters, cfg):
    """Return (ethernet_alias, tether_alias). Either may be None."""
    tether = cfg.get("tether_alias") or None
    ethernet = cfg.get("ethernet_alias") or None
    # Connected adapters first, so a stale unplugged one doesn't win.
    ordered = sorted(adapters, key=lambda a: a.get("Status") != "Up")
    if not tether:
        for a in ordered:
            desc = (a.get("InterfaceDescription") or "").lower()
            if any(h in desc for h in TETHER_HINTS):
                tether = a["Name"]
                break
    if not ethernet:
        for a in ordered:
            desc = (a.get("InterfaceDescription") or "").lower()
            if (a["Name"] != tether and a.get("MediaType") == "802.3"
                    and not any(h in desc for h in TETHER_HINTS + NOT_ETHERNET_HINTS)):
                ethernet = a["Name"]
                break
    return ethernet, tether


def ipv4_of(alias, addrs=None):
    if not alias:
        return None
    addrs = addrs if addrs is not None else psutil.net_if_addrs()
    for a in addrs.get(alias, ()):
        if a.family == socket.AF_INET and not a.address.startswith("169.254."):
            return a.address
    return None


class RouteControl:
    """Gives the tether a low metric and restores the original metric afterwards.

    The original values are written to disk before anything changes, so a crash
    or power cut is undone the next time DualLink starts (or with --restore).
    """

    def __init__(self, cfg, backup_path=None):
        self.cfg = cfg
        self.backup_path = Path(backup_path or config.METRIC_BACKUP_PATH)

    def _read_backup(self):
        try:
            with open(self.backup_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write_backup(self, data):
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        if data:
            with open(self.backup_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        elif self.backup_path.exists():
            self.backup_path.unlink()

    @staticmethod
    def _state(alias):
        return _ps_json(
            f"Get-NetIPInterface -InterfaceAlias {_quote(alias)} | Select-Object "
            "@{n='Family';e={\"$($_.AddressFamily)\"}}, "
            "@{n='Auto';e={\"$($_.AutomaticMetric)\"}}, InterfaceMetric"
        )

    def ensure(self, tether_alias):
        metric = int(self.cfg["tether_metric"])
        backup = self._read_backup()
        current = self._state(tether_alias)
        if tether_alias not in backup:
            backup[tether_alias] = current
            self._write_backup(backup)
        if any(r["Auto"] != "Disabled" or int(r["InterfaceMetric"]) != metric for r in current):
            _ps(f"Set-NetIPInterface -InterfaceAlias {_quote(tether_alias)} "
                f"-AutomaticMetric Disabled -InterfaceMetric {metric}")

    def restore(self):
        """Put back every saved metric. Adapters that are unplugged stay in the
        backup file and are restored on a later run."""
        backup = self._read_backup()
        left = {}
        for alias, rows in backup.items():
            try:
                for r in rows:
                    target = f"-InterfaceAlias {_quote(alias)} -AddressFamily {r['Family']}"
                    if r["Auto"] == "Enabled":
                        _ps(f"Set-NetIPInterface {target} -AutomaticMetric Enabled")
                    else:
                        _ps(f"Set-NetIPInterface {target} -AutomaticMetric Disabled "
                            f"-InterfaceMetric {int(r['InterfaceMetric'])}")
            except RuntimeError:
                left[alias] = rows
        self._write_backup(left)
        return not left


def obs_bind_ip(appdata=None):
    """What OBS's active profile binds its stream to.

    Returns None if OBS settings weren't found, "default" if OBS isn't bound to
    an address, or the bound IP.
    """
    base = Path(appdata or os.environ.get("APPDATA", "")) / "obs-studio"
    profile = None
    for name in ("user.ini", "global.ini"):  # user.ini is OBS 31+
        cp = _read_ini(base / name)
        if cp is not None:
            profile = cp.get("Basic", "ProfileDir", fallback=None)
            if profile:
                break
    if not profile:
        return None
    cp = _read_ini(base / "basic" / "profiles" / profile / "basic.ini")
    if cp is None:
        return None
    value = cp.get("Output", "BindIP", fallback="default").strip()
    return value or "default"


def _read_ini(path):
    if not path.exists():
        return None
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read(path, encoding="utf-8-sig")
    except configparser.Error:
        return None
    return cp


class LinkMonitor(threading.Thread):
    """Background thread: keeps link info and speeds fresh, and keeps the tether
    preferred while routing control is on."""

    DETECT_EVERY = 5.0
    ROUTE_CHECK_EVERY = 30.0

    def __init__(self, cfg, control_routes):
        super().__init__(daemon=True, name="LinkMonitor")
        self.cfg = cfg
        self.routes = RouteControl(cfg) if control_routes else None
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self.snapshot = {r: {"alias": None, "ip": None, "down": 0.0, "up": 0.0} for r in ROLES}
        self.error = ""

    def ips(self):
        with self._lock:
            return {r: self.snapshot[r]["ip"] for r in ROLES}

    def get(self):
        with self._lock:
            return {r: dict(v) for r, v in self.snapshot.items()}, self.error

    def stop(self):
        self._halt.set()

    def run(self):
        aliases = (None, None)
        last_detect = last_route = 0.0
        detect_error = route_error = ""
        prev, prev_t = psutil.net_io_counters(pernic=True), time.monotonic()
        while not self._halt.is_set():
            now = time.monotonic()
            if now - last_detect >= self.DETECT_EVERY:
                last_detect = now
                try:
                    new = detect(list_adapters(), self.cfg)
                    if new != aliases:
                        last_route = 0.0  # re-check routing right away
                    aliases = new
                    detect_error = ""
                except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                    detect_error = f"Couldn't read network adapters: {e}"
            addrs = psutil.net_if_addrs()
            tether_ip = ipv4_of(aliases[1], addrs)
            if self.routes and tether_ip and now - last_route >= self.ROUTE_CHECK_EVERY:
                last_route = now
                try:
                    self.routes.ensure(aliases[1])
                    route_error = ""
                except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                    route_error = f"Couldn't make the tether the preferred link: {e}"
            counters = psutil.net_io_counters(pernic=True)
            dt = max(time.monotonic() - prev_t, 1e-3)
            snap = {}
            for role, alias in zip(ROLES, aliases):
                c, p = counters.get(alias), prev.get(alias)
                down = (c.bytes_recv - p.bytes_recv) / dt if c and p else 0.0
                up = (c.bytes_sent - p.bytes_sent) / dt if c and p else 0.0
                snap[role] = {"alias": alias, "ip": ipv4_of(alias, addrs),
                              "down": max(down, 0.0), "up": max(up, 0.0)}
            prev, prev_t = counters, time.monotonic()
            with self._lock:
                self.snapshot = snap
                self.error = detect_error or route_error
            self._halt.wait(1.0)
