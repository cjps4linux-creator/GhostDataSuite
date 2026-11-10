"""Backup and restore for Ghost-Data Suite."""
from __future__ import annotations

import sys
import shutil
import tarfile
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
KEYS_DIR = BASE_DIR / "keys"
LOGS_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"


def backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = BACKUP_DIR / f"ghost-backup-{ts}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for folder in (DATA_DIR, KEYS_DIR, LOGS_DIR):
            if folder.exists():
                tar.add(folder, arcname=folder.name)
    print(f"Backup created: {archive}")
    print(f"Size: {archive.stat().st_size / 1024:.1f} KB")
    return archive


def restore(archive_path: Path) -> None:
    if not archive_path.exists():
        print(f"Backup not found: {archive_path}")
        sys.exit(1)
    extract_dir = BASE_DIR / "restore-tmp"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(extract_dir)
    for folder in ("data", "keys", "logs"):
        src = extract_dir / folder
        dst = BASE_DIR / folder
        if src.exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
    shutil.rmtree(extract_dir)
    print(f"Restored from: {archive_path}")
    print("Restart Docker: docker compose restart alchemist ghost-shell")


def list_backups() -> None:
    if not BACKUP_DIR.exists():
        print("No backups directory found.")
        return
    backups = sorted(BACKUP_DIR.glob("ghost-backup-*.tar.gz"))
    if not backups:
        print("No backups found.")
        return
    print(f"Found {len(backups)} backup(s):")
    for b in backups:
        size = b.stat().st_size / 1024
        print(f"  {b.name}  ({size:.1f} KB)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python scripts/backup-restore.py backup")
        print("  python scripts/backup-restore.py restore <backup-file.tar.gz>")
        print("  python scripts/backup-restore.py list")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "backup":
        backup()
    elif cmd == "restore":
        if len(sys.argv) < 3:
            print("Usage: python scripts/backup-restore.py restore <backup-file.tar.gz>")
            sys.exit(1)
        restore(Path(sys.argv[2]))
    elif cmd == "list":
        list_backups()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
