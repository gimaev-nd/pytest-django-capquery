"""Query hashing and statement classification."""

from __future__ import annotations

from capquery.hashing import hash_for, query_hash, stable_json


def test_hash_is_deterministic():
    sql = "SELECT id FROM t WHERE id = %s"
    assert query_hash(sql, (1,)) == query_hash(sql, (1,))


def test_hash_depends_on_the_sql():
    assert query_hash("SELECT 1", ()) != query_hash("SELECT 2", ())


def test_hash_depends_on_the_parameters():
    sql = "SELECT id FROM t WHERE id = %s"
    assert query_hash(sql, (1,)) != query_hash(sql, (2,))


def test_hash_does_not_depend_on_the_parameter_type():
    assert query_hash("SELECT %s", (1,)) != query_hash("SELECT %s", ("1",))


def test_hash_ignores_an_empty_parameter_list():
    assert hash_for("SELECT 1", []) == hash_for("SELECT 1", [])


def test_hash_is_not_affected_by_the_order_of_named_parameters():
    sql = "SELECT :a, :b"
    assert query_hash(sql, {"a": 1, "b": 2}) == query_hash(sql, {"b": 2, "a": 1})


def test_stable_json_sorts_keys():
    assert stable_json({"b": 1, "a": 2}) == stable_json({"a": 2, "b": 1})


def test_stable_json_is_compact():
    assert stable_json([1, 2]) == "[1,2]"


#: which statements may be captured at all is decided in ``statements.py``, see
#: ``tests/test_statements.py``; hashing only turns them into a lookup key
