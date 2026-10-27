"""Settings stored in %APPDATA%\\DualLink\\config.json."""
import json
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "DualLink"
CONFIG_PATH = APP_DIR / "config.json"
METRIC_BACKUP_PATH = APP_DIR / "metric-backup.json"

DEFAULTS = {
    # Leave blank to auto-detect. Otherwise the adapter name shown in
    # Settings > Network > Advanced network settings (e.g. "Ethernet 2").
    "ethernet_alias": "",
    # Which phone tether / Wi-Fi games and apps use. Blank = first tether found.
    "main_alias": "",
    # Lower metric = Windows prefers that link for normal traffic.
    "tether_metric": 5,
    "mode": "offair",
    "speed_unit": "Mbps",  # or "MB/s"
    "download_dir": str(Path.home() / "Downloads"),
    # Connections per link type, or per link ("tether 2": 4).
    "download_workers": {"ethernet": 3, "tether": 6, "wifi": 4},
    "chunk_mb": 4,
    "backup_sources": [],
    "backup_remote": "gdrive:PC Backup",
    "rclone_path": "rclone",
    "rclone_transfers": 4,
}


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        # A broken config file shouldn't stop the app; keep defaults.
        pass
    return cfg


def save(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
