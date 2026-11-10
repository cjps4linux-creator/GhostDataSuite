"""
Ghost-Data Suite  -  Data Alchemist
Captures web content, encrypts it, and stores it in SQLite.
Supports multi-target scheduling from config.
"""

from __future__ import annotations

import os
import sys
import sqlite3
import json
import hashlib
import time
import logging
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone
from cryptography.fernet import Fernet

DB_PATH = os.getenv("DB_PATH", "data/alchemy.db")
KEY_FILE = os.getenv("ENCRYPTION_KEY_FILE", "keys/master.key")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
CONFIG_FILE = os.getenv("CONFIG_FILE", "config/config.yaml")
LOG_PATH = os.getenv("LOG_PATH", "logs/alchemist.log")

# Same SSRF guard as ghost_server: only absolute http(s) URLs may be fetched.
ALLOWED_URL_SCHEMES = ("http", "https")


def _is_allowed_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return False
    return bool(parsed.netloc)


def _safe_open(url: str, timeout: int, headers: dict | None = None):
    if not _is_allowed_url(url):
        raise ValueError(f"blocked URL scheme or missing host: {url!r}")
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "GhostDataSuite/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 - scheme and host are validated by _is_allowed_url() above
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))

Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("alchemist")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_targets() -> list[dict]:
    p = Path(CONFIG_FILE)
    if not p.exists():
        return []
    try:
        import yaml
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        targets = ((cfg.get("alchemist") or {}).get("targets")) or []
        return [t for t in targets if t.get("enabled", True)]
    except Exception as exc:
        log.warning("Config load failed, using empty targets: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Encryption layer
# ---------------------------------------------------------------------------

class EncryptedStore:
    def __init__(self, db_path: str, key_file: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(key_file).parent.mkdir(parents=True, exist_ok=True)

        if not os.path.exists(key_file):
            key = Fernet.generate_key()
            with open(key_file, "wb") as fh:
                fh.write(key)
            log.info("Generated new master key at %s", key_file)

        with open(key_file, "rb") as fh:
            self.fernet = Fernet(fh.read())

        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self._init_schema()

    def _init_schema(self):
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS captures (
                    [lorem]          INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at  TEXT NOT NULL,
                    url_hash    TEXT NOT NULL,
                    encrypted   BLOB NOT NULL,
                    status      TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
        self._set_meta("last_boot", datetime.now(timezone.utc).isoformat())

    def _set_meta(self, key: str, value: str):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
            )

    def store(self, url: str, plaintext: str, status: str = "ok") -> int:
        encrypted = self.fernet.encrypt(plaintext.encode("utf-8"))
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO captures(created_at, url_hash, encrypted, status) VALUES (?, ?, ?, ?)",
                (created_at, url_hash, encrypted, status),
            )
            return cur.lastrowid

    def capture_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM captures")
        return cur.fetchone()[0]

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Browser logic
# ---------------------------------------------------------------------------

def build_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


def safe_text(text: str, limit: int = 2000) -> str:
    return text[:limit] if text else ""


MEDIA_EXT_MAP = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico", ".avif"},
    "video": {".mp4", ".webm", ".ogv", ".mov", ".mkv", ".avi"},
    "audio": {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"},
    "document": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".csv"},
    "map": {".png", ".jpg", ".jpeg", ".pbf", ".mvt"},
}


def _classify_media(url: str, content_type: str = "") -> str | None:
    try:
        path = url.split("?", 1)[0].split("#", 1)[0]
        ext = Path(path).suffix.lower()
        parts = [p.lower() for p in path.split("/") if p]
    except Exception:
        return None
    lower = url.lower()
    ctype = content_type.lower()
    if any(k in lower for k in ["map", "tile", "geo", "openstreetmap", "mapbox", "carto", "basemap"]):
        return "map"
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("audio/"):
        return "audio"
    if ctype in {"application/pdf", "application/octet-stream"} or "pdf" in parts:
        return "document"
    for category, exts in MEDIA_EXT_MAP.items():
        if category == "map":
            continue
        if ext in exts:
            return category
    for category, words in {
        "image": ["image", "img", "photo", "thumbnail", "cover", "avatar", "banner"],
        "video": ["video", "clip", "movie", "watch"],
        "audio": ["audio", "sound", "music", "podcast"],
        "document": ["doc", "pdf", "file", "download", "report"],
        "map": ["map", "tile", "basemap", "layer"],
    }.items():
        if any(w in parts for w in words):
            return category
    return None


def _extract_media_urls(html: str, base_url: str) -> list[str]:
    MEDIA_EXT_GUESS = {ext for exts in MEDIA_EXT_MAP.values() for ext in exts}
    from urllib.parse import urljoin, urlparse
    import re

    html = html.replace("\\/", "/")
    url_re = re.compile(r"https?://[^\\s\"<>]+|/[^\\s\"<>]+")
    seen: set[str] = set()
    out: list[str] = []
    for raw in url_re.findall(html):
        if raw.startswith("http://") or raw.startswith("https://"):
            candidate = raw
        else:
            candidate = urljoin(base_url, raw)
        candidate = candidate.replace("\\\\", "/")
        parsed = urlparse(candidate)
        path = parsed.path
        if parsed.netloc:
            segment = f"//{parsed.netloc}/"
            if segment in path:
                path = path.replace(segment, "/")
        while "//" in path:
            path = path.replace("//", "/")
        candidate = parsed._replace(path=path).geturl()
        parsed = urlparse(candidate)
        path = parsed.path.lower()
        if not path or path == "/":
            continue
        if "." not in path.split("/")[-1]:
            continue
        ext = "." + path.split(".")[-1]
        if ext not in MEDIA_EXT_GUESS:
            continue
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _download_media(base_dir: Path, capture_id: int, media_urls: list[str]) -> list[dict]:

    base_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for url in media_urls:
        url = url.replace("\\", "/")
        category = _classify_media(url, "")
        if not category:
            continue
        parsed = urlparse(url)
        filename = Path(parsed.path).name or f"media-{abs(hash(url))}"
        target_dir = base_dir / str(capture_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        local_path = target_dir / filename
        content_type = ""
        size_bytes = 0
        try:
            with _safe_open(url, 45) as upstream:
                data = upstream.read()
                size_bytes = len(data)
                content_type = upstream.headers.get("Content-Type", "") or ""
                local_path.write_bytes(data)
        except Exception as exc:
            log.warning("Media download failed | url=%s err=%s", url, exc)
        results.append({
            "url": url,
            "category": category,
            "filename": filename,
            "path": str(local_path),
            "content_type": content_type,
            "size_bytes": size_bytes,
        })
    return results


def _store_media_meta(conn: sqlite3.Connection, capture_id: int, items: list[dict]) -> None:
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS capture_media (
                [lorem] INTEGER PRIMARY KEY AUTOINCREMENT,
                capture_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                category TEXT NOT NULL,
                filename TEXT NOT NULL,
                local_path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        for item in items:
            conn.execute(
                "INSERT INTO capture_media(capture_id, url, category, filename, local_path, content_type, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    capture_id,
                    item["url"],
                    item["category"],
                    item["filename"],
                    item["path"],
                    item.get("content_type", "application/octet-stream"),
                    int(item.get("size_bytes", 0) or 0),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )


def run_hooks(stage: str, context: dict):
    """Plugin hook runner. Place hook modules under hooks/."""
    hooks_dir = Path("hooks")
    if not hooks_dir.exists():
        return
    for path in hooks_dir.glob("*.py"):
        try:
            mod = __import__(path.stem, fromlist=["run"])
            if hasattr(mod, "run"):
                mod.run(stage, context)
        except Exception as exc:
            log.warning("Hook %s failed: %s", path.name, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Starting Data Alchemist")
    store = EncryptedStore(DB_PATH, KEY_FILE)
    log.info("Store initialized | DB=%s | Records=%d", DB_PATH, store.capture_count())
    run_hooks("startup", {"db_path": DB_PATH})
    media_base = Path(MEDIA_DIR)
    media_base.mkdir(parents=True, exist_ok=True)

    def consume_trigger() -> dict | None:
      trigger_path = Path(os.environ.get("GHOST_DATA_DIR", "data")) / "trigger.json"
      manual_path = Path(os.environ.get("GHOST_DATA_DIR", "data")) / "manual_target.json"
      if manual_path.exists():
        try:
          data = json.loads(manual_path.read_text(encoding="utf-8"))
          if data.get("once"):
            log.info("One-shot manual scrape: %s", data.get("url"))
            return data
          return data
        except Exception as exc:
          log.warning("Manual target parse failed: %s", exc)
      if trigger_path.exists():
        try:
          data = json.loads(trigger_path.read_text(encoding="utf-8"))
          return data
        except Exception as exc:
          log.warning("Trigger parse failed: %s", exc)
      return None

    try:
        while True:
            trigger = consume_trigger()
            if trigger:
                url = trigger.get("url")
                interval = int(trigger.get("interval") or 60)
                targets = [{"url": url, "interval": interval, "enabled": True}] if url else []
            else:
                targets = load_targets()
            for target in targets:
                url = target.get("url") or os.getenv("TARGET_URL", "").strip()
                if not url:
                    continue
                interval = int(target.get("interval") or os.getenv("SCRAPE_INTERVAL") or 60)
                try:
                    from playwright.sync_api import sync_playwright

                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True)
                        context = browser.new_context(user_agent=build_headers()["User-Agent"])
                        page = context.new_page()
                        log.info("Navigating to %s", url)
                        try:
                            page.goto(url, timeout=90_000, wait_until="domcontentloaded")
                        except Exception as exc:
                            log.warning("Navigation failed on first attempt: %s", exc)
                            try:
                                page.goto(url, timeout=90_000, wait_until="domcontentloaded")
                            except Exception as exc2:
                                log.warning("Navigation failed on retry: %s", exc2)
                        try:
                            title = safe_text(page.title(), 300)
                            body = safe_text(page.inner_text("body"), 4000)
                        except Exception as exc:
                            log.warning("Partial extraction fallback: %s", exc)
                            title = safe_text(page.title(), 300)
                            body = safe_text(page.content(), 2000)
                        payload = json.dumps({"url": url, "title": title, "body": body})
                        capture_id = store.store(url, payload, status="ok")
                        try:
                            media_urls = _extract_media_urls(page.content(), url)
                            media_items = _download_media(media_base, capture_id, media_urls)
                            _store_media_meta(store.conn, capture_id, media_items)
                            log.info(
                                "Media captured | [lorem]=%d media=%d",
                                capture_id,
                                len(media_items),
                            )
                        except Exception as exc:
                            log.warning("Media capture failed: %s", exc)
                        run_hooks("post_capture", {"url": url, "status": "ok", "capture_id": capture_id})
                        log.info("Captured and encrypted | title=%s", title or "(none)")
                        browser.close()
                except Exception as exc:
                    log.exception("Scrape failed: %s", exc)
                    try:
                        store.store(url, json.dumps({"error": str(exc)}), status="error")
                        run_hooks("post_capture", {"url": url, "status": "error", "error": str(exc)})
                    except Exception:
                        pass

            manual_path = Path(os.environ.get("GHOST_DATA_DIR", "data")) / "manual_target.json"
            if manual_path.exists():
                manual_path.unlink()
            time.sleep(2)

    except KeyboardInterrupt:
        log.info("Shutdown requested")
    finally:
        store.close()
        log.info("Store closed")


if __name__ == "__main__":
    main()
