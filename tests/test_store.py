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
    store.add_record("ctx", record("hash", 1, [[{"t": "int", "v": 1}]], rowcount=1))
    stored = store.lookup("ctx", 1)
    assert stored.rowcount == 1
    assert stored.effective_rowcount() == 1


def test_a_missing_row_count_falls_back_to_the_stored_rows():
    assert record("hash", 1, []).rowcount is None
    assert record("hash", 1, []).effective_rowcount() == 0
    assert record("hash", 1, [[{"t": "int", "v": 1}]]).effective_rowcount() == 1


def test_store_is_empty_at_the_start(store):
    assert not store.known_context("ctx")
    assert store.lookup("ctx", 1) is None


def test_records_are_looked_up_by_position(store):
    store.add_record("ctx", record("hash", 1, [[{"t": "int", "v": 1}]]))
    store.add_record("ctx", record("hash", 2, [[{"t": "int", "v": 2}]]))
    assert store.lookup("ctx", 1).rows == [[{"t": "int", "v": 1}]]
    assert store.lookup("ctx", 2).rows == [[{"t": "int", "v": 2}]]
    assert store.lookup("ctx", 3) is None


def test_the_hash_of_a_record_is_kept_next_to_its_position(store):
    """A replay compares this hash with the statement that arrived at the position."""
    store.add_record("ctx", record("9f2c", 1, []))
    assert store.lookup("ctx", 1).hash == "9f2c"


def test_one_position_holds_one_record(store):
    """The position is the key of the table: the latest record of it wins."""
    store.add_record("ctx", record("first", 1, []))
    store.add_record("ctx", record("second", 1, []))
    assert store.lookup("ctx", 1).hash == "second"
    assert len(store.records("ctx")) == 1


def test_contexts_are_isolated(store):
    store.add_record("a", record("hash", 1, [[{"t": "int", "v": 1}]]))
    assert not store.known_context("b")
    assert store.lookup("b", 1) is None


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
    loaded = store.lookup("ctx", 3)
    assert loaded == original


def test_replace_context_drops_the_previous_records(store):
    store.add_record("ctx", record("old", 1, []))
    store.replace_context("ctx", [record("new", 1, [])])
    assert store.lookup("ctx", 1).hash == "new"
    assert len(store.records("ctx")) == 1


def test_forget_context(store):
    store.add_record("ctx", record("hash", 1, []))
    store.forget_context("ctx")
    assert not store.known_context("ctx")
    assert store.records("ctx") == []


def test_records_of_a_context_are_ordered_by_position(store):
    store.add_record("ctx", record("b", 2, []))
    store.add_record("ctx", record("a", 3, []))
    store.add_record("ctx", record("a", 1, []))
    assert [(item.hash, item.n) for item in store.records("ctx")] == [("a", 1), ("b", 2), ("a", 3)]


def test_contexts_are_listed(store):
    store.add_record("b", record("hash", 1, []))
    store.add_record("a", record("hash", 1, []))
    assert store.contexts() == ["a", "b"]
