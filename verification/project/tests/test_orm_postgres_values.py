"""The code a normal project writes on a model with the postgres-only columns.

``Ticket.objects.get(id=1)``, ``Ticket.objects.create(...)``, a ``jsonb`` containment or
key comparison, an ``inet`` or a range parameter, ``QuerySet.delete()`` — every one of
these hands the driver an object rather than a plain value:

    Ticket.objects.create(meta={...}, active=(a, b), peer="10.0.0.1")
        INSERT ... %s::jsonb, %s::daterange, %s::inet    -- Jsonb, Range, IPv4Address
    Ticket.objects.filter(meta__team="core")             -- Jsonb in the comparison
    Ticket.objects.filter(active__overlap=(a, b))        -- Range
    Ticket.objects.get(id=1)                             -- the row holds Range objects

capquery stores what those objects stand for, so the whole suite replays — and the
harness refuses to pass unless *every* managed test of this project produced a capture
file (``run.py``, "run 1 created one capture per managed test").
"""

from __future__ import annotations

import datetime
import uuid

from demo.shop.models import Ticket

UTC = datetime.timezone.utc


def test_orm_read_of_a_postgres_row(db):
    # SELECT of every column, so the row holds an inet and three ranges
    ticket = Ticket.objects.get(id=1)
    assert ticket.title == "Broken login"
    assert ticket.labels == ["bug", "ui"]


def test_orm_read_of_a_range_column(db):
    active = Ticket.objects.values_list("active", flat=True).get(id=1)
    assert active.lower == datetime.date(2024, 1, 1)
    assert active.upper == datetime.date(2024, 1, 31)
    assert active.bounds == "[)"


def test_orm_read_of_an_inet_column(db):
    assert str(Ticket.objects.values_list("peer", flat=True).get(id=1)) == "10.0.0.1"


def test_orm_create_of_a_postgres_row(db):
    created = Ticket.objects.create(
        code=uuid.UUID("00000000-0000-0000-0000-00000000abc1"),
        title="created through the ORM",
        status="open",
        priority=1,
        price="1.00",
        created=datetime.datetime(2024, 6, 1, tzinfo=UTC),
        labels=["orm"],
        scores=[1],
        meta={"team": "core"},
        active=(datetime.date(2024, 6, 1), datetime.date(2024, 7, 1)),
        weight=(1, 10),
        peer="10.0.0.9",
    )
    assert created.pk is not None
    assert Ticket.objects.filter(code=created.code).exists()


def test_orm_create_returns_the_recorded_id(db):
    """An INSERT ... RETURNING id is answered from the capture on a replay."""
    first = Ticket.objects.create(
        code=uuid.UUID("00000000-0000-0000-0000-00000000abc2"),
        title="first",
        status="open",
        priority=1,
        price="1.00",
        created=datetime.datetime(2024, 6, 1, tzinfo=UTC),
    )
    second = Ticket.objects.create(
        code=uuid.UUID("00000000-0000-0000-0000-00000000abc3"),
        title="second",
        status="open",
        priority=1,
        price="1.00",
        created=datetime.datetime(2024, 6, 1, tzinfo=UTC),
    )
    assert second.pk == first.pk + 1


def test_jsonb_containment_parameter(db):
    assert Ticket.objects.filter(meta__contains={"team": "core"}).count() == 3


def test_jsonb_equality_parameter(db):
    # jsonb equality compares the whole document: only one ticket has exactly this one
    assert Ticket.objects.filter(meta={"team": "platform"}).count() == 1


def test_json_key_compared_to_a_value(db):
    assert Ticket.objects.filter(meta__team="core").count() == 3


def test_json_nested_key_compared_to_a_number(db):
    assert Ticket.objects.filter(meta__level__gte=2).count() == 2


def test_orm_update_of_a_json_column(db):
    assert Ticket.objects.filter(id=1).update(meta={"team": "core", "level": 7}) == 1
    assert Ticket.objects.values_list("meta", flat=True).get(id=1) == {"team": "core", "level": 7}


def test_orm_delete_of_a_postgres_row(db):
    deleted, per_model = Ticket.objects.filter(id=5).delete()
    assert deleted == 1
    assert list(per_model.values()) == [1]
    assert Ticket.objects.filter(id=5).count() == 0


def test_inet_parameter(db):
    assert Ticket.objects.filter(peer="10.0.0.1").count() == 1


def test_range_overlap_parameter(db):
    overlapping = Ticket.objects.filter(
        active__overlap=(datetime.date(2024, 1, 1), datetime.date(2024, 2, 1))
    )
    assert overlapping.count() == 2


def test_range_contained_by_parameter(db):
    assert Ticket.objects.filter(weight__contained_by=(5, 30)).count() == 3


def test_array_of_ranges(db):
    """A value whose own type is postgres-only, inside another one."""
    with_range = Ticket.objects.filter(id=1).values_list("active", flat=True).get()
    assert [with_range.lower, with_range.upper] == [
        datetime.date(2024, 1, 1),
        datetime.date(2024, 1, 31),
    ]
