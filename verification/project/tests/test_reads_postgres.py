"""Reads over the postgres-only column types.

Every query here is written the way a real project writes it: the ORM with a postgres
lookup, or raw SQL where the ORM cannot express the value (``jsonb``/``inet``/range
literals).  The queries deliberately avoid *selecting* the columns whose values the
plugin cannot store — ``probe/test_unstorable_values.py`` shows what happens then.

The seed of the model (``demo/shop/migrations/0002_seed.py``) is:

    id  title          status  prio  price  labels         scores meta.team  weight    peer
    1   Broken login   open    2     10.50  bug,ui         1,2    core       [10,20)   10.0.0.1
    2   Slow report    closed  1     20.00  bug,perf       3      core       [5,10)    10.0.0.2
    3   Broken export  hold    3     30.25  bug,export     4,5    platform   [20,30)   192.168.1.10
    4   Nice to have   open    1     5.00   ui             -      platform   [1,5)     NULL
    5   Broken cache   closed  2     15.75  bug,cache      7      core       [30,40)   10.0.0.5
"""

from __future__ import annotations

import datetime
import decimal
import uuid

from django.contrib.postgres.aggregates import ArrayAgg, StringAgg
from django.contrib.postgres.search import SearchQuery, SearchVector
from django.db import connection
from django.db.models import Q, Sum, TextField
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from demo.shop.models import Ticket

UTC = datetime.timezone.utc


def ids(queryset) -> list[int]:
    return list(queryset.order_by("id").values_list("id", flat=True))


def test_array_contains_a_label(db):
    assert ids(Ticket.objects.filter(labels__contains=["bug"])) == [1, 2, 3, 5]


def test_array_overlaps_a_list(db):
    assert ids(Ticket.objects.filter(labels__overlap=["ui", "perf"])) == [1, 2, 4]


def test_array_length_and_index(db):
    assert ids(Ticket.objects.filter(labels__len=2)) == [1, 2, 3, 5]
    assert ids(Ticket.objects.filter(scores__0=4)) == [3]
    assert ids(Ticket.objects.filter(scores__1=2)) == [1]


def test_array_of_integers_contains(db):
    assert ids(Ticket.objects.filter(scores__contains=[4])) == [3]
    assert ids(Ticket.objects.filter(scores__contains=[1, 2])) == [1]


def test_array_agg_annotation(db):
    # array_agg of arrays needs every array to have the same dimensionality, so the
    # whole table cannot be aggregated that way: agg of a scalar column instead
    aggregated = Ticket.objects.aggregate(titles=StringAgg("title", delimiter=",", order_by="id"))
    assert aggregated["titles"] == (
        "Broken login,Slow report,Broken export,Nice to have,Broken cache"
    )


def test_array_agg_with_a_filter(db):
    aggregated = Ticket.objects.aggregate(
        bug_labels=ArrayAgg("labels", filter=Q(labels__contains=["bug"]), order_by="id")
    )
    assert aggregated["bug_labels"] == [
        ["bug", "ui"],
        ["bug", "perf"],
        ["bug", "export"],
        ["bug", "cache"],
    ]


def test_json_key_exists(db):
    assert ids(Ticket.objects.filter(meta__has_key="tags")) == [1, 5]


def test_json_key_equals_a_text(db):
    # the key transform reads the value as text, so the parameter stays a str
    rows = (
        Ticket.objects.annotate(team=KeyTextTransform("team", "meta"))
        .filter(team="core")
        .order_by("id")
        .values_list("id", flat=True)
    )
    assert list(rows) == [1, 2, 5]


def test_json_nested_key_compared_as_text(db):
    rows = (
        Ticket.objects.annotate(level=KeyTextTransform("level", "meta"))
        .filter(level__gte="2")
        .order_by("id")
        .values_list("id", flat=True)
    )
    assert list(rows) == [2, 3]


def test_json_reads_back_as_a_python_object(db):
    assert Ticket.objects.values_list("meta", flat=True).get(id=1) == {
        "team": "core",
        "level": 1,
        "tags": ["x"],
    }


def test_json_key_of_every_row(db):
    values = Ticket.objects.order_by("id").values_list("meta__level", flat=True)
    assert list(values) == [1, 3, 2, None, 1]


def test_date_range_contains_a_day(db):
    assert ids(Ticket.objects.filter(active__contains=datetime.date(2024, 1, 15))) == [1, 4]


def test_integer_range_contains_a_number(db):
    assert ids(Ticket.objects.filter(weight__contains=7)) == [2]
    assert ids(Ticket.objects.filter(weight__contains=25)) == [3]


def test_inet_column_reads_as_text(db):
    peers = Ticket.objects.order_by("id").values_list("peer", flat=True)
    assert list(peers) == ["10.0.0.1", "10.0.0.2", "192.168.1.10", None, "10.0.0.5"]


def test_inet_column_cast_to_text(db):
    # the explicit cast goes through postgres' own inet output, which keeps the mask
    queryset = (
        Ticket.objects.annotate(peer_text=Cast("peer", output_field=TextField()))
        .filter(peer_text__startswith="10.0.0.")
        .order_by("id")
    )
    assert list(queryset.values_list("id", "peer_text")) == [
        (1, "10.0.0.1/32"),
        (2, "10.0.0.2/32"),
        (5, "10.0.0.5/32"),
    ]


def test_uuid_datetime_bytea_and_interval(db):
    row = Ticket.objects.values("code", "created", "payload", "effort").get(id=1)
    assert row["code"] == uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert row["created"] == datetime.datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    assert bytes(row["payload"]) == b"\x00\x01seed"
    assert row["effort"] == datetime.timedelta(hours=2)


def test_decimal_sum_aggregate(db):
    aggregated = Ticket.objects.aggregate(total=Sum("price"))
    assert aggregated["total"] == decimal.Decimal("81.50")


def test_full_text_search_with_a_search_vector(db):
    Ticket.objects.filter(id=1).update(document=SearchVector("title", config="english"))
    found = (
        Ticket.objects.filter(document=SearchQuery("broken", config="english"))
        .order_by("id")
        .values_list("id", flat=True)
    )
    assert list(found) == [1, 3, 5]


def test_tsvector_column_reads_as_text(db):
    document = Ticket.objects.values_list("document", flat=True).get(id=1)
    assert isinstance(document, str)
    assert "'broken':1" in document


def test_raw_sql_with_an_array_parameter(db):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, labels FROM shop_ticket WHERE labels::text[] && %s::text[] ORDER BY id",
            [["ui"]],
        )
        rows = cursor.fetchall()
        rowcount = cursor.rowcount
    assert rows == [(1, ["bug", "ui"]), (4, ["ui"])]
    assert rowcount == 2


def test_raw_sql_with_an_any_parameter(db):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, title FROM shop_ticket WHERE id = ANY(%s) ORDER BY id", [[1, 3]]
        )
        assert cursor.fetchall() == [(1, "Broken login"), (3, "Broken export")]


def test_raw_sql_over_ranges_and_json(db):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, meta->>'team', upper(weight) FROM shop_ticket"
            " WHERE active && %s::daterange ORDER BY id",
            ["[2024-01-10,2024-01-20)"],
        )
        rows = cursor.fetchall()
    assert rows == [(1, "core", 20), (4, "platform", 5)]
