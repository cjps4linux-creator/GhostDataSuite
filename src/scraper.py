"""
Ghost-Data Suite  -  Data Alchemist
Captures web content, encrypts it, and stores it in SQLite.
Supports multi-target scheduling from config.
"""

from __future__ import annotations

import os
import json
import time
import heapq
from pathlib import Path
from urllib.parse import urlparse
from urllib import robotparser
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from cryptography.fernet import Fernet  # noqa: F401  (re-exported for test compatibility: scraper.Fernet)
from security import safe_open
from db_store import EncryptedStore

DB_PATH = os.getenv("DB_PATH", "data/alchemy.db")
KEY_FILE = os.getenv("ENCRYPTION_KEY_FILE", "keys/master.key")
GHOST_SERVER_URL = os.getenv("GHOST_SERVER_URL", "").rstrip("/")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
CONFIG_FILE = os.getenv("CONFIG_FILE", "config/config.yaml")
LOG_PATH = os.getenv("LOG_PATH", "logs/alchemist.log")

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))

Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)

from logging_config import setup_json_logging

log = setup_json_logging(
    "alchemist",
    log_file=LOG_PATH,
    level=LOG_LEVEL,
    json_logs=os.getenv("JSON_LOGS", "true").lower() == "true",
)

# OpenTelemetry tracing for scraper
_tracer = None
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otel_endpoint:
        provider = TracerProvider()
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint))
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
        RequestsInstrumentor().instrument()
        _tracer = trace.get_tracer(__name__)
        log.info("OpenTelemetry tracing enabled for scraper", endpoint=otel_endpoint)
    else:
        log.info("OpenTelemetry tracing disabled for scraper")
except Exception as exc:  # pragma: no cover
    log.warning("OpenTelemetry not available for scraper: %s", str(exc))
    _tracer = None

tracer = _tracer


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


def notify_server(capture_id: int, url: str, status: str) -> None:
    """POST capture metadata to the Ghost server so it can emit a SocketIO
    ``capture_created`` event for real-time dashboard updates.

    Best-effort: failures are logged but never raised, so a down server
    never breaks the scraper.
    """
    if not GHOST_SERVER_URL:
        return
    payload = json.dumps({"id": capture_id, "url": url, "status": status}).encode("utf-8")
    try:
        req = Request(
            GHOST_SERVER_URL + "/notify/capture",
            data=payload,
            headers={"Content-Type": "application/json", "X-Ghost-Token": os.getenv("GHOST_AUTH_TOKEN", "")},
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            resp.read()
    except (URLError, HTTPError, OSError) as exc:
        log.debug("Server notification failed (non-fatal): %s", exc)


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
# Browser grid — multiple concurrent Playwright browser instances
# ---------------------------------------------------------------------------

class BrowserGrid:
    """Manages multiple Playwright browser instances for concurrent scraping.

    Each browser runs in its own Playwright instance. Contexts are dispensed
    to the least-loaded browser to distribute load and provide isolation.
    Individual browsers recycle after a context count or time TTL to avoid
    memory leaks.

    Usage::

        grid = BrowserGrid(max_browsers=3, max_contexts_per_browser=10)
        grid.start()
        context = grid.new_context(user_agent="...")
        page = context.new_page()
        # ... scrape ...
        context.close()
        grid.stop()
    """

    def __init__(
        self,
        max_browsers: int = 3,
        max_contexts_per_browser: int = 10,
        recycle_after: int = 3600,
    ):
        self._max_browsers = max_browsers
        self._max_contexts_per_browser = max_contexts_per_browser
        self._recycle_after = recycle_after
        # Each entry: {"playwright": ..., "browser": ..., "context_count": int, "created_at": float}
        self._browsers: list[dict | None] = []

    def start(self):
        for _ in range(self._max_browsers):
            self._add_browser()
        log.info("Browser grid started | browsers=%d", len(self._browsers))

    def _add_browser(self):
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        self._browsers.append({
            "playwright": pw,
            "browser": browser,
            "context_count": 0,
            "created_at": time.time(),
        })
        log.info("Browser grid added browser %d/%d", len(self._browsers), self._max_browsers)

    def _should_recycle(self, b: dict) -> bool:
        if b["browser"] is None:
            return True
        if b["context_count"] >= self._max_contexts_per_browser:
            return True
        if b["created_at"] and time.time() - b["created_at"] > self._recycle_after:
            return True
        return False

    def _recycle_browser(self, idx: int):
        b = self._browsers[idx]
        if b is None:
            return
        try:
            b["browser"].close()
        except Exception:
            pass
        try:
            b["playwright"].stop()
        except Exception:
            pass
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        self._browsers[idx] = {
            "playwright": pw,
            "browser": browser,
            "context_count": 0,
            "created_at": time.time(),
        }
        log.info("Browser grid recycled browser %d", idx)

    def new_context(self, user_agent: str):
        # Find the least-loaded browser that doesn't need recycling
        best_idx = None
        best_count = None
        for i, b in enumerate(self._browsers):
            if b is None or b["browser"] is None:
                continue
            if self._should_recycle(b):
                self._recycle_browser(i)
                b = self._browsers[i]
            if best_count is None or b["context_count"] < best_count:
                best_idx = i
                best_count = b["context_count"]

        if best_idx is None:
            # All browsers exhausted or None; add one if under max
            if len(self._browsers) < self._max_browsers:
                self._add_browser()
                best_idx = len(self._browsers) - 1
                b = self._browsers[best_idx]
            else:
                # Fall back to browser 0 (will be recycled on next call)
                best_idx = 0
                b = self._browsers[0]

        context = b["browser"].new_context(user_agent=user_agent)
        b["context_count"] += 1
        return context

    def stop(self):
        for b in self._browsers:
            if b is None:
                continue
            if b["browser"] is not None:
                try:
                    b["browser"].close()
                except Exception:
                    pass
            if b["playwright"] is not None:
                try:
                    b["playwright"].stop()
                except Exception:
                    pass
        self._browsers = []


# ---------------------------------------------------------------------------
# Domain rate limiter — don't hammer a single host
# ---------------------------------------------------------------------------

class DomainRateLimiter:
    """Enforces a minimum delay between requests to the same domain."""

    def __init__(self, min_delay: float = 2.0):
        self._min_delay = min_delay
        self._last_request: dict[str, float] = {}

    def wait_if_needed(self, url: str) -> None:
        domain = urlparse(url).netloc
        now = time.time()
        last = self._last_request.get(domain, 0.0)
        elapsed = now - last
        if elapsed < self._min_delay:
            sleep_for = self._min_delay - elapsed
            log.debug("Rate limiting %s: sleeping %.1fs", domain, sleep_for)
            time.sleep(sleep_for)
        self._last_request[domain] = time.time()


# ---------------------------------------------------------------------------
# robots.txt respect — don't scrape disallowed paths
# ---------------------------------------------------------------------------

class RobotsChecker:
    """Caches and consults robots.txt before allowing a scrape."""

    def __init__(self, cache_ttl: int = 3600):
        self._cache: dict[str, robotparser.RobotFileParser] = {}
        self._cache_time: dict[str, float] = {}
        self._cache_ttl = cache_ttl

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        domain = parsed.netloc
        now = time.time()

        if domain in self._cache and now - self._cache_time.get(domain, 0.0) < self._cache_ttl:
            rp = self._cache[domain]
        else:
            rp = robotparser.RobotFileParser()
            try:
                rp.set_url(f"{parsed.scheme}://{domain}/robots.txt")
                rp.read()
                self._cache[domain] = rp
                self._cache_time[domain] = now
            except Exception as exc:
                log.warning("robots.txt fetch failed for %s: %s", domain, exc)
                return True  # Fail open: if we can't read robots.txt, allow

        return rp.can_fetch("*", url)


# ---------------------------------------------------------------------------
# Simple heap-based scheduler — replaces while True / sleep(2)
# ---------------------------------------------------------------------------

class SimpleScheduler:
    """A dependency-free scheduler that runs callables at fixed intervals.

    Uses a heap so the next due job is always O(1) to find.  No external
    packages required.
    """

    def __init__(self):
        self._heap: list[tuple[float, int, float, object, tuple]] = []
        self._counter = 0

    def every(self, seconds: float, func, *args) -> None:
        run_at = time.time() + seconds
        heapq.heappush(self._heap, (run_at, self._counter, seconds, func, args))
        self._counter += 1

    def run_pending(self) -> None:
        now = time.time()
        while self._heap and self._heap[0][0] <= now:
            _run_at, _counter, interval, func, args = heapq.heappop(self._heap)
            try:
                func(*args)
            except Exception as exc:
                log.exception("Scheduled job failed: %s", exc)
            heapq.heappush(self._heap, (now + interval, self._counter, interval, func, args))
            self._counter += 1

    def idle_seconds(self) -> float | None:
        if not self._heap:
            return None
        return max(0.0, self._heap[0][0] - time.time())


# ---------------------------------------------------------------------------
# Main — event-driven scrape loop
# ---------------------------------------------------------------------------

def main():
    log.info("Starting Data Alchemist")
    store = EncryptedStore(DB_PATH, KEY_FILE)
    log.info("Store initialized | DB=%s | Records=%d", DB_PATH, store.capture_count())
    run_hooks("startup", {"db_path": DB_PATH})
    media_base = Path(MEDIA_DIR)
    media_base.mkdir(parents=True, exist_ok=True)

    grid = BrowserGrid(
        max_browsers=int(os.getenv("BROWSER_MAX_BROWSERS", "3")),
        max_contexts_per_browser=int(os.getenv("BROWSER_MAX_CONTEXTS", "10")),
        recycle_after=int(os.getenv("BROWSER_RECYCLE_AFTER", "3600")),
    )
    grid.start()

    rate_limiter = DomainRateLimiter(
        min_delay=float(os.getenv("SCRAPE_DOMAIN_MIN_DELAY", "2.0")),
    )
    robots = RobotsChecker()
    scheduler = SimpleScheduler()

    def scrape_target(target):
        url = target.get("url") or os.getenv("TARGET_URL", "").strip()
        if not url:
            return

        # Domain rate limiting
        rate_limiter.wait_if_needed(url)

        # robots.txt respect
        if not robots.is_allowed(url):
            log.warning("robots.txt disallows scraping: %s", url)
            try:
                capture_id = store.store(url, json.dumps({"error": "robots.txt disallowed"}), status="error")
                notify_server(capture_id, url, "error")
            except Exception:
                pass
            return

        try:
            context = grid.new_context(user_agent=build_headers()["User-Agent"])
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
                    context.close()
                    return
            try:
                title = safe_text(page.title(), 300)
                body = safe_text(page.inner_text("body"), 4000)
            except Exception as exc:
                log.warning("Partial extraction fallback: %s", exc)
                title = safe_text(page.title(), 300)
                body = safe_text(page.content(), 2000)
            payload = json.dumps({"url": url, "title": title, "body": body})
            capture_id = store.store(url, payload, status="ok")
            notify_server(capture_id, url, "ok")
            try:
                media_urls = _extract_media_urls(page.content(), url)
                media_items = _download_media(media_base, capture_id, media_urls)
                store.store_media(capture_id, media_items)
                log.info(
                    "Media captured | id=%d media=%d",
                    capture_id,
                    len(media_items),
                )
            except Exception as exc:
                log.warning("Media capture failed: %s", exc)
            run_hooks("post_capture", {"url": url, "status": "ok", "capture_id": capture_id})
            log.info("Captured and encrypted | title=%s", title or "(none)")
            context.close()
        except Exception as exc:
            log.exception("Scrape failed: %s", exc)
            try:
                capture_id = store.store(url, json.dumps({"error": str(exc)}), status="error")
                notify_server(capture_id, url, "error")
                run_hooks("post_capture", {"url": url, "status": "error", "error": str(exc)})
            except Exception:
                pass

    def check_manual_trigger():
        manual_path = Path(os.environ.get("GHOST_DATA_DIR", "data")) / "manual_target.json"
        if manual_path.exists():
            try:
                data = json.loads(manual_path.read_text(encoding="utf-8"))
                if data.get("once"):
                    log.info("One-shot manual scrape: %s", data.get("url"))
                    scrape_target(data)
                    manual_path.unlink()
                else:
                    scrape_target(data)
            except Exception as exc:
                log.warning("Manual trigger parse failed: %s", exc)

    # Schedule all configured targets at their configured intervals
    for target in load_targets():
        interval = int(target.get("interval") or os.getenv("SCRAPE_INTERVAL") or 60)
        scheduler.every(interval, scrape_target, target)
        log.info("Scheduled target | url=%s interval=%ds", target.get("url"), interval)

    # Also schedule the env TARGET_URL if present
    env_url = os.getenv("TARGET_URL", "").strip()
    if env_url:
        interval = int(os.getenv("SCRAPE_INTERVAL") or 60)
        scheduler.every(interval, scrape_target, {"url": env_url, "interval": interval, "enabled": True})
        log.info("Scheduled env target | url=%s interval=%ds", env_url, interval)

    try:
        while True:
            check_manual_trigger()
            scheduler.run_pending()
            idle = scheduler.idle_seconds()
            if idle is None:
                time.sleep(1)
            else:
                time.sleep(min(idle, 1.0))
    except KeyboardInterrupt:
        log.info("Shutdown requested")
    finally:
        grid.stop()
        store.close()
        log.info("Store closed")


if __name__ == "__main__":
    main()
