"""Offline provider backed by recorded JSON.

Its purpose is calibration. The anchor niches ("convert bank statement pdf to
excel", "resize image for instagram") have known-correct verdicts, so the
scoring pipeline can be tested for its ability to *reject* without spending a
penny on API calls or depending on network conditions.

`live = False`, so anything derived from it is labelled as fixture data rather
than real retrieval, and the CLI refuses to present it as a live finding.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import SearchResult
from .base import SearchProvider

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


class FixtureProvider(SearchProvider):
    name = "fixture"
    live = False

    def __init__(self, store=None, kind: str = "web", path: Path | None = None) -> None:
        super().__init__(store=None)  # never cache fixtures
        self.kind = kind
        self.path = path or FIXTURE_DIR
        self._data: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            self._data = {}
            for file in sorted(self.path.glob("*.json")):
                blob = json.loads(file.read_text(encoding="utf-8"))
                for key, value in blob.items():
                    self._data[key.strip().lower()] = value
        return self._data

    def available(self) -> bool:
        return self.path.exists()

    def why_unavailable(self) -> str:
        return f"no fixture directory at {self.path}"

    def _fetch(self, query: str, limit: int) -> tuple[list[SearchResult], str]:
        entry = self._load().get(query.strip().lower(), {})
        items = entry.get(self.kind, [])
        results = [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("snippet", ""),
                rank=i + 1,
                provider=self.name,
            )
            for i, item in enumerate(items[:limit])
        ]
        return results, f"fixture://{self.kind}/{query}"
