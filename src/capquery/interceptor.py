"""Cursor level interception.

``connection.execute_wrapper`` cannot be used to answer a query from a capture:
the value returned by a wrapper is discarded and the caller always reads rows
from the real cursor (see ``CursorWrapper._execute_with_wrappers``).  The plugin
therefore replaces the methods of ``django.db.backends.utils.CursorWrapper``,
which covers both the plain wrapper and ``CursorDebugWrapper`` (a subclass that
does not override any of the fetching methods).

Every data statement is captured and replayed — reads and writes alike.  A
replayed ``INSERT`` answers from its record: its rows (``RETURNING``) and its row
count, which is what Django returns from ``QuerySet.update()`` and
``QuerySet.delete()``.  Schema DDL, transaction control and unrecognised
statements are always executed against postgres (see ``statements.py``).
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from . import migration_time
from .hashing import hash_for
from .records import Record, record_from_db
from .statements import is_data_statement
from .values import UnsupportedValue, encode_params

__all__ = [
    "PASSTHROUGH",
    "RECORD",
    "REPLAY",
    "CaptureContext",
    "activate",
    "current",
    "install",
    "is_installed",
    "uninstall",
]

RECORD = "record"
REPLAY = "replay"
PASSTHROUGH = "passthrough"

_original: dict[str, Any] = {}
_active: contextvars.ContextVar[Optional["CaptureContext"]] = contextvars.ContextVar(
    "capquery_active_context", default=None
)


@dataclass
class CaptureContext:
    """What is being captured right now: one test, or the migration phase."""

    key: str
    mode: str
    store: Any
    suspended: bool = False
    hits: int = 0
    misses: int = 0
    db_statements: int = 0
    system_statements: int = 0
    replayed_statements: int = 0
    recorded: list = field(default_factory=list)
    unsupported: Optional[str] = None
    last_sql: Optional[str] = None
    missed_sql: list = field(default_factory=list)
    _next: dict = field(default_factory=dict)

    def next_n(self, query_hash: str) -> int:
        n = self._next.get(query_hash, 0)
        self._next[query_hash] = n + 1
        return n

    def note_unsupported(self, message: str) -> None:
        if self.unsupported is None:
            self.unsupported = message

    def mark_replayed(self, sql: str) -> None:
        self.hits += 1
        self.replayed_statements += 1
        self.last_sql = sql

    @property
    def capture_mode(self) -> bool:
        return self.mode in (RECORD, REPLAY)


@contextlib.contextmanager
def suspend() -> Iterator[None]:
    """Run housekeeping SQL without recording or replaying it."""
    ctx = _active.get()
    if ctx is None:
        yield
        return
    previous = ctx.suspended
    ctx.suspended = True
    try:
        yield
    finally:
        ctx.suspended = previous


@contextlib.contextmanager
def activate(ctx: CaptureContext) -> Iterator[CaptureContext]:
    token = _active.set(ctx)
    try:
        yield ctx
    finally:
        _active.reset(token)


def current() -> Optional[CaptureContext]:
    return _active.get()


def is_installed() -> bool:
    return bool(_original)


def install() -> None:
    """Patch the cursor wrapper once per process."""
    from django.db.backends import utils as django_utils

    if is_installed():
        return
    cls = django_utils.CursorWrapper
    _original["execute"] = cls.execute
    _original["executemany"] = cls.executemany
    _original["getattr"] = cls.__getattr__
    cls.execute = _execute
    cls.executemany = _executemany
    cls.fetchone = _fetchone
    cls.fetchmany = _fetchmany
    cls.fetchall = _fetchall
    cls.__iter__ = _iter
    cls.description = property(_description)
    cls.rowcount = property(_rowcount)


def uninstall() -> None:
    """Restore Django's own methods (used by the plugin's own test suite)."""
    from django.db.backends import utils as django_utils

    if not is_installed():
        return
    cls = django_utils.CursorWrapper
    cls.execute = _original["execute"]
    cls.executemany = _original["executemany"]
    cls.__getattr__ = _original["getattr"]
    for name in ("fetchone", "fetchmany", "fetchall", "__iter__", "description", "rowcount"):
        try:
            delattr(cls, name)
        except AttributeError:
            pass
    _original.clear()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _delegate(wrapper, name: str, *args: Any):
    """Fetch through Django's error wrapping, exactly like ``__getattr__`` does."""
    return wrapper.db.wrap_database_errors(getattr(wrapper.cursor, name))(*args)


def _reset_buffer(wrapper) -> None:
    wrapper._capquery_buffer = None
    wrapper._capquery_pos = 0
    wrapper._capquery_columns = []
    wrapper._capquery_rowcount = None


def _set_buffer(wrapper, rows: list, columns: list, rowcount: Optional[int] = None) -> None:
    wrapper._capquery_buffer = list(rows)
    wrapper._capquery_pos = 0
    wrapper._capquery_columns = list(columns)
    wrapper._capquery_rowcount = len(rows) if rowcount is None else rowcount
    _remember_sql(wrapper)


def _remember_sql(wrapper) -> None:
    """Keep ``cursor._query`` usable so debug logging does not lose the SQL."""
    sql = None
    ctx = _active.get()
    if ctx is not None:
        sql = ctx.last_sql
    if not sql:
        return
    try:
        cursor = wrapper.cursor
        if getattr(cursor, "_query", "missing") == "missing":
            return
        cursor._query = _QueryStub(sql)
    except Exception:  # pragma: no cover - driver internals, best effort only
        pass


class _QueryStub:
    """Minimal stand-in for the driver's internal last-query object."""

    def __init__(self, sql: str) -> None:
        self.query = sql.encode("utf-8")
        self.params = None


def _buffer(wrapper) -> Optional[list]:
    return getattr(wrapper, "_capquery_buffer", None)


def _take(wrapper, size: Optional[int]) -> list:
    buffer = wrapper._capquery_buffer
    position = wrapper._capquery_pos
    if size is None:
        chunk = buffer[position:]
        wrapper._capquery_pos = len(buffer)
    else:
        chunk = buffer[position : position + size]
        wrapper._capquery_pos = position + len(chunk)
    return chunk


# --------------------------------------------------------------------------- #
# patched methods
# --------------------------------------------------------------------------- #
def _trace(*parts: Any) -> None:
    path = os.environ.get("CAPQUERY_TRACE")
    if not path:  # pragma: no cover - debugging aid
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(" | ".join(str(part) for part in parts) + "\n")


def _record_result(wrapper, ctx, *, sql, encoded_params, query_hash, n):
    """Store what the database answered, so a later run can answer instead."""
    try:
        description = wrapper.cursor.description
        columns = [column[0] for column in description] if description else []
        rows = _delegate(wrapper, "fetchall") if columns else []
        rowcount = wrapper.cursor.rowcount
    except Exception:
        _reset_buffer(wrapper)
        raise
    # a capture must not hold the time of the run: the caller sees the normalized rows
    # too, so a recording run and a replay of it answer with the very same values
    rows = migration_time.normalize_rows(sql, columns, rows)
    ctx.last_sql = sql
    _set_buffer(wrapper, rows, columns, rowcount)
    _trace("REC", ctx.mode, ctx.key, f"{query_hash[:8]}#{n}", sql.replace("\n", " ")[:90])
    record = record_from_db(
        sql=sql,
        encoded_params=encoded_params,
        query_hash=query_hash,
        n=n,
        columns=columns,
        rows=rows,
        rowcount=rowcount,
    )
    if record is None:
        ctx.note_unsupported(f"result of {sql.strip()[:80]!r} holds a value capquery cannot store")
    else:
        ctx.recorded.append(record)


def _serve_from_capture(wrapper, ctx, record, *, sql, query_hash, n, kind):
    """Answer an execute() from the captures without touching postgres."""
    _reset_buffer(wrapper)
    ctx.mark_replayed(sql)
    _set_buffer(
        wrapper,
        record.decoded_rows(),
        list(record.columns),
        record.effective_rowcount(),
    )
    _trace(kind, ctx.mode, ctx.key, f"{query_hash[:8]}#{n}", sql.replace("\n", " ")[:90])


def _note_miss(ctx, sql: str, query_hash: str, n: int, kind: str) -> None:
    ctx.misses += 1
    ctx.db_statements += 1
    if len(ctx.missed_sql) < 5:
        ctx.missed_sql.append(" ".join(sql.split())[:160])
    _trace(kind, ctx.mode, ctx.key, f"{query_hash[:8]}#{n}", sql.replace("\n", " ")[:90])


def _execute(wrapper, sql, params=None):
    ctx = _active.get()
    if ctx is None or ctx.suspended or not ctx.capture_mode or not isinstance(sql, str):
        _reset_buffer(wrapper)
        return _original["execute"](wrapper, sql, params)
    if not is_data_statement(sql):
        # schema DDL, transaction control, maintenance: always for real
        _reset_buffer(wrapper)
        ctx.system_statements += 1
        _trace("SYSTEM", ctx.mode, ctx.key, " ".join(sql.split())[:90])
        return _original["execute"](wrapper, sql, params)
    try:
        encoded_params = encode_params(params)
        query_hash = hash_for(sql, encoded_params)
    except UnsupportedValue as exc:
        ctx.note_unsupported(str(exc))
        _reset_buffer(wrapper)
        ctx.db_statements += 1
        _trace("UNSUPPORTED", ctx.mode, ctx.key, " ".join(sql.split())[:90], str(exc)[:90])
        return _original["execute"](wrapper, sql, params)

    n = ctx.next_n(query_hash)
    if ctx.mode == REPLAY:
        record = ctx.store.lookup(ctx.key, query_hash, n)
        if record is not None:
            _serve_from_capture(wrapper, ctx, record, sql=sql, query_hash=query_hash, n=n, kind="REPLAY")
            return None
        _note_miss(ctx, sql, query_hash, n, "MISS")
        _reset_buffer(wrapper)
        return _original["execute"](wrapper, sql, params)

    result = _original["execute"](wrapper, sql, params)
    ctx.db_statements += 1
    _record_result(
        wrapper,
        ctx,
        sql=sql,
        encoded_params=encoded_params,
        query_hash=query_hash,
        n=n,
    )
    return result


def _executemany(wrapper, sql, param_list):
    ctx = _active.get()
    if (
        ctx is None
        or ctx.suspended
        or not ctx.capture_mode
        or not isinstance(sql, str)
        or not isinstance(param_list, (list, tuple))
    ):
        _reset_buffer(wrapper)
        return _original["executemany"](wrapper, sql, param_list)
    if not is_data_statement(sql):
        _reset_buffer(wrapper)
        ctx.system_statements += 1
        _trace("SYSTEM", ctx.mode, ctx.key, " ".join(sql.split())[:90])
        return _original["executemany"](wrapper, sql, param_list)
    try:
        encoded_params = [encode_params(params) for params in param_list]
        query_hash = hash_for(sql, encoded_params)
    except UnsupportedValue as exc:
        ctx.note_unsupported(str(exc))
        _reset_buffer(wrapper)
        ctx.db_statements += 1
        return _original["executemany"](wrapper, sql, param_list)

    n = ctx.next_n(query_hash)
    if ctx.mode == REPLAY:
        record = ctx.store.lookup(ctx.key, query_hash, n)
        if record is not None:
            _serve_from_capture(
                wrapper, ctx, record, sql=sql, query_hash=query_hash, n=n, kind="REPLAY"
            )
            return None
        _note_miss(ctx, sql, query_hash, n, "MISS")
        _reset_buffer(wrapper)
        return _original["executemany"](wrapper, sql, param_list)

    result = _original["executemany"](wrapper, sql, param_list)
    ctx.db_statements += 1
    rowcount = wrapper.cursor.rowcount
    _reset_buffer(wrapper)
    ctx.last_sql = sql
    _trace("REC", ctx.mode, ctx.key, f"{query_hash[:8]}#{n}", sql.replace("\n", " ")[:90])
    record = record_from_db(
        sql=sql,
        encoded_params=encoded_params,
        query_hash=query_hash,
        n=n,
        columns=[],
        rows=[],
        rowcount=rowcount,
    )
    if record is None:
        ctx.note_unsupported(f"result of {sql.strip()[:80]!r} holds a value capquery cannot store")
    else:
        ctx.recorded.append(record)
    return result


def _fetchone(wrapper):
    buffer = _buffer(wrapper)
    if buffer is None:
        return _delegate(wrapper, "fetchone")
    chunk = _take(wrapper, 1)
    return chunk[0] if chunk else None


def _fetchmany(wrapper, size=None):
    buffer = _buffer(wrapper)
    if buffer is None:
        return _delegate(wrapper, "fetchmany", size) if size is not None else _delegate(wrapper, "fetchmany")
    if size is None:
        return _take(wrapper, 1)
    return _take(wrapper, size)


def _fetchall(wrapper):
    buffer = _buffer(wrapper)
    if buffer is None:
        return _delegate(wrapper, "fetchall")
    return _take(wrapper, None)


def _iter(wrapper):
    buffer = _buffer(wrapper)
    if buffer is None:
        yield from wrapper.cursor
        return
    yield from _take(wrapper, None)


def _description(wrapper):
    buffer = _buffer(wrapper)
    if buffer is None:
        return wrapper.cursor.description
    return [
        (str(name), None, None, None, None, None, None) for name in wrapper._capquery_columns
    ]


def _rowcount(wrapper):
    buffer = _buffer(wrapper)
    if buffer is None:
        return wrapper.cursor.rowcount
    rowcount = getattr(wrapper, "_capquery_rowcount", None)
    return len(buffer) if rowcount is None else rowcount
