"""A small Django suite that the integration tests run twice.

The first run records the captures, the second one replays them.  Every test here
is deterministic on purpose: the plugin is supposed to reproduce the recorded run
exactly.

``test_sequence_starts_from_the_seed`` must stay the first test that creates a
row: it asserts that the id sequence starts right after the seed migration, which
is only true because capquery runs ``sqlsequencereset`` before the migrations.
"""

from __future__ import annotations

import datetime
import decimal
import os
import uuid

import psycopg
import pytest
from django.conf import settings
from django.db import connection, models

from demo.shop.models import Order


class OrderName(models.TextChoices):
    """Django's own str-mixin enum: a member is a ``str``, but not a ``str``.

    A query parameter like this one is the sort of value the plugin has to store in
    a capture file, and PyYAML has no representer for a subclass of a builtin.
    """

    FIRST = "first"
    SECOND = "second"

#: under pytest-xdist the tests are spread over workers, so the ids of the rows a
#: test created cannot be predicted from the order of the tests in the file
PARALLEL = os.environ.get("PYTEST_XDIST_WORKER") is not None


@pytest.mark.skipif(PARALLEL, reason="row ids depend on the order of the tests in one process")
def test_sequence_starts_from_the_seed(db):
    # the seed migration uses ids 1..3, so the first new row of a run gets id 4
    created = Order.objects.create(name="sequence", amount=decimal.Decimal("1.00"))
    assert created.id == 4


def test_orders_count(db):
    assert Order.objects.count() == 3


def test_read_typed_values(db):
    order = Order.objects.get(id=1)
    assert order.name == "first"
    assert order.amount == decimal.Decimal("10.50")
    assert order.token == uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert order.created == datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    assert bytes(order.payload) == b"\x00\x01capquery"


def test_filter_by_amount(db):
    names = list(
        Order.objects.filter(amount__gte=decimal.Decimal("20")).values_list("name", flat=True)
    )
    assert names == ["second", "third"]


def test_an_enum_parameter_is_captured(db):
    """A query parameter that is a ``str`` subclass, as Django's choices are.

    Recording this statement used to take the whole session down: the value reached
    PyYAML, which has no representer for a subclass of a builtin.
    """
    assert list(Order.objects.filter(name=OrderName.FIRST).values_list("id", flat=True)) == [1]
    assert Order.objects.filter(name=OrderName.SECOND).count() == 1


def test_same_query_after_update_returns_the_next_result(db):
    before = Order.objects.get(id=2).name
    Order.objects.filter(id=2).update(name="second-updated")
    after = Order.objects.get(id=2).name
    assert (before, after) == ("second", "second-updated")


def test_raw_query_with_several_columns(db):
    with connection.cursor() as cursor:
        cursor.execute("SELECT name, amount FROM shop_order ORDER BY id")
        rows = cursor.fetchall()
        columns = [column[0] for column in cursor.description]
        rowcount = cursor.rowcount
    assert columns == ["name", "amount"]
    assert rowcount == 3
    assert rows[0] == ("first", decimal.Decimal("10.50"))


def test_create_and_read_back(db):
    created = Order.objects.create(name="fresh", amount=decimal.Decimal("1.00"))
    assert created.id  # the captures must not care where the sequence happens to be
    assert Order.objects.filter(name="fresh").count() == 1


def test_many_reads(db):
    for _ in range(25):
        assert Order.objects.count() == 3


def test_update_reports_the_row_count(db):
    updated = Order.objects.filter(name="third").update(amount=decimal.Decimal("42.00"))
    assert updated == 1
    assert Order.objects.get(name="third").amount == decimal.Decimal("42.00")


def test_delete_reports_the_row_count(db):
    deleted, per_model = Order.objects.filter(name="second").delete()
    assert deleted == 1
    assert list(per_model.values()) == [1]
    assert Order.objects.count() == 2


def test_bulk_create_returns_the_new_ids(db):
    orders = Order.objects.bulk_create(
        [
            Order(name="bulk-one", amount=decimal.Decimal("1.00")),
            Order(name="bulk-two", amount=decimal.Decimal("2.00")),
        ]
    )
    assert all(order.id for order in orders)
    assert Order.objects.filter(name__startswith="bulk-").count() == 2


def test_raw_insert_with_returning(db):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO shop_order (name, amount, created, token, payload)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            [
                "raw-insert",
                decimal.Decimal("7.00"),
                datetime.datetime(2024, 2, 2, 12, 0, tzinfo=datetime.timezone.utc),
                uuid.UUID("33333333-3333-3333-3333-333333333333"),
                b"\x00raw",
            ],
        )
        new_id = cursor.fetchone()[0]
        rowcount = cursor.rowcount
    assert rowcount == 1
    assert Order.objects.filter(id=new_id, name="raw-insert").count() == 1


def test_raw_executemany(db):
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO shop_order (name, amount, created, token, payload)"
            " VALUES (%s, %s, %s, %s, %s)",
            [
                (
                    "many-one",
                    decimal.Decimal("1.00"),
                    datetime.datetime(2024, 3, 1, 12, 0, tzinfo=datetime.timezone.utc),
                    uuid.UUID("44444444-4444-4444-4444-444444444444"),
                    b"\x00many",
                ),
                (
                    "many-two",
                    decimal.Decimal("2.00"),
                    datetime.datetime(2024, 3, 2, 12, 0, tzinfo=datetime.timezone.utc),
                    uuid.UUID("55555555-5555-5555-5555-555555555555"),
                    b"\x00many",
                ),
            ],
        )
        assert cursor.rowcount == 2
    assert Order.objects.filter(name__startswith="many-").count() == 2


def test_delete_all_rows(db):
    deleted, _ = Order.objects.all().delete()
    assert deleted == 3
    assert Order.objects.count() == 0


def test_the_raw_database_holds_only_what_was_really_written(django_db_setup):
    """The captures answer the tests, the database itself stays untouched.

    This test talks to postgres with a plain driver instead of Django, so capquery
    neither sees it nor answers it: it reports what really is in the database.  The
    expected number comes from the environment, which lets the integration tests ask
    for it on a replayed run only.
    """
    expected = os.environ.get("CAPQUERY_EXPECT_RAW_ROWS")
    if expected is None:
        pytest.skip("CAPQUERY_EXPECT_RAW_ROWS is not set")
    config = settings.DATABASES["default"]
    with psycopg.connect(
        dbname=config["NAME"],
        host=config["HOST"] or None,
        port=config["PORT"] or None,
        user=config["USER"],
        password=config["PASSWORD"] or None,
    ) as raw:
        rows = raw.execute("SELECT count(*) FROM shop_order").fetchone()[0]
    assert rows == int(expected)


def test_without_queries(db):
    assert 2 + 2 == 4


@pytest.mark.capquery_ignore
def test_ignored_is_not_captured(db):
    assert Order.objects.count() == 3
