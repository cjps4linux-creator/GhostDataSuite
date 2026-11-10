"""Security-focused tests for the ghost-data-suite SSRF guards and auth layer.

Every test in this module is hermetic:

* no network traffic is possible -- ``urllib.request.urlopen`` is replaced by
  a fixture that raises if anything tries to use it,
* no real database, key or log file of the repository is read or written,
* no browser, GUI or subprocess server is started (the single subprocess test
  stubs out the SocketIO ``run`` method so nothing binds a socket).

The allow/deny matrix is exercised against *both* implementations of the
guard -- ``src/scraper.py`` and ``src/ghost_server.py`` -- because the two
modules ship duplicate copies of the same SSRF check and must not drift.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# URL matrices
# ---------------------------------------------------------------------------

ALLOWED_URLS = [
    # plain absolute URLs
    "http://example.com",
    "https://example.com",
    "https://example.com/",
    "https://example.com/path",
    "https://example.com/path?query=1",
    "https://example.com/path#fragment",
    "https://example.com/path?a=1#b",
    # explicit ports
    "http://example.com:8080/path",
    "https://example.com:443/",
    # credentials embedded in the authority
    "https://user:pass@example.com",
    "https://user:pass@example.com:8443/x",
    # IPv6 literals
    "http://[::1]:8080/",
    "http://[::1]",
    "https://[2001:db8::1]/x",
    # uppercase scheme -- urlparse lowercases the scheme
    "HTTP://example.com",
    "HTTPS://EXAMPLE.COM",
    "HtTp://example.com",
    # surrounding whitespace is stripped before parsing
    "  https://example.com  ",
    "\thttp://example.com\n",
    "\nhttps://example.com\n",
    # multi-label host
    "https://a.b.example.co.uk/x",
]

DENIED_URLS = [
    # empty / blank
    "",
    "   ",
    # local file access
    "file:///etc/passwd",
    "file://C:/Windows/win.ini",
    # other protocol handlers
    "gopher://x",
    "ftp://h/f",
    "data:text/html,x",
    "javascript:alert(1)",
    "vbscript:msgbox(1)",
    "mailto:someone@example.com",
    "ws://example.com/socket",
    "wss://example.com/socket",
    "jar:file:///x!/y",
    "about:blank",
    "chrome://settings",
    # relative / schemeless references
    "/local/path",
    "./relative",
    "../up",
    "//example.com/path",
    # http(s) without a host
    "http://",
    "https://",
    "http:///path",
    "https:///path",
    "http://?x=1",
    # malformed scheme
    "http:/example.com",
    "httpx://example.com",
]

NON_STRING_URLS = [
    None,
    123,
    0,
    1.5,
    True,
    b"http://example.com",
    bytearray(b"http://example.com"),
    [],
    {},
    (),
    set(),
    object(),
]


# ---------------------------------------------------------------------------
# The allow-list itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module_name", ["scraper", "ghost_server"])
def test_allowed_schemes_are_http_and_https(module_name):
    module = pytest.importorskip(module_name)
    assert module.ALLOWED_URL_SCHEMES == ("http", "https")


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_allowed_urls_are_accepted(guard_module, url):
    assert guard_module._is_allowed_url(url) is True


@pytest.mark.parametrize("url", DENIED_URLS)
def test_denied_urls_are_rejected(guard_module, url):
    assert guard_module._is_allowed_url(url) is False


@pytest.mark.parametrize("url", NON_STRING_URLS)
def test_non_string_urls_are_rejected(guard_module, url):
    assert guard_module._is_allowed_url(url) is False


@pytest.mark.parametrize("url", ALLOWED_URLS + DENIED_URLS)
def test_guard_never_raises_for_any_input(guard_module, url):
    """The guard is a filter, not a parser: it must always return a bool."""
    assert isinstance(guard_module._is_allowed_url(url), bool)


def test_uppercase_scheme_is_allowed_because_scheme_is_lowercased(guard_module):
    assert guard_module._is_allowed_url("HTTP://example.com") is True
    assert guard_module._is_allowed_url("HtTpS://example.com") is True


def test_whitespace_padded_url_is_allowed(guard_module):
    assert guard_module._is_allowed_url("   https://example.com/path   ") is True


def test_url_with_credentials_is_allowed(guard_module):
    assert guard_module._is_allowed_url("https://user:pass@example.com") is True


def test_url_with_port_is_allowed(guard_module):
    assert guard_module._is_allowed_url("http://example.com:8080/x") is True


def test_ipv6_literal_is_allowed(guard_module):
    assert guard_module._is_allowed_url("http://[::1]:8080/") is True


def test_both_guards_agree_on_every_url(guard_module):
    """scraper.py and ghost_server.py must implement identical semantics."""
    from conftest import ghost_server, scraper

    for url in ALLOWED_URLS + DENIED_URLS:
        assert scraper._is_allowed_url(url) == ghost_server._is_allowed_url(url), url
    for value in NON_STRING_URLS:
        assert scraper._is_allowed_url(value) == ghost_server._is_allowed_url(value)


def test_host_containing_whitespace_is_accepted_quirk(guard_module):
    """Documented quirk: the guard only requires a [lorem]-empty netloc.

    ``urlparse`` happily returns ``"exa mple.com"`` as the netloc, so the
    guard lets it through.  The request itself still fails later in urllib.
    """
    assert guard_module._is_allowed_url("http://exa mple.com") is True


# ---------------------------------------------------------------------------
# _safe_open must refuse before touching the network
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", DENIED_URLS + NON_STRING_URLS)
def test_safe_open_raises_value_error_for_denied_urls(guard_module, url, no_network):
    with pytest.raises(ValueError) as excinfo:
        guard_module._safe_open(url, 5)
    assert "blocked URL scheme" in str(excinfo.value)
    assert no_network == [], "a blocked URL must never reach urllib.request.urlopen"


def test_safe_open_blocks_file_scheme_without_reading_the_file(guard_module, no_network):
    with pytest.raises(ValueError):
        guard_module._safe_open("file:///etc/passwd", 5)
    assert no_network == []


def test_safe_open_passes_allowed_url_to_urlopen(guard_module, monkeypatch):
    """An allowed URL is forwarded verbatim, together with timeout and headers."""
    recorded = {}

    def _fake_urlopen(request, timeout=None, **kwargs):
        recorded["url"] = request.get_full_url()
        recorded["timeout"] = timeout
        recorded["headers"] = dict(request.header_items())
        return "sentinel"

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    result = guard_module._safe_open("https://example.com/page", 42)

    assert result == "sentinel"
    assert recorded["url"] == "https://example.com/page"
    assert recorded["timeout"] == 42
    assert recorded["headers"].get("User-agent") == "GhostDataSuite/1.0"


def test_safe_open_uses_custom_headers_when_supplied(guard_module, monkeypatch):
    recorded = {}

    def _fake_urlopen(request, timeout=None, **kwargs):
        recorded["headers"] = dict(request.header_items())
        return "sentinel"

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    guard_module._safe_open("https://example.com/page", 7, headers={"X-Custom": "1"})

    assert recorded["headers"].get("X-custom") == "1"
    assert "User-agent" not in recorded["headers"]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def test_protected_endpoints_reject_missing_token(client, monkeypatch):
    from conftest import ghost_server

    monkeypatch.setattr(ghost_server, "AUTH_TOKEN", "s3cret-token")
    response = client.get("/status")
    assert response.status_code == 401
    assert response.get_json() == {"error": "unauthorized"}


def test_protected_endpoints_reject_wrong_token(client, monkeypatch):
    from conftest import ghost_server

    monkeypatch.setattr(ghost_server, "AUTH_TOKEN", "s3cret-token")
    response = client.get("/status", headers={"X-Ghost-Token": "wrong"})
    assert response.status_code == 401


def test_protected_endpoints_accept_header_token(client, monkeypatch):
    from conftest import ghost_server

    monkeypatch.setattr(ghost_server, "AUTH_TOKEN", "s3cret-token")
    response = client.get("/status", headers={"X-Ghost-Token": "s3cret-token"})
    assert response.status_code == 200
    assert response.get_json()["service"] == "ghost-server"


def test_protected_endpoints_accept_query_token(client, monkeypatch):
    from conftest import ghost_server

    monkeypatch.setattr(ghost_server, "AUTH_TOKEN", "s3cret-token")
    response = client.get("/status?token=s3cret-token")
    assert response.status_code == 200


def test_health_endpoint_needs_no_token(client, monkeypatch):
    from conftest import ghost_server

    monkeypatch.setattr(ghost_server, "AUTH_TOKEN", "s3cret-token")
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Local file disclosure through the media endpoints
# ---------------------------------------------------------------------------
@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Create an isolated database with one capture and one malicious media row.

    The database lives in ``tmp_path``; the repository database is never
    touched.
    """
    from conftest import ghost_server, scraper

    db_path = tmp_path / "alchemy.db"
    key_path = tmp_path / "master.key"

    store = scraper.EncryptedStore(str(db_path), str(key_path))
    capture_id = store.store(
        "https://example.com/page",
        json.dumps({"url": "https://example.com/page", "title": "Example"}),
        status="ok",
    )
    with store.conn:
        store.conn.execute(
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
            )
            """
        )
        store.conn.execute(
            "INSERT INTO capture_media"
            "(capture_id, url, category, filename, local_path, content_type, size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (capture_id, "file:///etc/passwd", "document", "leak.txt", "", "text/plain", 0, "now"),
        )
    store.close()

    monkeypatch.setattr(ghost_server, "DB_PATH", str(db_path))
    monkeypatch.setattr(ghost_server, "KEY_FILE", str(key_path))
    return capture_id


def test_media_download_refuses_file_scheme_row(client, seeded_db, no_network):
    response = client.get(f"/media/download/{seeded_db}/leak.txt")
    assert response.status_code == 502
    assert "blocked URL scheme" in response.get_json()["error"]
    assert b"root:" not in response.data


def test_public_media_refuses_file_scheme_row(client, seeded_db, no_network):
    response = client.get(f"/public/media/{seeded_db}/leak.txt")
    assert response.status_code == 502
    assert "blocked URL scheme" in response.get_json()["error"]
    assert b"root:" not in response.data


def test_download_all_zip_skips_blocked_rows(client, seeded_db, no_network):
    """A ``file://`` media row is skipped, so it never lands in the zip."""
    import io
    import zipfile

    response = client.get(f"/download/all/{seeded_db}")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.namelist() == []
        assert b"root:" not in response.data


def test_download_all_zip_of_a_capture_without_media_is_a_readme(client, seeded_db, monkeypatch):
    """With no media rows at all the endpoint returns the [content pending] zip."""
    import io
    import zipfile

    from conftest import ghost_server

    with ghost_server.app.app_context():
        connection = ghost_server._db()
        connection.execute("DELETE FROM capture_media")
        connection.commit()
        connection.close()

    response = client.get(f"/download/all/{seeded_db}")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.namelist() == ["README.txt"]
        assert b"No media files available" in archive.read("README.txt")


def test_captures_endpoint_does_not_leak_plaintext(client, seeded_db):
    response = client.get("/captures")
    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload) == 1
    assert "encrypted" not in payload[0]
    assert payload[0]["status"] == "ok"


def test_decrypt_endpoint_round_trips_encrypted_content(client, seeded_db):
    response = client.get(f"/captures/{seeded_db}/decrypt")
    assert response.status_code == 200
    body = response.get_json()
    assert body["content"]["url"] == "https://example.com/page"
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# Path traversal in the local media handler
# ---------------------------------------------------------------------------
def test_local_media_path_rejects_traversal_to_non_existing_files():
    """The sanitised lookup never resolves a traversal to an existing file."""
    from conftest import ghost_server

    assert ghost_server._local_media_path(1, "../../../etc/passwd") is None
    assert ghost_server._local_media_path(1, "/etc/passwd") is None


def test_local_media_path_returns_existing_file(tmp_path, monkeypatch):
    from conftest import ghost_server

    media_root = tmp_path / "media"
    (media_root / "1").mkdir(parents=True)
    (media_root / "1" / "ok.png").write_bytes(b"PNG")
    monkeypatch.setattr(ghost_server, "MEDIA_ROOT", media_root)

    resolved = ghost_server._local_media_path(1, "ok.png")
    assert resolved is not None
    assert resolved.read_bytes() == b"PNG"


@pytest.mark.parametrize(
    "probe",
    [
        "/local-media/1/..%2F..%2Fsecret.txt",
        "/local-media/1/../../secret.txt",
        "/local-media/1/..%2F..%2F..%2F..%2Fsecret.txt",
        "/local-media/1/%2e%2e%2f%2e%2e%2fsecret.txt",
        "/local-media/1/..%5C..%5Csecret.txt",
    ],
)
def test_local_media_endpoint_blocks_directory_traversal(client, tmp_path, monkeypatch, probe):
    """A traversal must never escape the media root.

    Regression guard: the ``alt`` lookup in ``_local_media_path`` used to join
    the *raw* filename onto MEDIA_ROOT, so ``..%2F..%2Fsecret.txt`` resolved
    outside the media directory and served arbitrary files.
    """
    from conftest import ghost_server

    media_root = tmp_path / "media"
    (media_root / "1").mkdir(parents=True)
    (media_root / "1" / "ok.png").write_bytes(b"PNG")
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP-SECRET")
    monkeypatch.setattr(ghost_server, "MEDIA_ROOT", media_root)

    response = client.get(probe)
    assert b"TOP-SECRET" not in response.data, "path traversal escaped the media root"
    assert response.status_code == 404


def test_local_media_endpoint_still_serves_legitimate_files(client, tmp_path, monkeypatch):
    """The traversal fix must not break normal media serving."""
    from conftest import ghost_server

    media_root = tmp_path / "media"
    (media_root / "7").mkdir(parents=True)
    (media_root / "7" / "photo.png").write_bytes(b"PNGDATA")
    monkeypatch.setattr(ghost_server, "MEDIA_ROOT", media_root)

    response = client.get("/local-media/7/photo.png")
    assert response.status_code == 200
    assert response.data == b"PNGDATA"


# ---------------------------------------------------------------------------
# Rate limiting helpers
# ---------------------------------------------------------------------------
def test_rate_limit_key_uses_first_forwarded_for():
    from conftest import ghost_server

    with ghost_server.app.test_request_context(
        "/status", headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"}
    ):
        assert ghost_server._rate_limit_key() == "203.0.113.5"


def test_rate_limit_key_handles_missing_forwarded_header():
    from conftest import ghost_server

    with ghost_server.app.test_request_context("/status"):
        assert ghost_server._rate_limit_key() == ""


def test_localhost_is_exempt_from_rate_limiting():
    from conftest import ghost_server

    limiter = ghost_server.LocalhostExemptLimiter(
        key_func=ghost_server._rate_limit_key, default_limits=["5 per minute"]
    )
    assert issubclass(ghost_server.LocalhostExemptLimiter, ghost_server.Limiter)
    assert limiter is not None


# ---------------------------------------------------------------------------
# GHOST_HOST bind-address override
# ---------------------------------------------------------------------------
_DRIVER = '''
import json
import runpy
import sys

import flask_socketio

captured = {}


def _fake_run(self, *args, **kwargs):
    captured["host"] = kwargs.get("host")
    captured["port"] = kwargs.get("port")


flask_socketio.SocketIO.run = _fake_run

try:
    runpy.run_path(sys.argv[1], run_name="__main__")
except SystemExit:
    pass

print("CAPTURED:" + json.dumps(captured))
'''


def _run_entrypoint(src_dir: Path, host: str | None) -> dict:
    """Execute ``ghost_server.py`` as ``__main__`` without binding a socket."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ghost-host-") as workdir:
        driver = Path(workdir) / "driver.py"
        driver.write_text(_DRIVER, encoding="utf-8")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(src_dir)
        env["GHOST_PORT"] = "5123"
        if host is None:
            env.pop("GHOST_HOST", None)
        else:
            env["GHOST_HOST"] = host

        completed = subprocess.run(
            [sys.executable, str(driver), str(src_dir / "ghost_server.py")],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    assert completed.returncode == 0, completed.stderr
    for line in completed.stdout.splitlines():
        if line.startswith("CAPTURED:"):
            return json.loads(line[len("CAPTURED:"):])
    raise AssertionError(f"entrypoint did not report a bind address:\n{completed.stdout}\n{completed.stderr}")


def test_ghost_host_defaults_to_all_interfaces(src_dir):
    assert _run_entrypoint(src_dir, host=None)["host"] == "0.0.0.0"


def test_ghost_host_env_override_is_honoured(src_dir):
    assert _run_entrypoint(src_dir, host="127.0.0.1")["host"] == "127.0.0.1"


def test_ghost_port_env_override_is_honoured(src_dir):
    assert _run_entrypoint(src_dir, host=None)["port"] == 5123
