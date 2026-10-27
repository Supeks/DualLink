import json
import os
import socket

from duallink import backup, links


def test_changed_files_finds_new_and_modified():
    local = {"a.txt": (10, 1000.0), "b.txt": (20, 2000.0), "c.txt": (30, 3000.0),
             "d.txt": (40, 4000.0)}
    remote = {"a.txt": (10, 1000.4),   # same (within a second)
              "b.txt": (21, 2000.0),   # size differs
              "c.txt": (30, 3500.0)}   # edited later
    assert backup.changed_files(local, remote) == [("b.txt", 20), ("c.txt", 30), ("d.txt", 40)]


def test_make_batches_respects_size_and_count():
    big = backup.BATCH_BYTES
    files = [("huge.mkv", big * 2), ("x1", 10), ("x2", 10)]
    batches = backup.make_batches("C:/src", "gdrive:dst", files)
    assert [b.files for b in batches] == [["huge.mkv"], ["x1", "x2"]]
    many = [(f"f{i}", 1) for i in range(backup.BATCH_FILES + 5)]
    sizes = [len(b.files) for b in backup.make_batches("s", "d", many)]
    assert sizes == [backup.BATCH_FILES, 5]


def test_remote_path():
    assert backup.remote_path("gdrive:PC Backup", "Photos") == "gdrive:PC Backup/Photos"
    assert backup.remote_path("gdrive:PC Backup/", "Photos") == "gdrive:PC Backup/Photos"
    assert backup.remote_path("gdrive:", "Photos") == "gdrive:Photos"


def test_parse_rclone_time_handles_nanoseconds_and_offsets():
    a = backup.parse_rclone_time("2024-05-01T10:00:00.123456789Z")
    b = backup.parse_rclone_time("2024-05-01T15:30:00.123456+05:30")
    c = backup.parse_rclone_time("2024-05-01T10:00:00Z")
    assert abs(a - b) < 1e-5
    assert abs(a - c - 0.123456) < 1e-5


def test_parse_log_line():
    stats_line = json.dumps({"level": "info", "msg": "x", "stats": {"bytes": 5, "speed": 1.5}})
    assert backup.parse_log_line(stats_line) == ({"bytes": 5, "speed": 1.5}, None)
    err_line = json.dumps({"level": "error", "msg": "quota exceeded"})
    assert backup.parse_log_line(err_line) == (None, "quota exceeded")
    assert backup.parse_log_line("not json") == (None, None)


def test_scan_local(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("hello")
    (tmp_path / "g.bin").write_bytes(b"12")
    found = backup.scan_local(str(tmp_path))
    assert set(found) == {"sub/f.txt", "g.bin"}
    assert found["sub/f.txt"][0] == 5


ADAPTERS = [
    {"Name": "Wi-Fi", "InterfaceDescription": "Intel Wireless", "Status": "Disconnected",
     "MediaType": "Native 802.11"},
    {"Name": "Bluetooth Network Connection", "InterfaceDescription":
     "Bluetooth Device (Personal Area Network)", "Status": "Disconnected", "MediaType": "802.3"},
    {"Name": "vEthernet (WSL)", "InterfaceDescription": "Hyper-V Virtual Ethernet Adapter",
     "Status": "Up", "MediaType": "802.3"},
    {"Name": "Ethernet", "InterfaceDescription": "Realtek PCIe GbE Family Controller",
     "Status": "Up", "MediaType": "802.3"},
    {"Name": "Ethernet 3", "InterfaceDescription": "Remote NDIS based Internet Sharing Device",
     "Status": "Up", "MediaType": "802.3"},
]


def ids(found):
    return [(l["id"], l["alias"]) for l in found]


def test_detect_finds_ethernet_and_android_tether():
    assert ids(links.detect(ADAPTERS, {})) == [("ethernet", "Ethernet"), ("tether", "Ethernet 3")]


def test_detect_without_phone_and_with_ethernet_override():
    no_phone = [a for a in ADAPTERS if "NDIS" not in a["InterfaceDescription"]]
    assert ids(links.detect(no_phone, {})) == [("ethernet", "Ethernet")]
    assert ids(links.detect(no_phone, {"ethernet_alias": "LAN"})) == [("ethernet", "LAN")]


def test_detect_iphone_tether():
    iphone = ADAPTERS[:4] + [{"Name": "Ethernet 4", "InterfaceDescription":
                              "Apple Mobile Device Ethernet", "Status": "Up", "MediaType": "802.3"}]
    assert ids(links.detect(iphone, {}))[1] == ("tether", "Ethernet 4")


def test_detect_two_tethers_and_wifi():
    more = [dict(a) for a in ADAPTERS]
    more[0]["Status"] = "Up"  # Wi-Fi connected
    more.append({"Name": "Ethernet 2", "InterfaceDescription":
                 "SAMSUNG Mobile USB Remote NDIS Network Device", "Status": "Up", "MediaType": "802.3"})
    more.append({"Name": "Ethernet 9", "InterfaceDescription":
                 "Remote NDIS based Internet Sharing Device #3", "Status": "Disconnected",
                 "MediaType": "802.3"})  # a ghost from an earlier plug-in
    assert ids(links.detect(more, {})) == [("ethernet", "Ethernet"), ("tether", "Ethernet 2"),
                                           ("tether 2", "Ethernet 3"), ("wifi", "Wi-Fi")]


def test_pick_main():
    found = [{"id": "ethernet", "kind": "ethernet", "alias": "Ethernet", "ip": "192.168.1.5"},
             {"id": "tether", "kind": "tether", "alias": "Ethernet 2", "ip": "10.0.0.2"},
             {"id": "tether 2", "kind": "tether", "alias": "Ethernet 3", "ip": "10.1.0.2"},
             {"id": "wifi", "kind": "wifi", "alias": "Wi-Fi", "ip": "172.20.10.3"}]
    assert links.pick_main(found, {}) == "tether"
    assert links.pick_main(found, {"main_alias": "Ethernet 3"}) == "tether 2"
    assert links.pick_main(found, {"tether_alias": "Wi-Fi"}) == "wifi"  # older setting name
    no_phone = [found[0], found[3]]
    assert links.pick_main(no_phone, {}) == "wifi"
    assert links.pick_main([found[0], dict(found[3], same_router=True)], {}) is None
    assert links.pick_main([found[0], dict(found[1], ip=None)], {}) is None


class Addr:
    def __init__(self, address, netmask):
        self.family, self.address, self.netmask = socket.AF_INET, address, netmask


def test_describe_marks_wifi_on_ethernets_router():
    found = [{"id": "ethernet", "kind": "ethernet", "alias": "Ethernet"},
             {"id": "wifi", "kind": "wifi", "alias": "Wi-Fi"}]
    same = {"Ethernet": [Addr("192.168.1.20", "255.255.255.0")],
            "Wi-Fi": [Addr("192.168.1.40", "255.255.255.0")]}
    hotspot = dict(same, **{"Wi-Fi": [Addr("172.20.10.3", "255.255.255.240")]})
    assert links.describe(found, same)[1]["same_router"] is True
    described = links.describe(found, hotspot)
    assert described[1]["same_router"] is False and described[1]["ip"] == "172.20.10.3"


def test_link_names():
    assert links.link_name("tether") == "Phone tether"
    assert links.link_name("tether 2") == "Phone tether 2"
    assert links.link_name("tether 2", short=True) == "Tether 2"
    assert links.link_name("wifi") == "Wi-Fi"
    assert links.link_name("ethernet", short=True) == "Eth"


def _obs(tmp_path, user_ini, basic_ini):
    base = tmp_path / "obs-studio"
    (base / "basic" / "profiles" / "Main").mkdir(parents=True)
    (base / "user.ini").write_text(user_ini, encoding="utf-8-sig")
    (base / "basic" / "profiles" / "Main" / "basic.ini").write_text(basic_ini, encoding="utf-8-sig")
    return str(tmp_path)


def test_obs_bind_ip(tmp_path):
    root = _obs(tmp_path, "[Basic]\nProfileDir=Main\n", "[Output]\nBindIP=192.168.1.20\n")
    assert links.obs_bind_ip(root) == "192.168.1.20"


def test_obs_bind_ip_default_and_missing(tmp_path):
    root = _obs(tmp_path, "[Basic]\nProfileDir=Main\n", "[Output]\nMode=Simple\n")
    assert links.obs_bind_ip(root) == "default"
    assert links.obs_bind_ip(str(tmp_path / "nowhere")) is None


def test_route_control_backup_survives_unplugged_adapter(tmp_path, monkeypatch):
    path = tmp_path / "metric-backup.json"
    path.write_text(json.dumps({"Phone": [{"Family": "IPv4", "Auto": "Enabled",
                                           "InterfaceMetric": 25}]}))
    def fail(_cmd):
        raise RuntimeError("No MSFT_NetIPInterface objects found")
    monkeypatch.setattr(links, "_ps", fail)
    rc = links.RouteControl({"tether_metric": 5}, backup_path=path)
    assert rc.restore() is False
    assert json.loads(path.read_text())["Phone"][0]["InterfaceMetric"] == 25

    calls = []
    monkeypatch.setattr(links, "_ps", lambda cmd: calls.append(cmd) or "")
    assert rc.restore() is True
    assert not path.exists()
    assert calls == ["Set-NetIPInterface -InterfaceAlias 'Phone' -AddressFamily IPv4 "
                     "-AutomaticMetric Enabled"]
