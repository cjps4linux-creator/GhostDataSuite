"""
Ghost-Data Suite  -  Hardened Ghost Server
- Token auth via X-Ghost-Token
- Rate limiting
- POST /scrape with JSON body
- Export/search endpoints
- Prometheus metrics
- SocketIO log streaming
- Backup/restore API
"""

from __future__ import annotations

import os
import warnings
warnings.filterwarnings("ignore", message=".*in-memory storage for tracking rate limits.*")
import sys
import json

# Optional Redis storage for Flask-Limiter (production)
try:
    import redis as _redis  # noqa: F401  (imported to ensure availability)
except Exception:  # pragma: no cover - redis is optional in local dev
    _redis = None
import io
import csv
import zipfile
import logging
log = logging.getLogger(__name__)
from pathlib import Path
from datetime import datetime, timezone

import sqlite3
from cryptography.fernet import Fernet
from flask import Flask, jsonify, Response, request, send_file, stream_with_context
from flask_limiter import Limiter
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from flask_socketio import SocketIO, emit

from urllib.parse import urlparse
from security import safe_open

import threading
import time as _time
import zipfile as _zipfile

# ---------------------------------------------------------------------------
# Automated backup scheduler (runs in a background daemon thread)
# ---------------------------------------------------------------------------

_BACKUP_INTERVAL_SEC = int(os.getenv("BACKUP_INTERVAL_SEC", "86400"))  # 24h default
_BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7"))  # keep last N backups


def _backup_dir() -> Path:
    return Path(os.getenv("BACKUP_DIR", str(DATA_DIR / "backups")))


def _rotate_backups():
    """Keep only the most recent _BACKUP_KEEP backup files."""
    try:
        bdir = _backup_dir()
        backups = sorted(bdir.glob("ghost-backup-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[_BACKUP_KEEP:]:
            old.unlink()
    except Exception:
        pass


def _backup_once() -> None:
    """Perform one backup of DB + encryption key + logs."""
    try:
        bdir = _backup_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = bdir / f"ghost-backup-{ts}.zip"
        with _zipfile.ZipFile(backup_path, "w", _zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(DB_PATH):
                zf.write(DB_PATH, "alchemy.db")
            if os.path.exists(KEY_FILE):
                zf.write(KEY_FILE, "master.key")
            if os.path.exists(LOG_FILE):
                zf.write(LOG_FILE, "alchemist.log")
        _rotate_backups()
        log.info("Automated backup created | path=%s", backup_path)
    except Exception as exc:
        log.warning("Automated backup failed: %s", exc)


def _backup_loop() -> None:
    """Background loop: run _backup_once every BACKUP_INTERVAL_SEC seconds."""
    while True:
        _time.sleep(_BACKUP_INTERVAL_SEC)
        _backup_once()


# Start the backup scheduler as a daemon thread (only when running as main app)
_backup_thread: threading.Thread | None = None

# RQ task queue — scrape jobs are enqueued here and processed by worker(s)
try:
    from redis import Redis as _Redis
    from rq import Queue as _RQQueue
    _redis_client = _Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    # Ping Redis to verify it's actually reachable; if not, fall back to None
    # so the scrape endpoint can run synchronously instead of failing with 500.
    _redis_client.ping()
    queue = _RQQueue("ghost-scrapes", connection=_redis_client)
except Exception:  # pragma: no cover - Redis optional in local dev
    queue = None
    log.debug("Redis not available at %s; scrape jobs will run synchronously",
                os.getenv("REDIS_URL", "redis://localhost:6379/0"))

# ---------------------------------------------------------------------------#
# Config
# ---------------------------------------------------------------------------#

def _resource_dir():
    """Directory containing bundled resources (dashboard.html, static/, config/).

    When running as a PyInstaller bundle, these are extracted to sys._MEIPASS.
    When running from source, this is the project root.
    """
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _data_dir():
    """Writable directory for user data (database, keys, logs, media).

    Defaults to the current working directory so the .exe can be run from
    any folder and data lands next to it.
    """
    return Path.cwd()


RESOURCE_DIR = _resource_dir()
DATA_DIR = _data_dir()

BASE_DIR = RESOURCE_DIR  # kept for backwards compatibility in imports
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "data/alchemy.db"))
KEY_FILE = os.getenv("ENCRYPTION_KEY_FILE", str(DATA_DIR / "keys/master.key"))
SECRET_FILE = os.getenv("SECRET_FILE", str(DATA_DIR / "keys/secret.key"))
CONFIG_FILE = RESOURCE_DIR / "config" / "config.yaml"
LOG_FILE = DATA_DIR / "logs" / "alchemist.log"

# Playwright browsers path — needed for scraping in PyInstaller .exe bundles
# Set to %LOCALAPPDATA%\ms-playwright by the launcher; fall back to common locations
if not os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
    _default_browser_paths = [
        os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright"),
        os.path.expanduser("~/.cache/ms-playwright"),
    ]
    for _p in _default_browser_paths:
        if os.path.isdir(_p):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _p
            log.info("Playwright browsers path set to %s", _p)
            break

# Startup browser check — verify Playwright can find a Chromium browser.
# If none is found, scraping will fail at runtime with an opaque error;
# surface a clear warning now instead.
_browser_check_ok = False
try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        _browsers = p.chromium.executable_path
        if _browsers and os.path.exists(_browsers):
            _browser_check_ok = True
            log.info("Playwright Chromium found at %s", _browsers)
except Exception as _exc:  # pragma: no cover - best-effort startup check
    log.warning("Playwright browser check failed: scraping will not work", exc_info=_exc)

if not _browser_check_ok:
    log.warning(
        "No Playwright Chromium browser detected. Set PLAYWRIGHT_BROWSERS_PATH "
        "or run 'playwright install chromium'. Scraping will fail until resolved."
    )

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------
from logging_config import setup_json_logging

log = setup_json_logging(
    "ghost-server",
    log_file=str(LOG_FILE),
    level=os.getenv("LOG_LEVEL", "INFO"),
    json_logs=os.getenv("JSON_LOGS", "true").lower() == "true",
)

# ---------------------------------------------------------------------------
# OpenTelemetry distributed tracing
# ---------------------------------------------------------------------------
_otel = None
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otel_endpoint:
        provider = TracerProvider()
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint))
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)

        RequestsInstrumentor().instrument()
        _otel = trace.get_tracer(__name__)
        log.info("OpenTelemetry tracing enabled", endpoint=otel_endpoint)
    else:
        log.info("OpenTelemetry tracing disabled (OTEL_EXPORTER_OTLP_ENDPOINT not set)")
except Exception as exc:  # pragma: no cover - OTel is optional
    log.debug("OpenTelemetry not available: %s", str(exc))
    _otel = None

# Module-level tracer for custom spans
tracer = _otel

_AUTH_TOKEN_FROM_ENV = bool(os.getenv("GHOST_AUTH_TOKEN"))
AUTH_TOKEN = os.getenv("GHOST_AUTH_TOKEN")
if not AUTH_TOKEN:
    import secrets
    AUTH_TOKEN = secrets.token_hex(16)
    log.info("GHOST_AUTH_TOKEN not set; auto-generated ephemeral token")

_SECRET_KEY_FROM_ENV = bool(os.getenv("SECRET_KEY"))
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    import secrets
    SECRET_KEY = secrets.token_hex(32)
    log.info("SECRET_KEY not set; auto-generated ephemeral key")

RATE_LIMIT = os.getenv("RATE_LIMIT", "300 per minute")
REDIS_URL = os.getenv("REDIS_URL")


def _rate_limit_key():
    try:
        forwarded = request.headers.get("X-Forwarded-For", "")
        ip = forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "")
        if ip in {"127.0.0.1", "::1", "172.18.0.1", "172.17.0.1", "172.19.0.1"}:
            return "localhost"
        return ip
    except RuntimeError:
        return "localhost"


class LocalhostExemptLimiter(Limiter):
    def should_exempt(self, request):
        ip = request.remote_addr or ""
        if ip in {"127.0.0.1", "::1", "172.18.0.1", "172.17.0.1", "172.19.0.1"}:
            return True
        return False


limiter_kwargs = {"key_func": _rate_limit_key, "default_limits": [RATE_LIMIT]}
if REDIS_URL:
    limiter_kwargs["storage_uri"] = REDIS_URL
    limiter_kwargs["storage_options"] = {"socket_timeout": 5, "socket_connect_timeout": 5}

limiter = LocalhostExemptLimiter(**limiter_kwargs)

app = Flask(__name__, static_folder=str(RESOURCE_DIR / "static"), static_url_path="/static")
app.config["SECRET_KEY"] = SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
limiter.init_app(app)

# Flask instrumentation for OpenTelemetry (must run after app is created)
if tracer:
    from opentelemetry.instrumentation.flask import FlaskInstrumentor

    FlaskInstrumentor().instrument_app(app)


@app.after_request
def _set_auth_cookie(response):
    """Set an httpOnly cookie when a valid X-Ghost-Token header is sent.

    This lets clients authenticate once via the header and thereafter rely on
    the cookie, so tokens never need to appear in URLs or localStorage.
    """
    token = request.headers.get("X-Ghost-Token")
    if token and token == AUTH_TOKEN:
        response.set_cookie(
            "ghost_token",
            token,
            httponly=True,
            samesite="Lax",
            path="/",
        )
    return response


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
SCRAPE_REQUESTS = Counter("ghost_scrape_requests_total", "Total scrape requests", ["status"])
DECRYPT_REQUESTS = Counter("ghost_decrypt_requests_total", "Total decrypt requests", ["status"])
CAPTURE_COUNT = Gauge("ghost_capture_count", "Current capture count")
ACTIVE_SOCKETS = Gauge("ghost_active_sockets", "Active SocketIO connections")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _load_fernet():
    if not os.path.exists(KEY_FILE):
        return None
    return Fernet(open(KEY_FILE, "rb").read())


def _auth_required():
    token = request.headers.get("X-Ghost-Token") or request.cookies.get("ghost_token") or ""
    if token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    return None


def _error_response(message: str, status: int = 500, log_exc: Exception | None = None):
    """Return a sanitized error response to the client.

    Full tracebacks are logged server-side only; clients never see
    file paths, line numbers, or exception internals.
    """
    if log_exc is not None:
        log.exception("Request failed: %s", message, exc_info=log_exc)
    else:
        log.error("Request failed: %s", message)
    return jsonify({"ok": False, "error": message}), status


def _emit_log(line: str):
    try:
        socketio.emit("log", {"line": line})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/")
def dashboard():
    html_path = RESOURCE_DIR / "dashboard.html"
    if not html_path.exists():
        return Response("<h1>Ghost-Data Suite</h1><p>dashboard.html missing</p>", mimetype="text/html")
    resp = Response(html_path.read_text(encoding="utf-8"), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/static/auth-config.js")
def serve_auth_config():
    """Serve auth config JS.

    When GHOST_AUTH_TOKEN is set via env var (launcher), serve the
    runtime-written auth-config.js file. When auto-generated (direct .exe
    launch), serve the token directly so it always matches AUTH_TOKEN.
    """
    # When token was auto-generated (no env var), serve it directly
    # so it always matches the server's AUTH_TOKEN
    if not _AUTH_TOKEN_FROM_ENV:
        return (
            f"window.GHOST_AUTH_TOKEN = '{AUTH_TOKEN}';"
            f"window.GHOST_SESSION_TOKEN = '{AUTH_TOKEN}';",
            200,
            {"Content-Type": "application/javascript", "Cache-Control": "no-cache"},
        )
    # Token from env var — serve the file written by the launcher
    runtime_path = DATA_DIR / "static" / "auth-config.js"
    if runtime_path.exists():
        response = send_file(str(runtime_path), mimetype="application/javascript")
        response.headers["Cache-Control"] = "no-cache"
        return response
    bundled_path = RESOURCE_DIR / "static" / "auth-config.js"
    if bundled_path.exists():
        response = send_file(str(bundled_path), mimetype="application/javascript")
        response.headers["Cache-Control"] = "no-cache"
        return response
    return (
        f"window.GHOST_AUTH_TOKEN = '{AUTH_TOKEN}';"
        f"window.GHOST_SESSION_TOKEN = '{AUTH_TOKEN}';",
        200,
        {"Content-Type": "application/javascript", "Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Health / Status
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "ghost-server",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/status")
def status():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    conn = _db()
    capture_count = 0
    newest = oldest = None
    db_exists = conn is not None
    if conn:
        try:
            cur = conn.execute("SELECT COUNT(*), MAX(created_at), MIN(created_at) FROM captures")
            row = cur.fetchone()
            capture_count = row[0] or 0
            newest = row[1]
            oldest = row[2]
            CAPTURE_COUNT.set(capture_count)
        except Exception:
            pass
        finally:
            conn.close()

    return jsonify({
        "service": "ghost-server",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "db": {"exists": db_exists, "capture_count": capture_count, "newest": newest, "oldest": oldest},
        "key_exists": os.path.exists(KEY_FILE),
        "env": {
            "db_path": DB_PATH,
            "key_file": KEY_FILE,
            "silent_mode": os.getenv("SILENT_MODE", "false"),
            "auth_enabled": bool(AUTH_TOKEN),
        },
    })


# ---------------------------------------------------------------------------
# Metrics endpoint
# ---------------------------------------------------------------------------

@app.get("/metrics")
def metrics():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Captures
# ---------------------------------------------------------------------------

@app.get("/captures")
def list_captures():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    q = request.args.get("q", "")
    status = request.args.get("status", "")
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(max(int(request.args.get("per_page", 20)), 1), 100)
    offset = (page - 1) * per_page
    conn = _db()
    if not conn:
        return jsonify({"captures": [], "meta": {"total": 0, "page": page, "per_page": per_page}})
    try:
        sql_count = "SELECT COUNT(*) FROM captures WHERE 1=1"
        sql = "SELECT id, created_at, url_hash, status, encrypted FROM captures WHERE 1=1"
        params = []
        if q:
            sql_count += " AND url_hash LIKE ?"
            sql += " AND url_hash LIKE ?"
            params.append(f"%{q}%")
        if status:
            sql_count += " AND status = ?"
            sql += " AND status = ?"
            params.append(status)
        total = conn.execute(sql_count, params).fetchone()[0]
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        rows = conn.execute(sql, params + [per_page, offset]).fetchall()
        fernet = _load_fernet()
        out = []
        for r in rows:
            item = dict(r)
            try:
                plaintext = fernet.decrypt(item.pop("encrypted")).decode("utf-8") if fernet else ""
                parsed = json.loads(plaintext) if plaintext else {}
                content = parsed.get("content", parsed)
                item["url"] = content.get("url", "")
            except Exception:
                item["url"] = ""
            out.append(item)
        return jsonify({
            "captures": out,
            "meta": {"total": total, "page": page, "per_page": per_page},
        })
    finally:
        conn.close()


@app.post("/captures/bulk")
def bulk_delete_captures():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "ids list required"}), 400
    conn = _db()
    if not conn:
        return jsonify({"ok": False, "error": "database missing"}), 500
    try:
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"DELETE FROM captures WHERE id IN ({placeholders})", ids
        )
        deleted = cur.rowcount
        conn.execute("DELETE FROM capture_media WHERE capture_id IN (" + placeholders + ")", ids)
        conn.commit()
        CAPTURE_COUNT.set(conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
        return jsonify({"ok": True, "deleted": deleted})
    finally:
        conn.close()


@app.post("/notify/capture")
def notify_capture():
    """Endpoint called by the scraper after storing a capture.

    Emits a SocketIO ``capture_created`` event so connected dashboards
    can refresh in real time without polling.
    """
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    capture_id = data.get("id")
    url = data.get("url", "")
    status = data.get("status", "ok")
    socketio.emit("capture_created", {"id": capture_id, "url": url, "status": status})
    conn2 = _db()
    if conn2:
        CAPTURE_COUNT.set(conn2.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
        conn2.close()
    return jsonify({"ok": True})


@app.get("/captures/<int:capture_id>/decrypt")
def decrypt_capture(capture_id: int):
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    fernet = _load_fernet()
    if not fernet:
        DECRYPT_REQUESTS.labels(status="error").inc()
        return jsonify({"error": "encryption key missing"}), 500

    conn = _db()
    if not conn:
        DECRYPT_REQUESTS.labels(status="error").inc()
        return jsonify({"error": "database missing"}), 404
    try:
        row = conn.execute("SELECT id, created_at, url_hash, encrypted, status FROM captures WHERE id = ?", (capture_id,)).fetchone()
        if not row:
            DECRYPT_REQUESTS.labels(status="not_found").inc()
            return jsonify({"error": "capture not found"}), 404
        try:
            plaintext = fernet.decrypt(row["encrypted"]).decode("utf-8")
            DECRYPT_REQUESTS.labels(status="ok").inc()
        except Exception as exc:
            DECRYPT_REQUESTS.labels(status="error").inc()
            return jsonify({"error": f"decryption failed: {exc}"}), 400
        try:
            parsed = json.loads(plaintext)
        except Exception:
            parsed = {"raw": plaintext}
        return jsonify({
            "id": row["id"],
            "created_at": row["created_at"],
            "url_hash": row["url_hash"],
            "status": row["status"],
            "content": parsed,
        })
    finally:
        conn.close()


@app.get("/captures/<int:capture_id>/media")
def list_capture_media(capture_id: int):
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    conn = _db()
    if not conn:
        return jsonify([])
    try:
        rows = conn.execute(
            "SELECT id, url, category, filename, local_path, content_type, size_bytes, created_at FROM capture_media WHERE capture_id = ? ORDER BY id ASC",
            (capture_id,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/app/media"))

def _local_media_path(capture_id: int, filename: str) -> Path | None:
    """Resolve a media file inside MEDIA_ROOT, or None.

    The filename arrives from the URL and may contain traversal sequences
    (``../``, encoded or not). The candidate is resolved and then verified
    to be inside MEDIA_ROOT before it is served  -  otherwise a request such as
    ``/local-media/1/..%2F..%2Fsecret.txt`` would read arbitrary files.
    """
    root = MEDIA_ROOT.resolve()
    # Flatten separators so the value can only ever name a single file.
    safe_name = filename.replace("\\", "/").split("/")[-1]
    if not safe_name or safe_name in {".", ".."}:
        return None

    candidate = (root / str(capture_id) / safe_name)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    # Must be inside MEDIA_ROOT/<capture_id>/ and must not have escaped via ..
    if not resolved.is_relative_to(root / str(capture_id)):
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    return resolved

@app.get("/local-media/<int:capture_id>/<path:filename>")
def local_media(capture_id: int, filename: str):
    path = _local_media_path(capture_id, filename)
    if not path:
        return jsonify({"ok": False, "error": "not found"}), 404
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".avif": "image/avif",
        ".ico": "image/x-icon",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")
    return send_file(
        str(path),
        mimetype=mime,
        as_attachment=False,
        download_name=Path(filename).name,
        etag=True,
        conditional=True,
    )


def _normalize_media_url(url: str) -> str:
    if not url:
        return url
    url = url.replace("\\", "/")
    parsed = urlparse(url)
    netloc = parsed.netloc
    path = parsed.path
    if netloc:
        segment = f"//{netloc}/"
        if segment in path:
            path = path.replace(segment, "/")
    while "//" in path:
        path = path.replace("//", "/")
    return parsed._replace(path=path).geturl()

@app.get("/public/media/<int:capture_id>/<path:filename>")
def public_media(capture_id: int, filename: str):
    conn = _db()
    if not conn:
        return jsonify({"ok": False, "error": "no db"}), 500
    try:
        row = conn.execute(
            "SELECT url, content_type FROM capture_media WHERE capture_id = ? AND filename = ? LIMIT 1",
            (capture_id, filename),
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        source_url = _normalize_media_url(row["url"])
        content_type = row["content_type"] or ""
    finally:
        conn.close()

    if not content_type:
        ext = Path(filename).suffix.lower()
        content_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".avif": "image/avif",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")

    try:
        upstream = safe_open(source_url, 90)

        def generate():
            try:
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(generate()),
            status=200,
            content_type=content_type,
            headers={
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except Exception as exc:
        return _error_response("download failed", 502, exc)


@app.get("/download/all/<int:capture_id>")
def download_all_media(capture_id: int):
    auth_err = _auth_required()
    if auth_err:
        token = request.args.get("token") or ""
        if token != AUTH_TOKEN:
            return auth_err
    conn = _db()
    if not conn:
        return jsonify({"ok": False, "error": "no db"}), 500
    try:
        rows = conn.execute(
            "SELECT filename, url, content_type, local_path FROM capture_media WHERE capture_id = ? ORDER BY id ASC",
            (capture_id,),
        ).fetchall()
        if not rows:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("README.txt", "No media files available for this capture.")
            buf.seek(0)
            return Response(
                buf.getvalue(),
                mimetype="application/zip",
                headers={"Content-Disposition": f"attachment; filename=capture-{capture_id}-media.zip"},
            )
    finally:
        conn.close()

    tmp_path = Path(f"/tmp/ghost-export-{capture_id}-{os.getpid()}.zip")
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for row in rows:
                filename = row["filename"]
                local_path = row["local_path"] or ""
                source_url = row["url"]
                data = None
                try:
                    if local_path:
                        candidate = Path(str(BASE_DIR / local_path))
                        if candidate.exists():
                            data = candidate.read_bytes()
                except Exception:
                    data = None
                if data is None:
                    try:
                        upstream = safe_open(source_url, 10)
                        chunks = []
                        while True:
                            chunk = upstream.read(8192)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        upstream.close()
                        data = b"".join(chunks)
                    except Exception as exc:
                        log.warning("Zip skip %s: %s", source_url, exc)
                        continue
                try:
                    zf.writestr(filename, data)
                except Exception:
                    pass

        def generate():
            with open(tmp_path, "rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    yield chunk
            try:
                tmp_path.unlink()
            except OSError:
                pass

        return Response(
            generate(),
            status=200,
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="capture-{capture_id}-media.zip"'},
        )
    except Exception as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return _error_response("zip failed", 502, exc)


@app.get("/media/download/<int:capture_id>/<path:filename>")
def download_media(capture_id: int, filename: str):
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    conn = _db()
    if not conn:
        return jsonify({"ok": False, "error": "no db"}), 500
    try:
        row = conn.execute(
            "SELECT url, content_type FROM capture_media WHERE capture_id = ? AND filename = ? LIMIT 1",
            (capture_id, filename),
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        source_url = row["url"]
        content_type = row["content_type"] or "application/octet-stream"
    finally:
        conn.close()

    try:
        upstream = safe_open(source_url, 90)

        def generate():
            try:
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(generate()),
            status=200,
            content_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )
    except Exception as exc:
        return _error_response("download failed", 502, exc)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@app.get("/export/json")
@app.get("/export/json/<int:capture_id>")
def export_json(capture_id: int | None = None):
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    conn = _db()
    if not conn:
        return jsonify([])
    try:
        if capture_id is not None:
            rows = conn.execute("SELECT id, created_at, url_hash, status FROM captures WHERE id = ?", (capture_id,)).fetchall()
        else:
            rows = conn.execute("SELECT id, created_at, url_hash, status FROM captures ORDER BY id DESC").fetchall()
        fernet = _load_fernet()
        out = []
        for r in rows:
            blob = conn.execute("SELECT encrypted FROM captures WHERE id = ?", (r["id"],)).fetchone()["encrypted"]
            try:
                plaintext = fernet.decrypt(blob).decode("utf-8")
            except Exception:
                plaintext = ""
            out.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "status": r["status"],
                "content": json.loads(plaintext) if plaintext else None,
            })
        payload = json.dumps(out, indent=2).encode("utf-8")
        return Response(payload, mimetype="application/json", headers={"Content-Disposition": "attachment; filename=ghost-export.json"})
    finally:
        conn.close()


@app.get("/export/csv")
@app.get("/export/csv/<int:capture_id>")
def export_csv(capture_id: int | None = None):
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    conn = _db()
    if not conn:
        return Response("", mimetype="text/csv")
    try:
        if capture_id is not None:
            rows = conn.execute("SELECT id, created_at, url_hash, status FROM captures WHERE id = ?", (capture_id,)).fetchall()
        else:
            rows = conn.execute("SELECT id, created_at, url_hash, status FROM captures ORDER BY id DESC").fetchall()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["id", "created_at", "url_hash", "status", "title", "url"])
        for r in rows:
            try:
                blob = conn.execute("SELECT encrypted FROM captures WHERE id = ?", (r["id"],)).fetchone()["encrypted"]
                plaintext = _load_fernet().decrypt(blob).decode("utf-8") if _load_fernet() else ""
                parsed = json.loads(plaintext) if plaintext else {}
                content = parsed.get("content", parsed)
                writer.writerow([r["id"], r["created_at"], r["url_hash"], r["status"], content.get("title", ""), content.get("url", "")])
            except Exception:
                writer.writerow([r["id"], r["created_at"], r["url_hash"], r["status"], "", ""])
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=ghost-export.csv"})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@app.get("/logs")
def get_logs():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    if not LOG_FILE.exists():
        return Response("", mimetype="text/plain")
    text = LOG_FILE.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()[-200:]
    return Response("\n".join(lines), mimetype="text/plain")


@app.post("/logs/clear")
def clear_logs():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    try:
        if LOG_FILE.exists():
            LOG_FILE.write_text("", encoding="utf-8")
        return jsonify({"ok": True})
    except Exception as exc:
        return _error_response("operation failed", 500, exc)


# ---------------------------------------------------------------------------
# Scrape trigger (POST JSON)
# ---------------------------------------------------------------------------

@app.post("/scrape")
@limiter.limit("5 per minute")
def scrape_trigger():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    data = request.get_json(silent=True) or {}
    url = data.get("url") or request.args.get("url") or os.getenv("TARGET_URL")
    interval = data.get("interval") or request.args.get("interval") or os.getenv("SCRAPE_INTERVAL") or "60"
    once = bool(data.get("once"))
    if not url:
        return jsonify({"ok": False, "error": "missing url"}), 400

    if tracer:
        with tracer.start_as_current_span("ghost.scrape.trigger") as span:
            span.set_attribute("scrape.url", url)
            span.set_attribute("scrape.interval", interval)
            span.set_attribute("scrape.once", once)
            SCRAPE_REQUESTS.labels(status="queued").inc()

            # Enqueue the scrape job via RQ — returns immediately, worker runs it
            # Falls back to synchronous execution when Redis/RQ is unavailable.
            try:
                if queue is not None:
                    job = queue.enqueue(
                        "tasks.scrape_job",
                        url,
                        interval,
                        once,
                        job_timeout=300,
                    )
                    span.set_attribute("rq.job_id", job.id)
                    span.set_status(trace.StatusCode.OK)
                    return jsonify({
                        "ok": True,
                        "queued": True,
                        "job_id": job.id,
                        "url": url,
                        "interval": interval,
                        "once": once,
                    }), 202
                else:
                    # Redis not available — run inline for local dev
                    _run_scrape_inline(url, interval, once)
                    span.set_status(trace.StatusCode.OK)
                    return jsonify({
                        "ok": True,
                        "queued": False,
                        "job_id": None,
                        "url": url,
                        "interval": interval,
                        "once": once,
                        "note": "Redis unavailable; ran synchronously",
                    }), 200
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                return _error_response("scrape failed", 500, exc)
    else:
        SCRAPE_REQUESTS.labels(status="queued").inc()
        try:
            if queue is not None:
                job = queue.enqueue(
                    "tasks.scrape_job",
                    url,
                    interval,
                    once,
                    job_timeout=300,
                )
                return jsonify({
                    "ok": True,
                    "queued": True,
                    "job_id": job.id,
                    "url": url,
                    "interval": interval,
                    "once": once,
                }), 202
            else:
                _run_scrape_inline(url, interval, once)
                return jsonify({
                    "ok": True,
                    "queued": False,
                    "job_id": None,
                    "url": url,
                    "interval": interval,
                    "once": once,
                    "note": "Redis unavailable; ran synchronously",
                }), 200
        except Exception as exc:
            return _error_response("operation failed", 500, exc)


@app.post("/scrape/stop")
def scrape_stop():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    manual_path = os.path.join(os.environ.get("GHOST_DATA_DIR", "/app/data"), "manual_target.json")
    try:
      if os.path.exists(manual_path):
        os.remove(manual_path)
    except Exception:
      pass
    return jsonify({"ok": True}), 202


# ---------------------------------------------------------------------------
@app.post("/db/clear")
def db_clear():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM capture_media")
            media_deleted = cur.rowcount
            cur.execute("DELETE FROM captures")
            captures_deleted = cur.rowcount
            conn.commit()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("VACUUM")
        return jsonify({
            "ok": True,
            "captures_deleted": captures_deleted,
            "media_deleted": media_deleted,
        })
    except Exception as exc:
        return _error_response("operation failed", 500, exc)


# Backup / Restore
# ---------------------------------------------------------------------------

@app.get("/backup")
def backup():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(DB_PATH):
            zf.write(DB_PATH, "alchemy.db")
        if os.path.exists(KEY_FILE):
            zf.write(KEY_FILE, "master.key")
        if os.path.exists(LOG_FILE):
            zf.write(LOG_FILE, "alchemist.log")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="ghost-backup.zip")


@app.post("/restore")
def restore():
    auth_err = _auth_required()
    if auth_err:
        return auth_err
    uploaded = request.files.get("backup")
    if not uploaded:
        return jsonify({"ok": False, "error": "missing backup file"}), 400
    try:
        with zipfile.ZipFile(uploaded.stream, "r") as zf:
            for name in ("alchemy.db", "master.key", "alchemist.log"):
                if name in zf.namelist():
                    target = BASE_DIR / ("data/alchemy.db" if name == "alchemy.db" else ("keys/master.key" if name == "master.key" else "logs/alchemist.log"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
        return jsonify({"ok": True})
    except Exception as exc:
        return _error_response("operation failed", 500, exc)


# ---------------------------------------------------------------------------
# SocketIO
# ---------------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    ACTIVE_SOCKETS.inc()


@socketio.on("disconnect")
def on_disconnect():
    ACTIVE_SOCKETS.dec()


@socketio.on("auth")
def on_auth(data):
    """Authenticate a SocketIO connection using the GHOST_AUTH_TOKEN."""
    token = (data or {}).get("token", "")
    if token == AUTH_TOKEN:
        emit("auth_ok", {"status": "authenticated"})
    else:
        emit("auth_error", {"status": "unauthorized"})
        # Disconnect unauthenticated clients after a brief allow
        from flask_socketio import disconnect as _disconnect
        _disconnect()


def _run_scrape_inline(url: str, interval: str, once: bool):
    """Run a scrape synchronously when RQ/Redis is unavailable (local dev)."""
    from tasks import scrape_job
    scrape_job(url, interval, once)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Handle --playwright-install: one-time browser installation, then exit
    if "--playwright-install" in sys.argv:
        import subprocess
        browsers_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "%LOCALAPPDATA%\\ms-playwright")
        log.info("Installing Playwright Chromium browsers | path=%s", browsers_path)
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": browsers_path},
        )
        sys.exit(result.returncode)

    # Start automated backup scheduler (daemon thread)
    _backup_thread = threading.Thread(target=_backup_loop, daemon=True, name="ghost-backup")
    _backup_thread.start()
    log.info("Automated backup scheduler started | interval=%ds keep=%d", _BACKUP_INTERVAL_SEC, _BACKUP_KEEP)

    port = int(os.getenv("GHOST_PORT", "5000"))
    # Bind address is configurable so a multi-container deployment can restrict
    # it to loopback. Defaults to 0.0.0.0 to preserve container behaviour.
    host = os.getenv("GHOST_HOST", "0.0.0.0")  # nosec B104 - container default; override with GHOST_HOST
    socketio.run(
        app,
        host=host,
        port=port,
        debug=False,
        allow_unsafe_werkzeug=True,
    )
