"""The in-memory sqlite store."""

from __future__ import annotations

import pytest

from capquery.records import Record
from capquery.store import CaptureStore


def record(query_hash: str, n: int, rows: list, rowcount: int | None = None) -> Record:
    return Record(
        hash=query_hash,
        n=n,
        sql=f"SELECT {query_hash}",
        params=[],
        rowcount=rowcount,
        columns=["id"],
        rows=rows,
    )


@pytest.fixture
def store():
    store = CaptureStore()
    yield store
    store.close()


def test_rowcount_survives_the_round_trip(store):
    store.add_record("ctx", record("hash", 0, [[{"t": "int", "v": 1}]], rowcount=1))
    stored = store.lookup("ctx", "hash", 0)
    assert stored.rowcount == 1
    assert stored.effective_rowcount() == 1


def test_a_missing_row_count_falls_back_to_the_stored_rows():
    assert record("hash", 0, []).rowcount is None
    assert record("hash", 0, []).effective_rowcount() == 0
    assert record("hash", 0, [[{"t": "int", "v": 1}]]).effective_rowcount() == 1


def test_store_is_empty_at_the_start(store):
    assert not store.known_context("ctx")
    assert store.lookup("ctx", "hash", 0) is None


def test_records_are_looked_up_by_hash_and_sequence_number(store):
    store.add_record("ctx", record("hash", 0, [[{"t": "int", "v": 1}]]))
    store.add_record("ctx", record("hash", 1, [[{"t": "int", "v": 2}]]))
    assert store.lookup("ctx", "hash", 0).rows == [[{"t": "int", "v": 1}]]
    assert store.lookup("ctx", "hash", 1).rows == [[{"t": "int", "v": 2}]]
    assert store.lookup("ctx", "hash", 2) is None


def test_contexts_are_isolated(store):
    store.add_record("a", record("hash", 0, [[{"t": "int", "v": 1}]]))
    assert not store.known_context("b")
    assert store.lookup("b", "hash", 0) is None


def test_records_round_trip_through_sqlite(store):
    original = Record(
        hash="abc",
        n=3,
        sql="SELECT id FROM t WHERE id = %s",
        params=[{"t": "int", "v": 7}],
        columns=["id", "name"],
        rows=[[{"t": "int", "v": 1}, {"t": "str", "v": "юникод"}]],
    )
    store.add_record("ctx", original)
    loaded = store.lookup("ctx", "abc", 3)
    assert loaded == original


def test_replace_context_drops_the_previous_records(store):
    store.add_record("ctx", record("old", 0, []))
    store.replace_context("ctx", [record("new", 0, [])])
    assert store.lookup("ctx", "old", 0) is None
    assert store.lookup("ctx", "new", 0) is not None


def test_forget_context(store):
    store.add_record("ctx", record("hash", 0, []))
    store.forget_context("ctx")
    assert not store.known_context("ctx")
    assert store.records("ctx") == []


def test_records_of_a_context_are_ordered(store):
    store.add_record("ctx", record("b", 0, []))
    store.add_record("ctx", record("a", 1, []))
    store.add_record("ctx", record("a", 0, []))
    assert [(item.hash, item.n) for item in store.records("ctx")] == [("a", 0), ("a", 1), ("b", 0)]


def test_contexts_are_listed(store):
    store.add_record("b", record("hash", 0, []))
    store.add_record("a", record("hash", 0, []))
    assert store.contexts() == ["a", "b"]
