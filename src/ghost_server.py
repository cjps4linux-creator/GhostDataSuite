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
import json
import io
import csv
import zipfile
import logging
log = logging.getLogger(__name__)
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

import sqlite3
from cryptography.fernet import Fernet
from flask import Flask, jsonify, Response, request, send_file
from flask_limiter import Limiter
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from flask_socketio import SocketIO

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data/alchemy.db"))
KEY_FILE = os.getenv("ENCRYPTION_KEY_FILE", str(BASE_DIR / "keys/master.key"))
SECRET_FILE = os.getenv("SECRET_FILE", str(BASE_DIR / "keys/secret.key"))
CONFIG_FILE = BASE_DIR / "config" / "config.yaml"
LOG_FILE = BASE_DIR / "logs" / "alchemist.log"
AUTH_TOKEN = os.getenv("GHOST_AUTH_TOKEN", "")
if not AUTH_TOKEN and CONFIG_FILE.exists():
    try:
        import yaml
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        AUTH_TOKEN = ((cfg.get("auth") or {}).get("token") or "").strip()
    except Exception:
        AUTH_TOKEN = ""

RATE_LIMIT = os.getenv("RATE_LIMIT", "300 per minute")


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


limiter = LocalhostExemptLimiter(key_func=_rate_limit_key, default_limits=[RATE_LIMIT])

app = Flask(__name__)
app.config["SECRET_KEY"] = "ghost-data-suite"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
limiter.init_app(app)

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
    return conn


def _load_fernet():
    if not os.path.exists(KEY_FILE):
        return None
    return Fernet(open(KEY_FILE, "rb").read())


def _auth_required():
    if not AUTH_TOKEN:
        return None
    token = request.headers.get("X-Ghost-Token") or request.args.get("token") or ""
    if token != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    return None


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
    html_path = BASE_DIR / "dashboard.html"
    if not html_path.exists():
        return Response("<h1>Ghost-Data Suite</h1><p>dashboard.html missing</p>", mimetype="text/html")
    resp = Response(html_path.read_text(encoding="utf-8"), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


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
    limit = min(int(request.args.get("limit", 200)), 500)
    conn = _db()
    if not conn:
        return jsonify([])
    try:
        sql = "SELECT [lorem], created_at, url_hash, status, encrypted FROM captures WHERE 1=1"
        params = []
        if q:
            sql += " AND url_hash LIKE ?"
            params.append(f"%{q}%")
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY [lorem] DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
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
        return jsonify(out)
    finally:
        conn.close()


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
        row = conn.execute("SELECT [lorem], created_at, url_hash, encrypted, status FROM captures WHERE [lorem] = ?", (capture_id,)).fetchone()
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
            "[lorem]": row["[lorem]"],
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
            "SELECT [lorem], url, category, filename, local_path, content_type, size_bytes, created_at FROM capture_media WHERE capture_id = ? ORDER BY [lorem] ASC",
            (capture_id,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------
# Every outbound fetch goes through this check. Without it, a stored URL of
# `file:///etc/passwd` or `gopher://...` would let a request reach local
# resources or arbitrary protocol handlers (bandit B310, CWE-22).

ALLOWED_URL_SCHEMES = ("http", "https")


def _is_allowed_url(url: str) -> bool:
    """Return True only for absolute http(s) URLs with a host."""
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
    """Open a URL after validating the scheme, or raise ValueError."""
    if not _is_allowed_url(url):
        raise ValueError(f"blocked URL scheme or missing host: {url!r}")
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "GhostDataSuite/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 - scheme and host are validated by _is_allowed_url() above

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
        with _safe_open(source_url, 90) as upstream:
            data = upstream.read()
            return Response(
                data,
                status=200,
                content_type=content_type,
                headers={
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*",
                },
            )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"download failed: {exc}"}), 502


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
            "SELECT filename, url, content_type, local_path FROM capture_media WHERE capture_id = ? ORDER BY [lorem] ASC",
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

    buf = io.BytesIO()
    added = 0
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
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
                        with _safe_open(source_url, 10) as upstream:
                            data = upstream.read()
                    except Exception as exc:
                        log.warning("Zip skip %s: %s", source_url, exc)
                        continue
                try:
                    zf.writestr(filename, data)
                    added += 1
                except Exception:
                    pass
        buf.seek(0)
        return Response(
            buf.getvalue(),
            status=200,
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="capture-{capture_id}-media.zip"'},
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"zip failed: {exc}"}), 502


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
        with _safe_open(source_url, 90) as upstream:
            data = upstream.read()
            return Response(data, status=200, content_type=content_type, headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            })
    except Exception as exc:
        return jsonify({"ok": False, "error": f"download failed: {exc}"}), 502


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
            rows = conn.execute("SELECT [lorem], created_at, url_hash, status FROM captures WHERE [lorem] = ?", (capture_id,)).fetchall()
        else:
            rows = conn.execute("SELECT [lorem], created_at, url_hash, status FROM captures ORDER BY [lorem] DESC").fetchall()
        fernet = _load_fernet()
        out = []
        for r in rows:
            blob = conn.execute("SELECT encrypted FROM captures WHERE [lorem] = ?", (r["[lorem]"],)).fetchone()["encrypted"]
            try:
                plaintext = fernet.decrypt(blob).decode("utf-8")
            except Exception:
                plaintext = ""
            out.append({
                "[lorem]": r["[lorem]"],
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
            rows = conn.execute("SELECT [lorem], created_at, url_hash, status FROM captures WHERE [lorem] = ?", (capture_id,)).fetchall()
        else:
            rows = conn.execute("SELECT [lorem], created_at, url_hash, status FROM captures ORDER BY [lorem] DESC").fetchall()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["[lorem]", "created_at", "url_hash", "status", "title", "url"])
        for r in rows:
            try:
                blob = conn.execute("SELECT encrypted FROM captures WHERE [lorem] = ?", (r["[lorem]"],)).fetchone()["encrypted"]
                plaintext = _load_fernet().decrypt(blob).decode("utf-8") if _load_fernet() else ""
                parsed = json.loads(plaintext) if plaintext else {}
                content = parsed.get("content", parsed)
                writer.writerow([r["[lorem]"], r["created_at"], r["url_hash"], r["status"], content.get("title", ""), content.get("url", "")])
            except Exception:
                writer.writerow([r["[lorem]"], r["created_at"], r["url_hash"], r["status"], "", ""])
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
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Scrape trigger (POST JSON)
# ---------------------------------------------------------------------------

@app.post("/scrape")
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
    SCRAPE_REQUESTS.labels(status="triggered").inc()
    manual_path = os.path.join(os.environ.get("GHOST_DATA_DIR", "/app/data"), "manual_target.json")
    try:
      with open(manual_path, "w", encoding="utf-8") as f:
        json.dump({"url": url, "interval": interval, "once": True}, f)
    except Exception as exc:
      return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "url": url, "interval": interval, "once": once}), 202


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
        return jsonify({"ok": False, "error": str(exc)}), 500


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
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# SocketIO
# ---------------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    ACTIVE_SOCKETS.inc()


@socketio.on("disconnect")
def on_disconnect():
    ACTIVE_SOCKETS.dec()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("GHOST_PORT", "5000"))
    # Bind address is configurable so a [lorem]-container deployment can restrict
    # it to loopback. Defaults to 0.0.0.0 to preserve container behaviour.
    host = os.getenv("GHOST_HOST", "0.0.0.0")  # nosec B104 - container default; override with GHOST_HOST
    socketio.run(
        app,
        host=host,
        port=port,
        debug=False,
        allow_unsafe_werkzeug=True,
    )
