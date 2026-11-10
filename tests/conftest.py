"""Pytest bootstrap for the ghost-data-suite test suite.

The application modules resolve their filesystem locations from environment
variables *at import time* (``src/scraper.py`` even creates its log directory
as a side effect of being imported).  To keep the suite hermetic this module
therefore, in this exact order:

1. creates a throwaway sandbox directory outside the repository,
2. points every path/secret environment variable at that sandbox,
3. puts ``src/`` on ``sys.path`` and imports the modules once, and
4. blocks every outbound network request for the whole session.

Nothing in the suite may touch the real database, keys, logs or media of the
repository, and nothing may reach the network.
"""

from __future__ import annotations

import atexit
import importlib
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

SANDBOX = Path(tempfile.mkdtemp(prefix="ghost-data-suite-tests-"))
atexit.register(shutil.rmtree, SANDBOX, True)


def _sandbox_path(*parts: str) -> str:
    """Return a path inside the sandbox, creating the parent directories."""
    path = SANDBOX.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


# ---------------------------------------------------------------------------
# 1. Environment first -- these are read while the modules are imported.
# ---------------------------------------------------------------------------
os.environ["DB_PATH"] = _sandbox_path("data", "alchemy.db")
os.environ["ENCRYPTION_KEY_FILE"] = _sandbox_path("keys", "master.key")
os.environ["SECRET_FILE"] = _sandbox_path("keys", "secret.key")
os.environ["CONFIG_FILE"] = _sandbox_path("config", "config.yaml")
os.environ["LOG_PATH"] = _sandbox_path("logs", "alchemist.log")
os.environ["MEDIA_DIR"] = _sandbox_path("media")
os.environ["MEDIA_ROOT"] = _sandbox_path("media")
os.environ["GHOST_DATA_DIR"] = _sandbox_path("data")
os.environ["GHOST_AUTH_TOKEN"] = ""
os.environ["LOG_LEVEL"] = "WARNING"

# ---------------------------------------------------------------------------
# 2. Make the application modules importable and import them exactly once.
# ---------------------------------------------------------------------------
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

scraper = importlib.import_module("scraper")
ghost_server = importlib.import_module("ghost_server")


# ---------------------------------------------------------------------------
# 3. Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def repo_root() -> Path:
    """Absolute path of the repository under test."""
    return REPO_ROOT


@pytest.fixture
def src_dir() -> Path:
    """Absolute path of the ``src`` package directory."""
    return SRC_DIR


@pytest.fixture
def sandbox() -> Path:
    """Absolute path of the throwaway directory used by the whole session."""
    return SANDBOX


@pytest.fixture(params=["scraper", "ghost_server"])
def guard_module(request):
    """Both SSRF guards under test, parameterised over the implementing module."""
    return importlib.import_module(request.param)


@pytest.fixture
def client():
    """Flask test client for the ghost server (no sockets are opened)."""
    app = ghost_server.app
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test tries to open a network connection.

    The yielded list records every attempted call, so a test can assert that
    a blocked URL never reached ``urllib.request.urlopen``.
    """
    attempts: list[tuple] = []

    def _blocked_urlopen(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError(f"tests must not perform network requests: {args!r}")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked_urlopen)
    yield attempts
