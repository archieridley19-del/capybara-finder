"""SQLite cache and result persistence.

One file holds both:

- `cache`  : raw provider responses keyed by provider+query, with a TTL. These
             runs are expensive and repeat often.
- `runs`   : completed candidate results, so a batch can be stopped and resumed
             and already-researched candidates are skipped on re-run.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB = Path("capyfind.db")
DEFAULT_TTL = 60 * 60 * 24 * 14  # 14 days

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    query      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_query ON cache(query);

CREATE TABLE IF NOT EXISTS runs (
    candidate   TEXT NOT NULL,
    depth       TEXT NOT NULL,
    seed        TEXT,
    payload     TEXT NOT NULL,
    composite   INTEGER,
    band        TEXT,
    verified    INTEGER,
    created_at  REAL NOT NULL,
    PRIMARY KEY (candidate, depth)
);
CREATE INDEX IF NOT EXISTS idx_runs_seed ON runs(seed);
"""


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB, ttl: int = DEFAULT_TTL) -> None:
        self.path = Path(path)
        self.ttl = ttl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- cache -----------------------------------------------------------

    @staticmethod
    def _key(provider: str, query: str, variant: str = "") -> str:
        return f"{provider}::{variant}::{query.strip().lower()}"

    def cache_get(
        self, provider: str, query: str, variant: str = ""
    ) -> Any | None:
        key = self._key(provider, query, variant)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, created_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        if time.time() - row["created_at"] > self.ttl:
            return None
        return json.loads(row["payload"])

    def cache_put(
        self, provider: str, query: str, payload: Any, variant: str = ""
    ) -> None:
        key = self._key(provider, query, variant)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache "
                "(key, provider, query, payload, created_at) VALUES (?,?,?,?,?)",
                (key, provider, query, json.dumps(payload), time.time()),
            )

    # --- runs ------------------------------------------------------------

    def save_run(self, run: Any, seed: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(candidate, depth, seed, payload, composite, band, verified, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    run.candidate,
                    run.depth,
                    seed,
                    run.to_json(indent=None),
                    run.score.composite,
                    run.band,
                    int(run.verified),
                    time.time(),
                ),
            )

    def get_run(self, candidate: str, depth: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM runs WHERE candidate = ? AND depth = ?",
                (candidate, depth),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def has_run(self, candidate: str, depth: str) -> bool:
        return self.get_run(candidate, depth) is not None

    def list_runs(
        self, seed: str | None = None, survivors_only: bool = False
    ) -> list[dict[str, Any]]:
        sql = "SELECT payload, composite, verified FROM runs"
        args: list[Any] = []
        clauses = []
        if seed:
            clauses.append("seed = ?")
            args.append(seed)
        if survivors_only:
            clauses.append("verified = 1 AND composite > 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # Unverified sorts to the bottom, never the top.
        sql += " ORDER BY verified DESC, composite DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def clear_runs(self, seed: str | None = None) -> int:
        with self._connect() as conn:
            if seed:
                cur = conn.execute("DELETE FROM runs WHERE seed = ?", (seed,))
            else:
                cur = conn.execute("DELETE FROM runs")
            return cur.rowcount
