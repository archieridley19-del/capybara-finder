"""Minimal HTTP with pacing, backoff and a circuit breaker.

Stdlib only. Two behaviours matter for batch runs:

- Requests are paced by a per-host minimum interval, so a 200-candidate sweep
  does not hammer one API.
- `CircuitBreaker` trips after N consecutive failures so a batch stops rather
  than burning an entire candidate list against a rate limit.
"""

from __future__ import annotations

import gzip
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = "capyfind/0.1 (+https://github.com/archieridley19-del/capybara-finder)"


class RetrievalError(RuntimeError):
    """Raised when a provider call fails after retries."""


class CircuitOpen(RetrievalError):
    """Raised when too many consecutive failures have occurred."""


@dataclass
class CircuitBreaker:
    """Stop the batch after `threshold` consecutive failures."""

    threshold: int = 3
    consecutive: int = 0
    tripped: bool = False
    last_error: str = ""

    def record_success(self) -> None:
        self.consecutive = 0

    def record_failure(self, error: str) -> None:
        self.consecutive += 1
        self.last_error = error
        if self.consecutive >= self.threshold:
            self.tripped = True

    def check(self) -> None:
        if self.tripped:
            raise CircuitOpen(
                f"stopped after {self.consecutive} consecutive failures; "
                f"last error: {self.last_error}"
            )


@dataclass
class Pacer:
    """Per-host minimum interval between requests."""

    min_interval: float = 1.0
    _last: dict[str, float] = field(default_factory=dict)

    def wait(self, url: str) -> None:
        host = urllib.parse.urlparse(url).netloc
        now = time.monotonic()
        previous = self._last.get(host)
        if previous is not None:
            delta = now - previous
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
        self._last[host] = time.monotonic()


_DEFAULT_PACER = Pacer()


def _decode(raw: bytes, encoding: str) -> str:
    """Some APIs (Stack Exchange notably) always respond gzipped."""
    encoding = (encoding or "").lower()
    if encoding == "gzip":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    elif encoding == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 20.0,
    retries: int = 2,
    pacer: Pacer | None = None,
) -> dict[str, Any]:
    """Fetch JSON, retrying on transient errors with exponential backoff.

    Raises RetrievalError with a readable message on final failure. Callers are
    expected to record that message against the candidate rather than swallow
    it -- failures are never silent.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    body = json.dumps(payload).encode() if payload is not None else None
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    (pacer or _DEFAULT_PACER).wait(url)

    last_error = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, data=body, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = _decode(response.read(), response.headers.get("Content-Encoding"))
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = _decode(exc.read(), exc.headers.get("Content-Encoding"))[:200]
            last_error = f"HTTP {exc.code}: {detail}"
            # 4xx other than rate-limiting will not fix themselves.
            if exc.code not in (408, 425, 429) and exc.code < 500:
                break
        except urllib.error.URLError as exc:
            last_error = f"network error: {exc.reason}"
        except json.JSONDecodeError as exc:
            last_error = f"malformed JSON: {exc}"
            break
        except TimeoutError:
            last_error = f"timeout after {timeout}s"

        if attempt < retries:
            time.sleep(2**attempt)

    raise RetrievalError(last_error or "unknown retrieval failure")
