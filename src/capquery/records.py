"""A single captured query and its result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .values import decode_row

__all__ = ["Record"]


@dataclass
class Record:
    """One execution of one statement inside one capture context.

    ``n`` is the sequence number of this execution among the executions of the
    very same ``(sql, params)`` pair in the context.  It is what makes an
    ordered replay possible when a statement returns different rows over the
    lifetime of a test (for example a SELECT repeated after an INSERT).

    ``rowcount`` is what ``cursor.rowcount`` reported: the number of rows of a
    SELECT, and the number of affected rows of an INSERT/UPDATE/DELETE, which is
    what Django returns from ``QuerySet.update()`` and ``QuerySet.delete()``.
    """

    hash: str
    n: int
    sql: str
    params: list = field(default_factory=list)
    rowcount: Optional[int] = None
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)

    def decoded_rows(self) -> list:
        """Rows as psycopg would have returned them."""
        return [decode_row(row) for row in self.rows]

    def effective_rowcount(self) -> int:
        """Row count to report, falling back to the number of stored rows."""
        if self.rowcount is None:
            return len(self.rows)
        return self.rowcount

    def typed_params(self) -> list:
        return list(self.params)


def record_from_db(
    *,
    sql: str,
    encoded_params: Any,
    query_hash: str,
    n: int,
    columns: Any,
    rows: Any,
    rowcount: Optional[int] = None,
) -> Optional[Record]:
    """Build a record from raw database results, or None if they are untypable."""
    from .values import UnsupportedValue, encode_row

    try:
        typed_rows = [encode_row(row) for row in rows]
    except UnsupportedValue:
        return None
    return Record(
        hash=query_hash,
        n=n,
        sql=sql,
        params=encoded_params,
        rowcount=rowcount,
        columns=[str(column) for column in columns],
        rows=typed_rows,
    )
