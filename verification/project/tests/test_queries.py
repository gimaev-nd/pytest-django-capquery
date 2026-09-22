"""The query shapes that make the SQL interesting.

``DISTINCT ON``, subqueries (scalar, ``EXISTS``, ``IN``), window functions, ``UNION``,
``GROUP BY ... HAVING``, filtered aggregates and ``CASE`` — the statements a cache of
query results has to reproduce exactly, including their order.
"""

from __future__ import annotations

import datetime
import decimal

from django.db.models import (
    Case,
    CharField,
    Count,
    DateTimeField,
    DecimalField,
    Exists,
    F,
    OuterRef,
    Q,
    RowRange,
    Subquery,
    Sum,
    Value,
    When,
    Window,
)
from django.db.models.functions import RowNumber, TruncDate

from demo.shop.models import Comment, Team, Ticket

UTC = datetime.timezone.utc


def test_distinct_statuses(db):
    statuses = Ticket.objects.order_by("status").values_list("status", flat=True).distinct()
    assert list(statuses) == ["closed", "hold", "open"]


def test_distinct_on_a_column(db):
    rows = Ticket.objects.order_by("status", "id").distinct("status").values_list("status", "id")
    assert list(rows) == [("closed", 2), ("hold", 3), ("open", 1)]


def test_distinct_on_a_json_key(db):
    rows = (
        Ticket.objects.order_by("meta__team", "id")
        .distinct("meta__team")
        .values_list("meta__team", "id")
    )
    assert list(rows) == [("core", 1), ("platform", 3)]


def test_scalar_subquery_annotation(db):
    rows = (
        Ticket.objects.annotate(
            owner_name=Subquery(Team.objects.filter(id=OuterRef("owner_id")).values("name")[:1])
        )
        .order_by("id")
        .values_list("id", "owner_name")
    )
    assert list(rows) == [
        (1, "core"),
        (2, "core"),
        (3, "platform"),
        (4, "platform"),
        (5, "payments"),
    ]


def test_exists_subquery_as_a_filter(db):
    teams = (
        Team.objects.filter(Exists(Ticket.objects.filter(owner=OuterRef("pk"), status="closed")))
        .order_by("id")
        .values_list("name", flat=True)
    )
    assert list(teams) == ["core", "payments"]


def test_subquery_in_an_in_lookup(db):
    tickets = Ticket.objects.filter(
        owner__in=Subquery(Team.objects.filter(name__startswith="c").values("id"))
    ).order_by("id")
    assert list(tickets.values_list("id", flat=True)) == [1, 2]


def test_excludes_with_a_subquery(db):
    tickets = Ticket.objects.exclude(
        owner__in=Subquery(Team.objects.filter(name="core").values("id"))
    ).order_by("id")
    assert list(tickets.values_list("id", flat=True)) == [3, 4, 5]


def test_group_by_with_having(db):
    rows = (
        Ticket.objects.values("status")
        .annotate(rows=Count("id"), total=Sum("price"))
        .filter(rows__gte=2)
        .order_by("status")
    )
    assert list(rows) == [
        {"status": "closed", "rows": 2, "total": decimal.Decimal("35.75")},
        {"status": "open", "rows": 2, "total": decimal.Decimal("15.50")},
    ]


def test_count_with_a_filter_over_a_join(db):
    teams = (
        Team.objects.annotate(
            total=Count("tickets", distinct=True),
            bugs=Count("tickets", filter=Q(tickets__labels__contains=["bug"])),
        )
        .order_by("id")
        .values_list("name", "total", "bugs")
    )
    assert list(teams) == [("core", 2, 2), ("platform", 2, 1), ("payments", 1, 1)]


def test_window_function_ranking(db):
    rows = (
        Ticket.objects.annotate(
            rank=Window(
                expression=RowNumber(),
                partition_by=[F("status")],
                order_by=F("priority").desc(),
            )
        )
        .order_by("id")
        .values_list("id", "rank")
    )
    assert list(rows) == [(1, 1), (2, 2), (3, 1), (4, 2), (5, 1)]


def test_window_frame_sum(db):
    rows = (
        Ticket.objects.annotate(
            running=Window(
                expression=Sum("price"),
                order_by=F("id").asc(),
                frame=RowRange(start=None, end=0),
            )
        )
        .order_by("id")
        .values_list("id", "running")
    )
    values = list(rows)
    assert [row[0] for row in values] == [1, 2, 3, 4, 5]
    assert values[-1][1] == decimal.Decimal("81.50")


def test_union_of_two_querysets(db):
    open_tickets = Ticket.objects.filter(status="open").values_list("id", flat=True)
    urgent = Ticket.objects.filter(priority=1).values_list("id", flat=True)
    assert sorted(open_tickets.union(urgent)) == [1, 2, 4]


def test_union_all_of_two_querysets(db):
    first = Ticket.objects.filter(status="closed").values_list("id", flat=True)
    second = Ticket.objects.filter(priority=2).values_list("id", flat=True)
    assert sorted(first.union(second, all=True)) == [1, 2, 5, 5]


def test_values_of_a_related_row(db):
    rows = Ticket.objects.values("owner__name").annotate(rows=Count("id")).order_by("owner__name")
    assert list(rows) == [
        {"owner__name": "core", "rows": 2},
        {"owner__name": "payments", "rows": 1},
        {"owner__name": "platform", "rows": 2},
    ]


def test_trunc_date_annotation(db):
    rows = (
        Ticket.objects.annotate(day=TruncDate("created"))
        .values("day")
        .annotate(rows=Count("id"))
        .order_by("day")
    )
    days = [row["day"].isoformat() for row in rows]
    assert days == ["2024-01-01", "2024-01-15", "2024-02-01", "2024-03-01", "2024-05-01"]


def test_case_when_annotation(db):
    rows = (
        Ticket.objects.annotate(
            bucket=Case(
                When(priority__gte=3, then=Value("high")),
                When(priority=1, then=Value("low")),
                default=Value("normal"),
                output_field=CharField(),
            )
        )
        .order_by("id")
        .values_list("title", "bucket")
    )
    assert list(rows) == [
        ("Broken login", "normal"),
        ("Slow report", "low"),
        ("Broken export", "high"),
        ("Nice to have", "low"),
        ("Broken cache", "normal"),
    ]


def test_case_with_a_null_branch(db):
    rows = list(
        Ticket.objects.annotate(
            closed=Case(
                When(closed_at__isnull=True, then=Value(None)),
                default=F("closed_at"),
                output_field=DateTimeField(),
            )
        )
        .order_by("id")
        .values_list("id", "closed")
    )
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
    assert rows[1][1] == datetime.datetime(2024, 2, 3, 12, 0, tzinfo=UTC)
    assert rows[0][1] is None


def test_arithmetic_on_a_decimal_column(db):
    rows = (
        Ticket.objects.annotate(rubles=F("price") * Value(decimal.Decimal("2")))
        .order_by("id")
        .values_list("id", "rubles")
    )
    assert rows[0] == (1, decimal.Decimal("21.00"))
    assert rows[4] == (5, decimal.Decimal("31.50"))


def test_select_for_update_on_a_plain_model(db):
    comment = Comment.objects.create(
        ticket_id=1,
        body="first comment",
        score=1,
        created=datetime.datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
    )
    locked = Comment.objects.select_for_update().filter(ticket_id=1).values_list("id", flat=True)
    assert list(locked) == [comment.id]


def test_sum_of_an_expression(db):
    discounted = Ticket.objects.annotate(price_with_tax=F("price") * Value(decimal.Decimal("1.5")))
    total = discounted.aggregate(total=Sum("price_with_tax", output_field=DecimalField(max_digits=10, decimal_places=2)))
    assert total["total"] == decimal.Decimal("122.25")
