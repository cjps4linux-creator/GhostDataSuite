"""
Shared security helpers for Ghost-Data Suite.

This module centralises security-critical logic so it is implemented once
and reused everywhere, instead of being copy-pasted between modules.

Currently provides:

* ALLOWED_URL_SCHEMES
* is_allowed_url(url) -> bool
* safe_open(url, timeout, headers=None) -> file-like object
"""

from __future__ import annotations

import urllib.request
from urllib.parse import urlparse
from typing import Optional, Dict

# Only absolute http(s) URLs may be fetched. Everything else (file://,
# gopher://, data:, javascript:, relative references, schemeless URLs) is
# refused before the network is ever touched.
ALLOWED_URL_SCHEMES = ("http", "https")


def is_allowed_url(url: str) -> bool:
    """Return True only for absolute http(s) URLs with a host.

    The guard is a filter, not a parser: it must always return a bool and
    must never raise for any input.
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return False
    return bool(parsed.netloc)


def safe_open(url: str, timeout: int, headers: Optional[Dict[str, str]] = None):
    """Open a URL after validating the scheme, or raise ValueError.

    This is a drop-in replacement for urllib.request.urlopen that refuses
    blocked schemes before any I/O happens.
    """
    if not is_allowed_url(url):
        raise ValueError(f"blocked URL scheme or missing host: {url!r}")
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "GhostDataSuite/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)
