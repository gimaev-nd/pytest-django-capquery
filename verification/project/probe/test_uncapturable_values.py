"""What the plugin does with a value it cannot store at all.

capquery understands what Django and the driver prepare for the column types a table can
have: ``jsonb`` (the ``Jsonb`` wrapper), arrays (plain lists), ``inet``
(``ipaddress`` objects), the range types (``Range``), ``bytea`` and ``tsvector``.  It
draws the line at a *project's own* type — one that arrives with a dumper of its own, or
a postgres type no loader of the driver was taught (a multirange).  Such a statement is
executed against postgres, the test keeps working, pytest still exits 0, no capture file
appears for it — and the plugin *says so*::

    capquery: <nodeid> was not captured: it holds values capquery cannot store

That report is the whole point of this module: before, the decision ``unsupported`` fell
through every branch of the retry loop and the test silently never got a capture file.

This module is run as its own session (``pytest probe``) because one uncapturable test
makes the session redo its migration phase for real, which would hide the "nothing but
DDL reaches postgres" property the main run checks.  It doubles as the regression test of
the fixed clock: with an uncapturable test in the session the phase *is* recorded for
real every run, so ``captures/migrations.yaml`` must still come out byte-identical.
"""

from __future__ import annotations

import datetime

import psycopg
import pytest
from django.db import connection
from psycopg.types.string import StrDumper

from demo.shop.models import Comment

UTC = datetime.timezone.utc


class Pixel:
    """A type of the project's own, with a dumper that teaches postgres how to read it."""

    def __init__(self, x: int, y: int) -> None:
        self.x, self.y = x, y

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Pixel({self.x}, {self.y})"


class PixelDumper(StrDumper):
    def dump(self, obj) -> bytes:
        return f"({obj.x},{obj.y})".encode()


psycopg.adapters.register_dumper(Pixel, PixelDumper)


def test_a_parameter_of_an_unknown_type(db):
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s::text", [Pixel(3, 4)])
        assert cursor.fetchone() == ("(3,4)",)


def test_a_list_of_unknown_values(db):
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s::text[]", [[Pixel(1, 1), Pixel(2, 2)]])
        assert cursor.fetchone()[0] == ["(1,1)", "(2,2)"]


def test_an_unknown_value_next_to_a_capturable_one(db):
    """One bad value makes the whole statement uncapturable, not half of it."""
    today = datetime.datetime(2024, 6, 1, tzinfo=UTC)
    assert Comment.objects.filter(created=today).count() == 0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT %s::text, count(*) FROM shop_comment WHERE created = %s::timestamptz",
            [Pixel(5, 6), today],
        )
        assert cursor.fetchone() == ("(5,6)", 0)


def test_an_update_with_an_unknown_value(db):
    with connection.cursor() as cursor:
        cursor.execute("UPDATE shop_comment SET body = %s::text WHERE id = %s", [Pixel(7, 8), 1])
        assert cursor.rowcount == 0


def test_a_result_of_an_unknown_type(db):
    """A multirange: postgres has the type, the driver has a loader for it, and it is
    still not something a capture file describes (it is a sequence of ranges)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT '{[1,3),[5,8)}'::int4multirange")
        multirange = cursor.fetchone()[0]
    assert [str(item) for item in multirange] == ["[1, 3)", "[5, 8)"]


@pytest.mark.parametrize(
    "sql, params",
    [
        ("SELECT %s::text", [Pixel(1, 2)]),
        ("SELECT %s::text[]", [[Pixel(3, 4)]]),
        ("SELECT %s::text, %s::text", [Pixel(5, 6), Pixel(7, 8)]),
    ],
    ids=["scalar", "list", "several"],
)
def test_unknown_values_in_every_shape(db, sql, params):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        assert cursor.fetchone() is not None
