"""Fetch and flatten a competitor's homepage.

Used only at verify depth. Page text is what lets the classifier see pricing,
a copyright year and country markers that a SERP snippet omits -- the
difference between "looks like a competitor" and "is a polished competitor".
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from html import unescape

from .http import USER_AGENT

SCRIPT_STYLE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")


def fetch_text(url: str, timeout: float = 12.0, limit: int = 20000) -> str:
    """Best-effort page text. Returns "" on any failure -- callers treat an
    empty page as "no extra evidence", never as a positive."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            ctype = response.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return ""
            raw = response.read(limit * 4).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return ""

    body = SCRIPT_STYLE.sub(" ", raw)
    body = TAG.sub(" ", body)
    return WHITESPACE.sub(" ", unescape(body)).strip()[:limit]
