"""In-memory sqlite store of everything that was captured.

Yaml files are read once at session start and written at most once per test, so
during the run every lookup goes to sqlite only.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional

from .records import Record
from .hashing import stable_json

__all__ = ["CaptureStore"]

_SCHEMA = """
CREATE TABLE captures (
    ctx     TEXT    NOT NULL,
    hash    TEXT    NOT NULL,
    n       INTEGER NOT NULL,
    sql      TEXT    NOT NULL,
    params   TEXT    NOT NULL,
    rowcount INTEGER,
    columns  TEXT    NOT NULL,
    rows     BLOB    NOT NULL,
    PRIMARY KEY (ctx, hash, n)
)
"""


class CaptureStore:
    """Lookup table ``(context, query hash, sequence number) -> record``."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(_SCHEMA)
        # sqlite stays the single source of truth; this index only spares the
        # plugin a query plus a json decode for every replayed statement
        self._index: dict[tuple[str, str, int], Record] = {}

    # -- loading ---------------------------------------------------------
    def add_record(self, ctx: str, record: Record) -> None:
        self._index[(ctx, record.hash, record.n)] = record
        self._conn.execute(
            "INSERT OR REPLACE INTO captures"
            " (ctx, hash, n, sql, params, rowcount, columns, rows)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ctx,
                record.hash,
                record.n,
                record.sql,
                stable_json(record.params),
                record.rowcount,
                stable_json(record.columns),
                json.dumps(record.rows, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def add_records(self, ctx: str, records: Iterable[Record]) -> None:
        for record in records:
            self.add_record(ctx, record)

    def replace_context(self, ctx: str, records: Iterable[Record]) -> None:
        """Drop everything known about a context and store the new records."""
        self.forget_context(ctx)
        self.add_records(ctx, records)

    def forget_context(self, ctx: str) -> None:
        self._conn.execute("DELETE FROM captures WHERE ctx = ?", (ctx,))
        self._conn.commit()
        for key in [key for key in self._index if key[0] == ctx]:
            del self._index[key]

    def known_context(self, ctx: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM captures WHERE ctx = ? LIMIT 1", (ctx,)).fetchone()
        return row is not None

    # -- lookup ----------------------------------------------------------
    def lookup(self, ctx: str, query_hash: str, n: int) -> Optional[Record]:
        record = self._index.get((ctx, query_hash, n))
        if record is not None:
            return record
        row = self._conn.execute(
            "SELECT hash, n, sql, params, rowcount, columns, rows"
            " FROM captures WHERE ctx = ? AND hash = ? AND n = ?",
            (ctx, query_hash, n),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def records(self, ctx: str) -> list[Record]:
        rows = self._conn.execute(
            "SELECT hash, n, sql, params, rowcount, columns, rows"
            " FROM captures WHERE ctx = ? ORDER BY hash, n",
            (ctx,),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def contexts(self) -> list[str]:
        rows = self._conn.execute("SELECT DISTINCT ctx FROM captures ORDER BY ctx").fetchall()
        return [row[0] for row in rows]

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_record(row: Any) -> Record:
        return Record(
            hash=row[0],
            n=row[1],
            sql=row[2],
            params=json.loads(row[3]),
            rowcount=None if row[4] is None else int(row[4]),
            columns=json.loads(row[5]),
            rows=json.loads(row[6]),
        )
