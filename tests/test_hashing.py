"""Query hashing."""

from __future__ import annotations

import hashlib

from capquery.hashing import query_hash, stable_json


def test_hash_is_deterministic():
    sql = "SELECT id FROM t WHERE id = %s"
    assert query_hash(sql) == query_hash(sql)


def test_hash_depends_on_the_sql():
    assert query_hash("SELECT 1") != query_hash("SELECT 2")


def test_hash_is_the_hash_of_the_statement_text_alone():
    """The parameters take no part in the lookup key, so they take none in the hash.

    A capture is found by the position of its statement, and this hash only has to
    answer whether the query at that position is still the recorded one: a statement
    executed with a value that is new in every run is found again.
    """
    assert query_hash("SELECT id FROM t WHERE id = %s") == hashlib.sha256(
        b"SELECT id FROM t WHERE id = %s"
    ).hexdigest()


def test_two_executions_of_one_statement_share_the_hash():
    sql = "SELECT id FROM t WHERE name = %s"
    # the same statement: two positions of one context, one hash
    assert query_hash(sql) == query_hash(sql)
    assert query_hash(sql) != query_hash("SELECT id FROM t WHERE name = %s AND id > 0")


def test_stable_json_sorts_keys():
    assert stable_json({"b": 1, "a": 2}) == stable_json({"a": 2, "b": 1})


def test_stable_json_is_compact():
    assert stable_json([1, 2]) == "[1,2]"


#: which statements may be captured at all is decided in ``statements.py``, see
#: ``tests/test_statements.py``; hashing only turns a statement into its check value
