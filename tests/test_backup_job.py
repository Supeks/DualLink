import os
import sys

from duallink import backup

HERE = os.path.dirname(os.path.abspath(__file__))


def test_backup_job_spreads_batches_over_both_links(tmp_path, monkeypatch):
    drive = tmp_path / "drive"
    drive.mkdir()
    src = tmp_path / "Documents"
    for i in range(30):
        sub = src / f"folder{i % 3}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"file{i}.txt").write_bytes(os.urandom(1000 + i))
    cmd = tmp_path / "rclone.cmd"
    cmd.write_text(f'@"{sys.executable}" "{os.path.join(HERE, "fake_rclone.py")}" %*\n')
    monkeypatch.setenv("FAKE_DRIVE_ROOT", str(drive))
    # Slow enough that one link can't finish every batch before the other starts.
    monkeypatch.setenv("FAKE_RCLONE_DELAY", "0.1")
    monkeypatch.setattr(backup, "BATCH_FILES", 3)  # many small batches

    cfg = {"rclone_path": str(cmd), "backup_sources": [str(src)],
           "backup_remote": "gdrive:PC Backup", "rclone_transfers": 4}
    job = backup.BackupJob(cfg, lambda: {"ethernet": "10.0.0.5", "tether": "192.168.42.7"})
    job.start()
    job.join(120)

    assert job.status == "Done", job.problems
    assert job.files_done == job.files_total == 30
    assert job.progress_bytes() == job.total_bytes
    for p in src.rglob("*.txt"):
        copied = drive / "PC Backup" / "Documents" / p.relative_to(src)
        assert copied.read_bytes() == p.read_bytes()
    binds = (drive / "binds.log").read_text().split()
    used = set(binds[0::2])
    assert used == {"10.0.0.5", "192.168.42.7"}


def test_backup_job_reports_missing_remote(tmp_path, monkeypatch):
    cmd = tmp_path / "rclone.cmd"
    cmd.write_text(f'@"{sys.executable}" "{os.path.join(HERE, "fake_rclone.py")}" %*\n')
    monkeypatch.setenv("FAKE_DRIVE_ROOT", str(tmp_path))
    cfg = {"rclone_path": str(cmd), "backup_sources": [str(tmp_path)],
           "backup_remote": "mydrive:Backup", "rclone_transfers": 4}
    job = backup.BackupJob(cfg, lambda: {"ethernet": "10.0.0.5", "tether": None})
    job.start()
    job.join(60)
    assert job.status == "Failed"
    assert "mydrive:" in job.problems[0]
