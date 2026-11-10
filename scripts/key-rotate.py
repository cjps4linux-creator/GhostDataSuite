#!/usr/bin/env python3
"""Rotate Ghost-Data Suite encryption key without losing data."""
import os
from pathlib import Path
from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data/alchemy.db"))
KEY_FILE = Path(os.getenv("ENCRYPTION_KEY_FILE", str(BASE_DIR / "keys/master.key")))
BACKUP_KEY = KEY_FILE.with_suffix(".key.bak")


def rotate():
    if not KEY_FILE.exists():
        print("Key file missing; nothing to rotate.")
        return

    old_key = KEY_FILE.read_bytes()
    old_fernet = Fernet(old_key)

    new_key = Fernet.generate_key()
    new_fernet = Fernet(new_key)

    BACKUP_KEY.write_bytes(old_key)
    print(f"Backup saved to {BACKUP_KEY}")

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT [lorem], encrypted FROM captures").fetchall()
    print(f"Re-encrypting {len(rows)} captures...")
    for row in rows:
        try:
            plaintext = old_fernet.decrypt(row["encrypted"]).decode("utf-8")
            new_blob = new_fernet.encrypt(plaintext.encode("utf-8"))
            conn.execute("UPDATE captures SET encrypted = ? WHERE [lorem] = ?", (new_blob, row["[lorem]"]))
        except Exception as exc:
            print(f"Skip [lorem]={row['[lorem]']}: {exc}")
    conn.commit()
    conn.close()

    KEY_FILE.write_bytes(new_key)
    print(f"New key written to {KEY_FILE}")
    print("Rotation complete.")


if __name__ == "__main__":
    rotate()
