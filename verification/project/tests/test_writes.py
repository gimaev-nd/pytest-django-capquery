"""Writes: what the database answered has to be reproduced on a replay.

The plugin caches writes like everything else, so ``INSERT ... RETURNING`` has to give
the id back, ``UPDATE``/``DELETE`` have to report the row count Django returns from
``QuerySet.update()``/``delete()``, and ``executemany`` is captured as one statement.

Writes to the plain model go through the ORM.  Writes to the postgres-typed model go
through raw SQL with explicit casts, because Django prepares ``jsonb``/``inet``/range
parameters as objects that a capture file cannot hold (see ``probe``).
"""

from __future__ import annotations

import datetime
import decimal
import uuid

from django.db import connection
from django.db.models import F, TextField
from django.db.models.functions import Cast

from demo.shop.models import Comment, Team, Ticket

from pg_sql import insert_ticket, ticket_row

UTC = datetime.timezone.utc


def test_orm_create_returns_the_new_id(db):
    comment = Comment.objects.create(
        ticket_id=1, body="create", score=1, created=datetime.datetime(2024, 6, 1, tzinfo=UTC)
    )
    assert comment.id is not None
    assert Comment.objects.get(pk=comment.id).body == "create"


def test_orm_save_twice_is_an_insert_then_an_update(db):
    comment = Comment(ticket_id=1, body="save", score=1, created=datetime.datetime(2024, 6, 1, tzinfo=UTC))
    comment.save()
    comment.body = "saved twice"
    comment.save()
    assert Comment.objects.get(pk=comment.id).body == "saved twice"


def test_orm_bulk_create_returns_ids(db):
    rows = Comment.objects.bulk_create(
        [
            Comment(ticket_id=1, body=f"bulk-{index}", score=index, created=datetime.datetime(2024, 6, 1, tzinfo=UTC))
            for index in range(3)
        ]
    )
    assert all(row.id for row in rows)
    assert Comment.objects.filter(body__startswith="bulk-").count() == 3


def test_orm_bulk_create_with_explicit_ids(db):
    rows = Comment.objects.bulk_create(
        [
            Comment(id=901, ticket_id=1, body="fixed-1", score=1, created=datetime.datetime(2024, 6, 1, tzinfo=UTC)),
            Comment(id=902, ticket_id=2, body="fixed-2", score=2, created=datetime.datetime(2024, 6, 2, tzinfo=UTC)),
        ]
    )
    assert [row.id for row in rows] == [901, 902]
    assert Comment.objects.filter(id__gte=901).count() == 2


def test_orm_update_reports_the_row_count(db):
    today = datetime.datetime(2024, 6, 1, tzinfo=UTC)
    Comment.objects.bulk_create(
        [
            Comment(ticket_id=1, body="a", score=1, created=today),
            Comment(ticket_id=1, body="b", score=2, created=today),
            Comment(ticket_id=2, body="c", score=3, created=today),
        ]
    )
    assert Comment.objects.filter(score__gte=2).update(body="bumped") == 2
    assert Comment.objects.filter(body="bumped").count() == 2


def test_orm_update_with_an_f_expression(db):
    today = datetime.datetime(2024, 6, 1, tzinfo=UTC)
    comment = Comment.objects.create(ticket_id=1, body="f", score=5, created=today)
    assert Comment.objects.filter(pk=comment.id).update(score=F("score") + 10) == 1
    assert Comment.objects.get(pk=comment.id).score == 15


def test_orm_delete_reports_the_row_count(db):
    today = datetime.datetime(2024, 6, 1, tzinfo=UTC)
    Comment.objects.bulk_create(
        [
            Comment(ticket_id=1, body="x", score=1, created=today),
            Comment(ticket_id=1, body="y", score=1, created=today),
        ]
    )
    deleted, per_model = Comment.objects.all().delete()
    assert deleted == 2
    assert list(per_model.values()) == [2]
    assert Comment.objects.count() == 0


def test_orm_cascade_delete_of_a_team(db):
    deleted, per_model = Team.objects.filter(name="payments").delete()
    # the team, its ticket and the row of the m2m table that pointed at the team
    assert deleted == 3
    assert per_model["shop.Team"] == 1 and per_model["shop.Ticket"] == 1
    assert Ticket.objects.filter(id=5).count() == 0


def test_orm_get_or_create_both_paths(db):
    today = datetime.datetime(2024, 6, 1, tzinfo=UTC)
    created = Comment.objects.get_or_create(
        ticket_id=1, body="only", defaults={"score": 1, "created": today}
    )
    again = Comment.objects.get_or_create(
        ticket_id=1, body="only", defaults={"score": 99, "created": today}
    )
    assert created[1] is True and created[0].score == 1
    assert again[1] is False and again[0].score == 1


def test_orm_update_or_create_both_paths(db):
    today = datetime.datetime(2024, 6, 1, tzinfo=UTC)
    row, created = Comment.objects.update_or_create(
        id=910, defaults={"ticket_id": 1, "body": "uoc", "score": 1, "created": today}
    )
    assert created is True
    row, created = Comment.objects.update_or_create(
        id=910, defaults={"ticket_id": 1, "body": "uoc-2", "score": 2, "created": today}
    )
    assert created is False and row.body == "uoc-2"


def test_raw_insert_with_returning(db):
    with connection.cursor() as cursor:
        new_id = insert_ticket(cursor, id=920, code="00000000-0000-0000-0000-000000000920", title="raw insert")
        rowcount = cursor.rowcount
    assert new_id == 920
    assert rowcount == 1
    assert Ticket.objects.filter(id=920).values_list("title", flat=True).get() == "raw insert"


def test_raw_insert_of_a_full_postgres_row(db):
    with connection.cursor() as cursor:
        new_id = insert_ticket(
            cursor,
            id=921,
            code="00000000-0000-0000-0000-000000000921",
            title="full row",
            labels=["bug", "raw"],
            scores=[9, 8],
            meta='{"team": "platform", "level": 9}',
            active="[2024-07-01,2024-08-01)",
            weight="[100,200)",
            peer="10.9.9.9",
        )
    assert new_id == 921
    # the range column is read as text: its own value type cannot be stored (see probe)
    row = (
        Ticket.objects.annotate(weight_text=Cast("weight", output_field=TextField()))
        .values("labels", "scores", "meta", "peer", "weight_text")
        .get(id=921)
    )
    assert row["labels"] == ["bug", "raw"]
    assert row["scores"] == [9, 8]
    assert row["meta"] == {"team": "platform", "level": 9}
    assert row["peer"] == "10.9.9.9"
    assert row["weight_text"] == "[100,200)"


def test_raw_update_reports_the_row_count(db):
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE shop_ticket SET priority = %s WHERE labels::text[] && %s::text[]",
            [9, ["bug"]],
        )
        updated = cursor.rowcount
    assert updated == 4
    assert Ticket.objects.filter(priority=9).count() == 4


def test_raw_delete_reports_the_row_count(db):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM shop_ticket WHERE id > 3")
        deleted = cursor.rowcount
    assert deleted == 2
    assert Ticket.objects.count() == 3


def test_raw_executemany(db):
    rows = [
        (930, 1, "many-one", 1, datetime.datetime(2024, 6, 1, tzinfo=UTC)),
        (931, 1, "many-two", 2, datetime.datetime(2024, 6, 2, tzinfo=UTC)),
        (932, 2, "many-three", 3, datetime.datetime(2024, 6, 3, tzinfo=UTC)),
    ]
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO shop_comment (id, ticket_id, body, score, created)"
            " VALUES (%s, %s, %s, %s, %s)",
            rows,
        )
        rowcount = cursor.rowcount
    assert rowcount == 3
    assert Comment.objects.filter(body__startswith="many-").count() == 3


def test_raw_insert_on_conflict_do_update(db):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO shop_ticket (id, code, title, status, priority, price, created,"
            " labels, scores, meta) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (code) DO UPDATE SET title = EXCLUDED.title, priority = EXCLUDED.priority"
            " RETURNING id",
            [
                940,
                uuid.UUID("00000000-0000-0000-0000-000000000001"),
                "upserted",
                "open",
                4,
                decimal.Decimal("1.00"),
                datetime.datetime(2024, 6, 1, tzinfo=UTC),
                ["upsert"],
                [1],
                '{"team": "core"}',
            ],
        )
        returned = cursor.fetchone()[0]
        rowcount = cursor.rowcount
    assert returned == 1
    assert rowcount == 1
    assert Ticket.objects.filter(id=1).values_list("title", "priority").get() == ("upserted", 4)


def test_raw_truncate(db):
    # a table this transaction did not write: postgres refuses to truncate one with
    # pending trigger events, i.e. one it wrote in the same transaction
    with connection.cursor() as cursor:
        insert_ticket(cursor, id=950, code="00000000-0000-0000-0000-000000000950", title="kept")
        cursor.execute("TRUNCATE shop_comment")
    assert Comment.objects.count() == 0
    assert Ticket.objects.filter(id=950).exists()


def test_orm_update_of_a_postgres_row(db):
    assert Ticket.objects.filter(id=1).update(price=decimal.Decimal("1.00")) == 1
    assert Ticket.objects.values_list("price", flat=True).get(id=1) == decimal.Decimal("1.00")


def test_orm_delete_of_a_plain_row(db):
    today = datetime.datetime(2024, 6, 1, tzinfo=UTC)
    Comment.objects.bulk_create(
        [
            Comment(ticket_id=1, body="z", score=1, created=today),
            Comment(ticket_id=2, body="w", score=1, created=today),
        ]
    )
    deleted, per_model = Comment.objects.filter(ticket_id=2).delete()
    assert deleted == 1
    assert list(per_model.values()) == [1]


def test_orm_values_update_keeps_the_row(db):
    with connection.cursor() as cursor:
        insert_ticket(cursor, id=960, code="00000000-0000-0000-0000-000000000960", title="kept")
    row = Ticket.objects.values("id", "title", "labels", "meta").get(id=960)
    assert row == {"id": 960, "title": "kept", "labels": ["new"], "meta": {"team": "core"}}
