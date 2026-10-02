"""The capture/replay decision: the position of a statement is the key, its hash the check.

The interceptor is driven here without Django: ``_execute`` reads the real cursor
methods out of ``interceptor._original``, so a fake stands in for postgres and every
statement that reaches it is recorded by the fixture.
"""

from __future__ import annotations

import pytest

from capquery import interceptor
from capquery.hashing import query_hash
from capquery.interceptor import RECORD, REPLAY, CaptureContext
from capquery.records import Record
from capquery.store import CaptureStore


class _Database:
    """``connection``: only the error wrapping of the real cursor is used."""

    @staticmethod
    def wrap_database_errors(func):
        return func


class _Cursor:
    def __init__(self, columns=(), rows=(), rowcount=None):
        self.description = [(name, None, None, None, None, None, None) for name in columns] or None
        self.rows = [tuple(row) for row in rows]
        self.rowcount = len(self.rows) if rowcount is None else rowcount
        self._query = None

    def fetchall(self):
        return list(self.rows)


class _Wrapper:
    """The parts of Django's ``CursorWrapper`` the interceptor touches."""

    def __init__(self):
        self.cursor = _Cursor()
        self.db = _Database()


@pytest.fixture
def executed(monkeypatch):
    """Record every statement that reached postgres; answer it with one row."""
    statements = []

    def fake_execute(wrapper, sql, params=None):
        statements.append((sql, params))
        wrapper.cursor = _Cursor(columns=["id"], rows=[(1,)])
        return None

    monkeypatch.setitem(interceptor._original, "execute", fake_execute)
    return statements


def _ctx(mode: str, records=(), key: str = "ctx") -> CaptureContext:
    store = CaptureStore()
    store.add_records(key, records)
    return CaptureContext(key=key, mode=mode, store=store)


def _record(sql: str, n: int, *, rows=(), columns=("id",), params=()) -> Record:
    return Record(
        hash=query_hash(sql),
        n=n,
        sql=sql,
        params=list(params),
        rowcount=len(rows),
        columns=list(columns),
        rows=[[{"t": "int", "v": value} for value in row] for row in rows],
    )


# -- recording -----------------------------------------------------------------
def test_a_statement_is_recorded_under_its_position(executed):
    with interceptor.activate(_ctx(RECORD)) as ctx:
        interceptor._execute(_Wrapper(), "SELECT id FROM t")

    assert [record.n for record in ctx.recorded] == [1]
    assert ctx.recorded[0].hash == query_hash("SELECT id FROM t")
    assert executed == [("SELECT id FROM t", None)]


def test_every_statement_of_a_context_gets_the_next_position(executed):
    """The position counts the statements of the context, not the repeats of a query."""
    with interceptor.activate(_ctx(RECORD)) as ctx:
        interceptor._execute(_Wrapper(), "SELECT id FROM t")
        interceptor._execute(_Wrapper(), "SELECT id FROM t WHERE id = %s", (1,))
        interceptor._execute(_Wrapper(), "SELECT id FROM t")

    assert [record.n for record in ctx.recorded] == [1, 2, 3]
    # the first and the last statement are the same query, recorded twice
    assert ctx.recorded[0].hash == ctx.recorded[2].hash
    assert ctx.recorded[0].hash != ctx.recorded[1].hash


def test_the_parameters_are_stored_but_not_hashed(executed):
    with interceptor.activate(_ctx(RECORD)) as ctx:
        interceptor._execute(_Wrapper(), "SELECT id FROM t WHERE id = %s", (7,))

    assert ctx.recorded[0].params == [{"t": "int", "v": 7}]
    assert ctx.recorded[0].hash == query_hash("SELECT id FROM t WHERE id = %s")


def test_a_statement_the_plugin_never_captures_takes_no_position(executed):
    with interceptor.activate(_ctx(RECORD)) as ctx:
        interceptor._execute(_Wrapper(), "CREATE TABLE t (id integer)")
        interceptor._execute(_Wrapper(), "SELECT id FROM t")

    assert [record.n for record in ctx.recorded] == [1]
    assert ctx.system_statements == 1
    assert len(executed) == 2


# -- replay --------------------------------------------------------------------
def test_the_record_of_a_position_is_served_without_touching_postgres(executed):
    wrapper = _Wrapper()
    with interceptor.activate(_ctx(REPLAY, [_record("SELECT id FROM t", 1, rows=[(7,)])])) as ctx:
        interceptor._execute(wrapper, "SELECT id FROM t")

    assert executed == []
    assert ctx.hits == 1
    assert wrapper._capquery_buffer == [(7,)]


def test_a_statement_with_other_parameters_is_still_served(executed):
    """The parameters are not part of the key: only the query text is checked.

    This is what makes a statement with a value that is new in every run
    (``timezone.now``, ``uuid4``, an ``IN (...)`` list built from a ``set``) replayable.
    """
    sql = "SELECT id FROM t WHERE id = %s"
    wrapper = _Wrapper()
    with interceptor.activate(_ctx(REPLAY, [_record(sql, 1, rows=[(7,)], params=[{"t": "int", "v": 1}])])) as ctx:
        interceptor._execute(wrapper, sql, (999,))

    assert executed == []
    assert ctx.hits == 1
    assert wrapper._capquery_buffer == [(7,)]


def test_another_query_at_a_captured_position_is_a_miss_and_is_reported(executed):
    with interceptor.activate(_ctx(REPLAY, [_record("SELECT id FROM t", 1)])) as ctx:
        interceptor._execute(_Wrapper(), "SELECT id FROM t WHERE id = 5")

    assert len(executed) == 1  # it went to postgres
    assert ctx.hits == 0
    assert ctx.misses == 1
    assert ctx.changed == 1
    assert ctx.changed_sql == ["position 1: SELECT id FROM t WHERE id = 5"]


def test_a_statement_beyond_the_capture_is_a_miss_but_not_a_changed_position(executed):
    """A test that gained a query: the capture simply does not hold that position."""
    with interceptor.activate(_ctx(REPLAY, [_record("SELECT id FROM t", 1)])) as ctx:
        interceptor._execute(_Wrapper(), "SELECT id FROM t")
        interceptor._execute(_Wrapper(), "SELECT count(*) FROM t")

    assert ctx.hits == 1
    assert ctx.misses == 1
    assert ctx.changed == 0


def test_a_statement_that_a_later_position_holds_is_not_served(executed):
    """The check is positional: a record of the same query elsewhere does not answer.

    A statement inserted in the middle of a test moves every position after it, so the
    plugin has to notice instead of serving the rows of the query it replaced.
    """
    records = [_record("SELECT id FROM t", 1), _record("SELECT count(*) FROM t", 2)]
    with interceptor.activate(_ctx(REPLAY, records)) as ctx:
        interceptor._execute(_Wrapper(), "SELECT count(*) FROM t")

    assert len(executed) == 1
    assert ctx.changed == 1
    assert ctx.hits == 0
