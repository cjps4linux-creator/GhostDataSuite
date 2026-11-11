"""Unit tests for the pure logic of ``src/scraper.py`` (Data Alchemist).

The suite covers the config loader, the header builder, the text truncation
helper, the media classifier/URL extractor and the encrypted store.  It never
starts a browser, never performs a network request and never writes to the
repository's database, keys, logs or media directories -- the sandbox paths
are installed by ``tests/conftest.py`` before ``scraper`` is imported.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import scraper
from security import ALLOWED_URL_SCHEMES


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def test_load_targets_returns_empty_list_when_config_is_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist.yaml"
    monkeypatch.setattr(scraper, "CONFIG_FILE", str(missing))
    assert scraper.load_targets() == []


def test_load_targets_returns_empty_list_for_empty_file(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setattr(scraper, "CONFIG_FILE", str(config))
    assert scraper.load_targets() == []


def test_load_targets_returns_empty_list_when_alchemist_section_is_missing(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("ghost_server:\n  port: 5000\n", encoding="utf-8")
    monkeypatch.setattr(scraper, "CONFIG_FILE", str(config))
    assert scraper.load_targets() == []


def test_load_targets_returns_empty_list_for_malformed_yaml(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("alchemist: [unclosed\n  : :\n", encoding="utf-8")
    monkeypatch.setattr(scraper, "CONFIG_FILE", str(config))
    assert scraper.load_targets() == []


def test_load_targets_returns_empty_list_for_empty_target_list(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("alchemist:\n  targets: []\n", encoding="utf-8")
    monkeypatch.setattr(scraper, "CONFIG_FILE", str(config))
    assert scraper.load_targets() == []


def test_load_targets_reads_configured_targets(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "alchemist:\n"
        "  targets:\n"
        '    - url: "https://a.example.com"\n'
        "      interval: 30\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scraper, "CONFIG_FILE", str(config))

    targets = scraper.load_targets()

    assert targets == [{"url": "https://a.example.com", "interval": 30}]


def test_load_targets_skips_targets_that_are_not_enabled(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "alchemist:\n"
        "  targets:\n"
        '    - url: "https://on.example.com"\n'
        "      enabled: true\n"
        '    - url: "https://off.example.com"\n'
        "      enabled: false\n"
        '    - url: "https://default.example.com"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(scraper, "CONFIG_FILE", str(config))

    urls = [t["url"] for t in scraper.load_targets()]

    assert "https://off.example.com" not in urls
    assert "https://on.example.com" in urls
    assert "https://default.example.com" in urls


@pytest.mark.parametrize(
    ("enabled", "expected_kept"),
    [
        (True, True),
        (False, False),
        (None, False),
        (0, False),
        (1, True),
        ("false", True),
        ("", False),
    ],
)
def test_enabled_flag_is_treated_as_truthiness(tmp_path, monkeypatch, enabled, expected_kept):
    """Only a falsy ``enabled`` value drops the target."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "alchemist:\n"
        "  targets:\n"
        '    - url: "https://t.example.com"\n'
        f"      enabled: {json.dumps(enabled)}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scraper, "CONFIG_FILE", str(config))

    targets = scraper.load_targets()

    assert bool(targets) is expected_kept


def test_load_targets_ignores_targets_key_outside_alchemist(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "targets:\n"
        '  - url: "https://top.example.com"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(scraper, "CONFIG_FILE", str(config))
    assert scraper.load_targets() == []


# ---------------------------------------------------------------------------
# build_headers
# ---------------------------------------------------------------------------
def test_build_headers_contains_user_agent():
    headers = scraper.build_headers()
    assert isinstance(headers, dict)
    assert "User-Agent" in headers
    assert headers["User-Agent"]


@pytest.mark.parametrize(
    "header",
    ["User-Agent", "Accept-Language", "Accept-Encoding", "Connection"],
)
def test_build_headers_contains_expected_browser_headers(header):
    assert header in scraper.build_headers()


def test_build_headers_returns_a_fresh_dict_each_call():
    first = scraper.build_headers()
    second = scraper.build_headers()
    assert first == second
    assert first is not second


def test_build_headers_user_agent_looks_like_a_browser():
    user_agent = scraper.build_headers()["User-Agent"]
    assert "Mozilla/5.0" in user_agent
    assert "Chrome/" in user_agent


# ---------------------------------------------------------------------------
# safe_text
# ---------------------------------------------------------------------------
def test_safe_text_truncates_to_the_limit():
    assert scraper.safe_text("abcdef", limit=3) == "abc"


def test_safe_text_returns_text_shorter_than_the_limit_unchanged():
    assert scraper.safe_text("abc", limit=10) == "abc"


def test_safe_text_default_limit_is_2000():
    assert len(scraper.safe_text("x" * 5000)) == 2000


@pytest.mark.parametrize("limit", [0, 1, 5, 100])
def test_safe_text_never_returns_more_than_the_limit(limit):
    assert len(scraper.safe_text("x" * 500, limit=limit)) == limit


@pytest.mark.parametrize("value", ["", None, 0, [], {}, False])
def test_safe_text_returns_empty_string_for_falsy_input(value):
    assert scraper.safe_text(value) == ""


def test_safe_text_preserves_leading_and_trailing_whitespace():
    assert scraper.safe_text("  hello  ") == "  hello  "


# ---------------------------------------------------------------------------
# _classify_media
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "content_type", "expected"),
    [
        ("https://cdn.example.com/a.jpg", "", "image"),
        ("https://cdn.example.com/a.jpeg", "", "image"),
        ("https://cdn.example.com/a.png", "", "image"),
        ("https://cdn.example.com/a.webp", "", "image"),
        ("https://cdn.example.com/a.mp4", "", "video"),
        ("https://cdn.example.com/a.webm", "", "video"),
        ("https://cdn.example.com/a.mp3", "", "audio"),
        ("https://cdn.example.com/a.flac", "", "audio"),
        ("https://cdn.example.com/a.pdf", "", "document"),
        ("https://cdn.example.com/a.csv", "", "document"),
        ("https://cdn.example.com/a.txt", "", "document"),
    ],
)
def test_classify_media_by_file_extension(url, content_type, expected):
    assert scraper._classify_media(url, content_type) == expected


@pytest.mark.parametrize(
    ("url", "content_type", "expected"),
    [
        ("https://cdn.example.com/a.bin", "image/png", "image"),
        ("https://cdn.example.com/a.bin", "image/webp", "image"),
        ("https://cdn.example.com/a.bin", "video/mp4", "video"),
        ("https://cdn.example.com/a.bin", "audio/mpeg", "audio"),
        ("https://cdn.example.com/a.bin", "application/pdf", "document"),
        ("https://cdn.example.com/a.bin", "application/octet-stream", "document"),
    ],
)
def test_classify_media_by_content_type(url, content_type, expected):
    assert scraper._classify_media(url, content_type) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/tiles/map/1/2/3.png",
        "https://cdn.example.com/basemap/example_value.png",
        "https://cdn.example.com/openstreetmap/tile.png",
        "https://cdn.example.com/mapbox/tile.png",
    ],
)
def test_classify_media_detects_map_tiles(url):
    assert scraper._classify_media(url, "") == "map"


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/thumbnail",
        "https://cdn.example.com/image/photo",
        "https://cdn.example.com/avatar",
    ],
)
def test_classify_media_detects_images_by_path_words(url):
    assert scraper._classify_media(url, "") == "image"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://cdn.example.com/video/clip", "video"),
        ("https://cdn.example.com/sound/music", "audio"),
        ("https://cdn.example.com/download/report", "document"),
    ],
)
def test_classify_media_detects_categories_by_path_words(url, expected):
    assert scraper._classify_media(url, "") == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/a.unknown-extension",
        "https://cdn.example.com/",
        "https://cdn.example.com",
        "not a url at all",
    ],
)
def test_classify_media_returns_none_for_unclassifiable_urls(url):
    assert scraper._classify_media(url, "") is None


def test_classify_media_map_takes_priority_over_the_extension():
    assert scraper._classify_media("https://cdn.example.com/map/tile.png", "image/png") == "map"


# ---------------------------------------------------------------------------
# _extract_media_urls
# ---------------------------------------------------------------------------
def test_extract_media_urls_finds_absolute_media_urls():
    html = (
        '<img src="https://cdn.example.org/a/b.png">'
        '<img src="https://cdn.example.org/media/cat.jpeg">'
    )
    urls = scraper._extract_media_urls(html, "https://base.example.com")
    assert "https://cdn.example.org/a/b.png" in urls
    assert "https://cdn.example.org/media/cat.jpeg" in urls


def test_extract_media_urls_resolves_relative_urls_against_the_base():
    html = '<img src="/media/a.jpg">'
    assert scraper._extract_media_urls(html, "https://base.example.com") == [
        "https://base.example.com/media/a.jpg"
    ]


def test_extract_media_urls_deduplicates_repeated_urls():
    html = '<img src="https://cdn.example.org/a/b.png"><img src="https://cdn.example.org/a/b.png">'
    urls = scraper._extract_media_urls(html, "https://base.example.com")
    assert urls.count("https://cdn.example.org/a/b.png") == 1


def test_extract_media_urls_ignores_non_media_extensions():
    html = '<a href="https://example.com/page.html">x</a>'
    assert scraper._extract_media_urls(html, "https://base.example.com") == []


def test_extract_media_urls_returns_empty_list_for_empty_html():
    assert scraper._extract_media_urls("", "https://base.example.com") == []


def test_extract_media_urls_ignores_urls_without_a_file_extension():
    html = '<a href="https://example.com/section/page">x</a>'
    assert scraper._extract_media_urls(html, "https://base.example.com") == []


def test_extract_media_urls_does_not_mutate_its_input():
    html = '<img src="https://cdn.example.org/a/b.png">'
    scraper._extract_media_urls(html, "https://base.example.com")
    assert html == '<img src="https://cdn.example.org/a/b.png">'


# ---------------------------------------------------------------------------
# EncryptedStore
# ---------------------------------------------------------------------------
def test_encrypted_store_creates_the_schema(tmp_path):
    store = scraper.EncryptedStore(str(tmp_path / "alchemy.db"), str(tmp_path / "master.key"))
    try:
        tables = {
            row[0]
            for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"captures", "meta"} <= tables
    finally:
        store.close()


def test_encrypted_store_generates_a_master_key(tmp_path):
    key_file = tmp_path / "keys" / "master.key"
    store = scraper.EncryptedStore(str(tmp_path / "alchemy.db"), str(key_file))
    try:
        assert key_file.exists()
        assert len(key_file.read_bytes()) == 44
    finally:
        store.close()


def test_encrypted_store_reuses_an_existing_master_key(tmp_path):
    key_file = tmp_path / "master.key"
    key_file.write_bytes(scraper.Fernet.generate_key())
    original = key_file.read_bytes()

    store = scraper.EncryptedStore(str(tmp_path / "alchemy.db"), str(key_file))
    try:
        assert key_file.read_bytes() == original
    finally:
        store.close()


def test_encrypted_store_encrypts_the_payload(tmp_path):
    store = scraper.EncryptedStore(str(tmp_path / "alchemy.db"), str(tmp_path / "master.key"))
    try:
        capture_id = store.store("https://example.com/x", "top secret payload", status="ok")
        row = store.conn.execute(
            "SELECT encrypted FROM captures WHERE id = ?", (capture_id,)
        ).fetchone()
        blob = row[0]
        assert b"top secret payload" not in bytes(blob)
        assert store.fernet.decrypt(blob).decode("utf-8") == "top secret payload"
    finally:
        store.close()


def test_encrypted_store_hashes_the_url(tmp_path):
    store = scraper.EncryptedStore(str(tmp_path / "alchemy.db"), str(tmp_path / "master.key"))
    try:
        url = "https://example.com/x"
        capture_id = store.store(url, "payload", status="ok")
        row = store.conn.execute(
            "SELECT url_hash FROM captures WHERE id = ?", (capture_id,)
        ).fetchone()
        assert row[0] == hashlib.sha256(url.encode("utf-8")).hexdigest()
    finally:
        store.close()


def test_encrypted_store_records_the_status(tmp_path):
    store = scraper.EncryptedStore(str(tmp_path / "alchemy.db"), str(tmp_path / "master.key"))
    try:
        capture_id = store.store("https://example.com/x", "payload", status="error")
        row = store.conn.execute(
            "SELECT status FROM captures WHERE id = ?", (capture_id,)
        ).fetchone()
        assert row[0] == "error"
    finally:
        store.close()


def test_encrypted_store_counts_captures(tmp_path):
    store = scraper.EncryptedStore(str(tmp_path / "alchemy.db"), str(tmp_path / "master.key"))
    try:
        assert store.capture_count() == 0
        store.store("https://example.com/1", "a")
        store.store("https://example.com/2", "b")
        assert store.capture_count() == 2
    finally:
        store.close()


def test_encrypted_store_sets_the_last_boot_metadata(tmp_path):
    store = scraper.EncryptedStore(str(tmp_path / "alchemy.db"), str(tmp_path / "master.key"))
    try:
        row = store.conn.execute("SELECT value FROM meta WHERE key = 'last_boot'").fetchone()
        assert row is not None
        assert row[0]
    finally:
        store.close()


def test_encrypted_store_can_decrypt_another_store_with_the_same_key(tmp_path):
    key_file = str(tmp_path / "master.key")
    first = scraper.EncryptedStore(str(tmp_path / "one.db"), key_file)
    capture_id = first.store("https://example.com/x", "shared secret", status="ok")
    blob = first.conn.execute(
        "SELECT encrypted FROM captures WHERE id = ?", (capture_id,)
    ).fetchone()[0]
    first.close()

    second = scraper.EncryptedStore(str(tmp_path / "two.db"), key_file)
    try:
        assert second.fernet.decrypt(blob).decode("utf-8") == "shared secret"
    finally:
        second.close()


# ---------------------------------------------------------------------------
# Media download
# ---------------------------------------------------------------------------
def test_download_media_skips_blocked_urls_and_writes_nothing(tmp_path):
    """A ``file://`` URL is refused; no file is created on disk."""
    base = tmp_path / "media"
    results = scraper._download_media(base, 1, ["file:///etc/passwd.png"])

    assert len(results) == 1
    assert results[0]["url"] == "file:///etc/passwd.png"
    assert results[0]["size_bytes"] == 0
    assert not (base / "1" / "passwd.png").exists()


def test_download_media_ignores_unclassifiable_urls(tmp_path):
    base = tmp_path / "media"
    results = scraper._download_media(base, 1, ["https://example.com/page.html"])
    assert results == []


def test_download_media_returns_metadata_for_each_url(tmp_path):
    base = tmp_path / "media"
    results = scraper._download_media(base, 2, ["https://cdn.example.org/a/b.png"])

    assert len(results) == 1
    entry = results[0]
    assert entry["category"] == "image"
    assert entry["filename"] == "b.png"
    assert entry["content_type"] == ""
    assert entry["size_bytes"] == 0
    assert entry["path"].endswith(str(Path("2") / "b.png"))


# ---------------------------------------------------------------------------
# Module level configuration
# ---------------------------------------------------------------------------
def test_module_paths_point_into_the_sandbox(sandbox):
    """The imported module must not reference the repository's own files."""
    assert Path(scraper.DB_PATH).is_relative_to(sandbox)
    assert Path(scraper.KEY_FILE).is_relative_to(sandbox)
    assert Path(scraper.CONFIG_FILE).is_relative_to(sandbox)
    assert Path(scraper.LOG_PATH).is_relative_to(sandbox)
    assert Path(scraper.MEDIA_DIR).is_relative_to(sandbox)


def test_importing_the_module_created_the_log_directory(sandbox):
    assert (sandbox / "logs").is_dir()


def test_allowed_url_schemes_constant():
    assert ALLOWED_URL_SCHEMES == ("http", "https")


def test_media_extension_map_covers_the_expected_categories():
    assert set(scraper.MEDIA_EXT_MAP) == {"image", "video", "audio", "document", "map"}
    assert ".jpg" in scraper.MEDIA_EXT_MAP["image"]
    assert ".mp4" in scraper.MEDIA_EXT_MAP["video"]
    assert ".mp3" in scraper.MEDIA_EXT_MAP["audio"]
    assert ".pdf" in scraper.MEDIA_EXT_MAP["document"]
