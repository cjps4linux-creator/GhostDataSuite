"""
Dual-mode database store for Ghost-Data Suite.

Supports both SQLite (default, single-writer with WAL) and PostgreSQL
(multi-user production).  Controlled via the ``DATABASE_URL`` environment
variable:

  DATABASE_URL=sqlite:///data/alchemy.db   # default
  DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/ghost

All SQL is parameterised.  Encryption at rest (Fernet) is applied in the
application layer regardless of backend, so the database never sees
plaintext capture content.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from logging_config import setup_json_logging

# PostgreSQL driver — optional; only required when DATABASE_URL is set
try:
    import psycopg2
    import psycopg2.extras
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover - psycopg2 is optional for SQLite-only deploys
    psycopg2 = None  # type: ignore[assignment]
    RealDictCursor = None  # type: ignore[assignment]

log = setup_json_logging(
    "db-store",
    log_file=os.getenv("LOG_PATH", "logs/db-store.log"),
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    json_logs=os.getenv("JSON_LOGS", "true").lower() == "true",
)

DATABASE_URL = os.getenv("DATABASE_URL", "")
KEY_FILE = os.getenv("ENCRYPTION_KEY_FILE", "keys/master.key")


class EncryptedStore:
    """
    Encrypted capture store with SQLite or PostgreSQL backend.

    Usage::

        store = EncryptedStore(db_path="data/alchemy.db", key_file="keys/master.key")
        # or, when DATABASE_URL is set:
        store = EncryptedStore(key_file="keys/master.key")  # db_path ignored

        capture_id = store.store(url="https://example.com", plaintext="...", status="ok")
        count = store.capture_count()
        store.close()
    """

    def __init__(self, db_path: str = "data/alchemy.db", key_file: str = KEY_FILE):
        self.key_file = key_file
        self._fernet = self._load_fernet()
        self.fernet = self._fernet  # public alias for backward compatibility
        self.use_postgres = bool(DATABASE_URL)
        self.db_path = db_path

        if self.use_postgres:
            self._init_postgres()
        else:
            self._init_sqlite(db_path)

    # ------------------------------------------------------------------
    # Encryption
    # ------------------------------------------------------------------

    def _load_fernet(self) -> Fernet:
        if not os.path.exists(self.key_file):
            Path(self.key_file).parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            with open(self.key_file, "wb") as fh:
                fh.write(key)
            log.info("Generated new master key at %s", self.key_file)
        with open(self.key_file, "rb") as fh:
            return Fernet(fh.read())

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    # ------------------------------------------------------------------
    # SQLite backend
    # ------------------------------------------------------------------

    def _init_sqlite(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        log.info("SQLite store initialized | DB=%s", db_path)

    def _create_schema(self):
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS captures (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at    TEXT NOT NULL,
                    url_hash      TEXT NOT NULL,
                    encrypted     BLOB NOT NULL,
                    status        TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS capture_media (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id        INTEGER NOT NULL,
                    url               TEXT NOT NULL,
                    category          TEXT NOT NULL,
                    filename          TEXT NOT NULL,
                    local_path        TEXT NOT NULL,
                    content_type      TEXT NOT NULL,
                    size_bytes        INTEGER NOT NULL,
                    created_at        TEXT NOT NULL
                );
                """
            )
        self._set_meta("last_boot", datetime.now(timezone.utc).isoformat())

    def _set_meta(self, key: str, value: str):
        if self.use_postgres:
            self.pg_conn.execute(
                "INSERT INTO meta(key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )
            self.pg_conn.commit()
        else:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    (key, value),
                )

    # ------------------------------------------------------------------
    # PostgreSQL backend
    # ------------------------------------------------------------------

    def _init_postgres(self):
        self.pg_conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        self.pg_conn.autocommit = False
        self._create_schema_pg()
        log.info("PostgreSQL store initialized | DATABASE_URL=%s", DATABASE_URL)

    def _create_schema_pg(self):
        with self.pg_conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS captures (
                    id            SERIAL PRIMARY KEY,
                    created_at    TEXT NOT NULL,
                    url_hash      TEXT NOT NULL,
                    encrypted     BYTEA NOT NULL,
                    status        TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS capture_media (
                    id                SERIAL PRIMARY KEY,
                    capture_id        INTEGER NOT NULL,
                    url               TEXT NOT NULL,
                    category          TEXT NOT NULL,
                    filename          TEXT NOT NULL,
                    local_path        TEXT NOT NULL,
                    content_type      TEXT NOT NULL,
                    size_bytes        INTEGER NOT NULL,
                    created_at        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_captures_url_hash ON captures(url_hash);
                CREATE INDEX IF NOT EXISTS idx_captures_created_at ON captures(created_at);
                CREATE INDEX IF NOT EXISTS idx_media_capture_id ON capture_media(capture_id);
                """
            )
        self.pg_conn.commit()
        self._set_meta("last_boot", datetime.now(timezone.utc).isoformat())

    # ------------------------------------------------------------------
    # Common API
    # ------------------------------------------------------------------

    def store(self, url: str, plaintext: str, status: str = "ok") -> int:
        """Encrypt and store a capture. Returns the new capture id."""
        encrypted = self._encrypt(plaintext)
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()

        if self.use_postgres:
            with self.pg_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO captures(created_at, url_hash, encrypted, status) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (created_at, url_hash, psycopg2.Binary(encrypted), status),
                )
                row = cur.fetchone()
                self.pg_conn.commit()
                return int(row["id"])
        else:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO captures(created_at, url_hash, encrypted, status) "
                    "VALUES (?, ?, ?, ?)",
                    (created_at, url_hash, encrypted, status),
                )
                return int(cur.lastrowid)

    def store_media(
        self,
        capture_id: int,
        items: list[dict],
    ) -> None:
        """Bulk-insert media metadata for a capture."""
        if not items:
            return
        rows = [
            (
                capture_id,
                item["url"],
                item["category"],
                item["filename"],
                item["path"],
                item.get("content_type", "application/octet-stream"),
                int(item.get("size_bytes", 0) or 0),
                datetime.now(timezone.utc).isoformat(),
            )
            for item in items
        ]
        if self.use_postgres:
            import psycopg2.extras

            with self.pg_conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO capture_media(capture_id, url, category, filename, "
                    "local_path, content_type, size_bytes, created_at) "
                    "VALUES %s",
                    rows,
                )
            self.pg_conn.commit()
        else:
            with self.conn:
                self.conn.executemany(
                    "INSERT INTO capture_media(capture_id, url, category, filename, "
                    "local_path, content_type, size_bytes, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )

    def capture_count(self) -> int:
        if self.use_postgres:
            with self.pg_conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM captures")
                row = cur.fetchone()
                return int(row["count"])
        else:
            cur = self.conn.execute("SELECT COUNT(*) FROM captures")
            return int(cur.fetchone()[0])

    def close(self):
        if self.use_postgres:
            if self.pg_conn:
                self.pg_conn.close()
        else:
            self.conn.close()
