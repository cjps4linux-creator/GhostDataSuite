"""
RQ task queue for Ghost-Data Suite scrape jobs.

Scraping is long-running and CPU/browser-intensive. Instead of blocking a
Gunicorn worker for the duration of a scrape, the HTTP endpoint now enqueues
a job into Redis and returns immediately.  RQ workers (one per container or
host) pop jobs and run the scrape in a separate process with its own Playwright
browser instance.

Environment variables:
  REDIS_URL        Redis connection string (default: redis://localhost:6379/0)
  QUEUE_NAME       RQ queue name (default: ghost-scrapes)
  GHOST_SERVER_URL Base URL of the Ghost server (for notification callbacks)
  GHOST_AUTH_TOKEN Auth token for server notifications
  DB_PATH          SQLite database path
  ENCRYPTION_KEY_FILE  Path to Fernet master key
  MEDIA_DIR        Directory for downloaded media
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib import robotparser
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from redis import Redis
from rq import Queue, Worker, Connection

from security import safe_open
from logging_config import setup_json_logging
from db_store import EncryptedStore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = os.getenv("QUEUE_NAME", "ghost-scrapes")
GHOST_SERVER_URL = os.getenv("GHOST_SERVER_URL", "").rstrip("/")
GHOST_AUTH_TOKEN = os.getenv("GHOST_AUTH_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "data/alchemy.db")
KEY_FILE = os.getenv("ENCRYPTION_KEY_FILE", "keys/master.key")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
JSON_LOGS = os.getenv("JSON_LOGS", "true").lower() == "true"

log = setup_json_logging(
    "rq-worker",
    log_file=os.getenv("LOG_PATH", "logs/rq-worker.log"),
    level=LOG_LEVEL,
    json_logs=JSON_LOGS,
)

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(KEY_FILE).parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Redis + RQ queue
# ---------------------------------------------------------------------------

redis_conn = Redis.from_url(REDIS_URL, socket_timeout=5, socket_connect_timeout=5)
queue = Queue(QUEUE_NAME, connection=redis_conn)

# ---------------------------------------------------------------------------
# Shared helpers (scrape logic extracted so both worker and CLI can use it)
# ---------------------------------------------------------------------------


def notify_server(capture_id: int, url: str, status: str) -> None:
    """Best-effort POST to Ghost server for SocketIO notification."""
    if not GHOST_SERVER_URL:
        return
    payload = json.dumps({"id": capture_id, "url": url, "status": status}).encode("utf-8")
    try:
        req = Request(
            GHOST_SERVER_URL + "/notify/capture",
            data=payload,
            headers={"Content-Type": "application/json", "X-Ghost-Token": GHOST_AUTH_TOKEN},
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            resp.read()
    except (URLError, HTTPError, OSError) as exc:
        log.debug("Server notification failed (non-fatal): %s", exc)


def build_headers() -> dict:
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


# ---------------------------------------------------------------------------
# The actual scrape job — runs inside an RQ worker process
# ---------------------------------------------------------------------------


def scrape_job(url: str, interval: str = "60", once: bool = True) -> dict:
    """
    Scrape a single URL using Playwright, encrypt, store, and notify the server.

    Returns a dict with capture_id, url, status, and any error information.
    """
    result = {"url": url, "status": "error", "capture_id": None, "error": None}

    # Domain rate limiting (simple in-process; cross-worker dedup handled by RQ)
    domain = urlparse(url).netloc
    last_key = f"rq:last_scrape:{domain}"
    now = time.time()
    try:
        last = redis_conn.get(last_key)
    except Exception:  # Redis unavailable — skip rate limiting in inline mode
        last = None
    min_delay = float(os.getenv("SCRAPE_DOMAIN_MIN_DELAY", "2.0"))
    if last:
        elapsed = now - float(last)
        if elapsed < min_delay:
            time.sleep(min_delay - elapsed)

    # robots.txt respect
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(f"{urlparse(url).scheme}://{domain}/robots.txt")
        rp.read()
        if not rp.can_fetch("*", url):
            log.warning("robots.txt disallows scraping: %s", url)
            store = EncryptedStore(DB_PATH, KEY_FILE)
            capture_id = store.store(url, json.dumps({"error": "robots.txt disallowed"}), status="error")
            store.close()
            notify_server(capture_id, url, "error")
            result["status"] = "error"
            result["error"] = "robots.txt disallowed"
            result["capture_id"] = capture_id
            return result
    except Exception as exc:
        log.warning("robots.txt fetch failed for %s: %s", domain, exc)
        # Fail open

    # Launch a dedicated browser per job — this is the "browser grid" pattern.
    # Each RQ worker gets its own Playwright browser instance; concurrent
    # workers = concurrent browsers.
    from playwright.sync_api import sync_playwright

    capture_id = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=build_headers()["User-Agent"])
                try:
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
                            raise exc2

                    title = safe_text(page.title(), 300)
                    body = safe_text(page.inner_text("body"), 4000)
                    payload = json.dumps({"url": url, "title": title, "body": body})

                    store = EncryptedStore(DB_PATH, KEY_FILE)
                    try:
                        capture_id = store.store(url, payload, status="ok")
                        notify_server(capture_id, url, "ok")

                        # Media capture
                        media_urls = _extract_media_urls(page.content(), url)
                        media_items = _download_media(MEDIA_DIR, capture_id, media_urls)
                        store.store_media(capture_id, media_items)
                        log.info("Media captured | id=%d media=%d", capture_id, len(media_items))
                    finally:
                        store.close()

                    result["status"] = "ok"
                    result["capture_id"] = capture_id
                    log.info("Captured and encrypted | title=%s", title or "(none)")
                finally:
                    context.close()
            finally:
                browser.close()

    except Exception as exc:
        log.exception("Scrape failed: %s", exc)
        try:
            store = EncryptedStore(DB_PATH, KEY_FILE)
            capture_id = store.store(url, json.dumps({"error": str(exc)}), status="error")
            store.close()
            notify_server(capture_id, url, "error")
            result["capture_id"] = capture_id
        except Exception:
            pass
        result["error"] = str(exc)

    # Record the scrape timestamp for rate limiting
    try:
        redis_conn.setex(last_key, 3600, now)
    except Exception:  # Redis unavailable — skip rate limiting in inline mode
        pass

    return result


# ---------------------------------------------------------------------------
# Media helpers (extracted from scraper.py to avoid duplication)
# ---------------------------------------------------------------------------

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
    from urllib.parse import urljoin, urlparse
    import re

    html = html.replace("\\\\/", "/")
    url_re = re.compile(r"https?://[^\s\"<>]+|/[^\s\"<>]+")
    seen: set[str] = set()
    out: list[str] = []

    def _is_fetchable(candidate: str) -> bool:
        """Return True only for absolute http(s) URLs with a valid host.

        Rejects malformed IPv6 addresses (e.g. bare ::1), fragment-only URLs,
        hash-like paths, and anything that would crash urlparse/urljoin.
        """
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return False
        if parsed.scheme.lower() not in ("http", "https"):
            return False
        if not parsed.netloc:
            return False
        # Reject IPv6 addresses that are not bracket-enclosed (e.g. [::1])
        host = parsed.hostname or ""
        if ":" in host and not (host.startswith("[") and host.endswith("]")):
            return False
        # Reject paths that look like bare hashes (e.g. /9f3551...abc47)
        path_lower = parsed.path.lower()
        if path_lower == "/" or not path_lower:
            return False
        last = path_lower.split("/")[-1]
        if last and len(last) >= 32 and all(c in "0123456789abcdef" for c in last):
            # Likely a hash fragment mistakenly treated as a path
            return False
        return True

    for raw in url_re.findall(html):
        if raw.startswith("http://") or raw.startswith("https://"):
            candidate = raw
        else:
            candidate = urljoin(base_url, raw)
        candidate = candidate.replace("\\\\", "/")

        if not _is_fetchable(candidate):
            continue

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
        if ext not in {ext for exts in MEDIA_EXT_MAP.values() for ext in exts}:
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
            with safe_open(url, 45) as upstream:
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


# ---------------------------------------------------------------------------
# RQ worker entry point
# ---------------------------------------------------------------------------


def main():
    """Run an RQ worker that processes ghost-scrapes queue jobs."""
    log.info("Starting RQ worker | queue=%s | redis=%s", QUEUE_NAME, REDIS_URL)
    with Connection(redis_conn):
        worker = Worker([QUEUE_NAME])
        worker.work()


if __name__ == "__main__":
    main()
