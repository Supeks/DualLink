"""Find the uplinks, make Windows prefer the main one, and measure traffic.

Links: ethernet, any number of USB phone tethers, and Wi-Fi (useful when it's
connected to a phone hotspot rather than the router ethernet uses).

Routing model: the main link (the first phone tether, or Wi-Fi if there's no
tether) gets the lowest interface metric, so everything that doesn't pick a
link itself (games, browsers, launchers) uses it. OBS is pinned to ethernet
with its own "Bind to IP" setting. DualLink's downloads and backups bind each
connection to a link's IP, which Windows (strong host model) sends out of the
adapter that owns that IP.
"""
import configparser
import ipaddress
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
KIND_NAMES = {"ethernet": "Ethernet", "tether": "Phone tether", "wifi": "Wi-Fi"}
SHORT_NAMES = {"ethernet": "Eth", "tether": "Tether", "wifi": "Wi-Fi"}


def kind_of(link_id):
    """Link ids are "ethernet", "tether", "tether 2", ..., "wifi"."""
    return link_id.split()[0]


def link_name(link_id, short=False):
    kind = kind_of(link_id)
    base = (SHORT_NAMES if short else KIND_NAMES).get(kind, link_id)
    number = link_id[len(kind):].strip()
    return f"{base} {number}" if number else base


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
    """Classify adapters into links: [{"id", "kind", "alias"}], ethernet first,
    then phone tethers (ids "tether", "tether 2", ... by adapter name), then
    Wi-Fi. Tethers and Wi-Fi are only listed while connected; ethernet is listed
    even when its cable is out, so the window can say it's offline."""
    # Connected adapters first, so a stale unplugged one doesn't win.
    ordered = sorted(adapters, key=lambda a: a.get("Status") != "Up")
    tethers = sorted(a["Name"] for a in ordered if a.get("Status") == "Up"
                     and any(h in (a.get("InterfaceDescription") or "").lower() for h in TETHER_HINTS))
    ethernet = cfg.get("ethernet_alias") or None
    if not ethernet:
        for a in ordered:
            desc = (a.get("InterfaceDescription") or "").lower()
            if (a.get("MediaType") == "802.3"
                    and not any(h in desc for h in TETHER_HINTS + NOT_ETHERNET_HINTS)):
                ethernet = a["Name"]
                break
    wifi = next((a["Name"] for a in ordered if a.get("Status") == "Up"
                 and a.get("MediaType") == "Native 802.11"), None)
    links = []
    if ethernet:
        links.append({"id": "ethernet", "kind": "ethernet", "alias": ethernet})
    for n, alias in enumerate(t for t in tethers if t != ethernet):
        links.append({"id": "tether" if n == 0 else f"tether {n + 1}", "kind": "tether",
                      "alias": alias})
    if wifi:
        links.append({"id": "wifi", "kind": "wifi", "alias": wifi})
    return links


def pick_main(links, cfg):
    """The link games and other apps should use: the adapter named in settings
    (main_alias, or the older tether_alias), else the first connected tether,
    else Wi-Fi if it reaches a different network than ethernet. None means
    there's no second link and everything stays on ethernet."""
    want = cfg.get("main_alias") or cfg.get("tether_alias")
    usable = [l for l in links if l["kind"] != "ethernet" and l.get("ip")
              and not l.get("same_router")]
    for l in usable:
        if want and l["alias"] == want:
            return l["id"]
    for kind in ("tether", "wifi"):
        for l in usable:
            if l["kind"] == kind:
                return l["id"]
    return None


def ipv4_of(alias, addrs=None):
    if not alias:
        return None
    addrs = addrs if addrs is not None else psutil.net_if_addrs()
    for a in addrs.get(alias, ()):
        if a.family == socket.AF_INET and not a.address.startswith("169.254."):
            return a.address
    return None


def network_of(alias, addrs=None):
    """The IPv4 network the adapter is on, e.g. 192.168.1.0/24, or None."""
    addrs = addrs if addrs is not None else psutil.net_if_addrs()
    for a in addrs.get(alias, ()):
        if a.family == socket.AF_INET and a.netmask and not a.address.startswith("169.254."):
            return ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
    return None


class RouteControl:
    """Gives the main link a low metric and restores the original metric afterwards.

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

    def ensure(self, alias):
        metric = int(self.cfg["tether_metric"])
        backup = self._read_backup()
        current = self._state(alias)
        if alias not in backup:
            backup[alias] = current
            self._write_backup(backup)
        if any(r["Auto"] != "Disabled" or int(r["InterfaceMetric"]) != metric for r in current):
            _ps(f"Set-NetIPInterface -InterfaceAlias {_quote(alias)} "
                f"-AutomaticMetric Disabled -InterfaceMetric {metric}")

    def release(self, keep_alias):
        """Restore every adapter except keep_alias. Used when the main link moves
        (say phone 1 was unplugged), so only one adapter is ever preferred."""
        backup = self._read_backup()
        others = {a: rows for a, rows in backup.items() if a != keep_alias}
        if not others:
            return
        self._write_backup(others)
        self.restore()
        if keep_alias in backup:
            merged = self._read_backup()
            merged[keep_alias] = backup[keep_alias]
            self._write_backup(merged)

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


def describe(links, addrs):
    """Add ip and same_router to each link. Wi-Fi on the same network as
    ethernet is just the same internet line again, so it's marked and left out
    of downloads and backups."""
    eth_net = next((network_of(l["alias"], addrs) for l in links if l["kind"] == "ethernet"), None)
    out = []
    for l in links:
        l = dict(l, ip=ipv4_of(l["alias"], addrs), same_router=False)
        if l["kind"] == "wifi" and eth_net is not None:
            l["same_router"] = network_of(l["alias"], addrs) == eth_net
        out.append(l)
    return out


class LinkMonitor(threading.Thread):
    """Background thread: keeps link info and speeds fresh, and keeps the main
    link preferred while routing control is on."""

    DETECT_EVERY = 5.0
    ROUTE_CHECK_EVERY = 30.0

    def __init__(self, cfg, control_routes):
        super().__init__(daemon=True, name="LinkMonitor")
        self.cfg = cfg
        self.routes = RouteControl(cfg) if control_routes else None
        self._halt = threading.Event()
        self._lock = threading.Lock()
        # Each: id, kind, alias, ip, same_router, main, down, up (bytes/s).
        self.links = []
        self.error = ""

    def ips(self):
        """{link id: ip or None} for every link downloads and backups may use."""
        with self._lock:
            return {l["id"]: l["ip"] for l in self.links if not l["same_router"]}

    def get(self):
        with self._lock:
            return [dict(l) for l in self.links], self.error

    def stop(self):
        self._halt.set()

    def run(self):
        found = []
        main_alias = None
        last_detect = last_route = 0.0
        detect_error = route_error = ""
        prev, prev_t = psutil.net_io_counters(pernic=True), time.monotonic()
        while not self._halt.is_set():
            now = time.monotonic()
            if now - last_detect >= self.DETECT_EVERY:
                last_detect = now
                try:
                    found = detect(list_adapters(), self.cfg)
                    detect_error = ""
                except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                    detect_error = f"Couldn't read network adapters: {e}"
            links = describe(found, psutil.net_if_addrs())
            main = pick_main(links, self.cfg)
            alias = next((l["alias"] for l in links if l["id"] == main), None)
            if alias != main_alias:
                main_alias, last_route = alias, 0.0  # re-check routing right away
            if self.routes and alias and now - last_route >= self.ROUTE_CHECK_EVERY:
                last_route = now
                try:
                    self.routes.release(alias)
                    self.routes.ensure(alias)
                    route_error = ""
                except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                    route_error = f"Couldn't make {link_name(main)} the preferred link: {e}"
            counters = psutil.net_io_counters(pernic=True)
            dt = max(time.monotonic() - prev_t, 1e-3)
            for l in links:
                c, p = counters.get(l["alias"]), prev.get(l["alias"])
                l["down"] = max((c.bytes_recv - p.bytes_recv) / dt, 0.0) if c and p else 0.0
                l["up"] = max((c.bytes_sent - p.bytes_sent) / dt, 0.0) if c and p else 0.0
                l["main"] = l["id"] == main
            prev, prev_t = counters, time.monotonic()
            with self._lock:
                self.links = links
                self.error = detect_error or route_error
            self._halt.wait(1.0)
