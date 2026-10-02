"""Deterministic seed data, written with raw SQL on purpose.

The rows themselves carry postgres types that a query *parameter* can hold as a plain
value — arrays (lists), ``bytea`` (bytes), ``timestamptz`` (datetimes), ``interval``
(timedeltas) — and types that only a text literal with a cast can express: ``jsonb``,
``daterange``/``tstzrange``/``int4range``, ``inet`` and ``tsvector``.  A stray
``Jsonb(...)``/``Range(...)`` object among the parameters is not a value the plugin can
store, so the seed keeps to values it can.

Ids are explicit and the sequences are set afterwards, so every run of every test
creates rows with exactly the same ids — that is what makes the captures reproducible
instead of "stable until the sequence moves".
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from django.db import migrations

UTC = datetime.timezone.utc

TEAMS = [(1, "core"), (2, "platform"), (3, "payments")]

#: (id, code, title, status, priority, price, effort, created, closed_at, labels,
#:  scores, meta, active, review, weight, peer, payload, document, owner_id)
TICKETS: list[tuple[Any, ...]] = [
    (
        1,
        uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "Broken login",
        "open",
        2,
        "10.50",
        datetime.timedelta(hours=2),
        datetime.datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
        None,
        ["bug", "ui"],
        [1, 2],
        '{"team": "core", "level": 1, "tags": ["x"]}',
        "[2024-01-01,2024-01-31)",
        "[2024-01-01 10:00:00+00,2024-01-02 10:00:00+00)",
        "[10,20)",
        "10.0.0.1",
        b"\x00\x01seed",
        "broken login",
        1,
    ),
    (
        2,
        uuid.UUID("00000000-0000-0000-0000-000000000002"),
        "Slow report",
        "closed",
        1,
        "20.00",
        datetime.timedelta(minutes=90),
        datetime.datetime(2024, 2, 1, 9, 30, tzinfo=UTC),
        datetime.datetime(2024, 2, 3, 12, 0, tzinfo=UTC),
        ["bug", "perf"],
        [3],
        '{"team": "core", "level": 3}',
        "[2024-02-01,2024-03-01)",
        "[2024-02-01 09:00:00+00,2024-02-04 09:00:00+00)",
        "[5,10)",
        "10.0.0.2",
        None,
        "slow report",
        1,
    ),
    (
        3,
        uuid.UUID("00000000-0000-0000-0000-000000000003"),
        "Broken export",
        "hold",
        3,
        "30.25",
        datetime.timedelta(hours=1, minutes=15),
        datetime.datetime(2024, 3, 1, 8, 0, tzinfo=UTC),
        None,
        ["bug", "export"],
        [4, 5],
        '{"team": "platform", "level": 2}',
        "[2024-03-01,2024-04-01)",
        "[2024-03-01 08:00:00+00,2024-03-02 08:00:00+00)",
        "[20,30)",
        "192.168.1.10",
        b"\x02seed",
        "broken export",
        2,
    ),
    (
        4,
        uuid.UUID("00000000-0000-0000-0000-000000000004"),
        "Nice to have",
        "open",
        1,
        "5.00",
        None,
        datetime.datetime(2024, 1, 15, 18, 45, tzinfo=UTC),
        None,
        ["ui"],
        [],
        '{"team": "platform"}',
        "[2024-01-15,2024-02-15)",
        None,
        "[1,5)",
        None,
        None,
        "nice to have",
        2,
    ),
    (
        5,
        uuid.UUID("00000000-0000-0000-0000-000000000005"),
        "Broken cache",
        "closed",
        2,
        "15.75",
        datetime.timedelta(hours=3),
        datetime.datetime(2024, 5, 1, 7, 15, tzinfo=UTC),
        datetime.datetime(2024, 5, 2, 7, 15, tzinfo=UTC),
        ["bug", "cache"],
        [7],
        '{"team": "core", "level": 1, "tags": ["y"]}',
        "[2024-05-01,2024-06-01)",
        "[2024-05-01 07:00:00+00,2024-05-02 07:00:00+00)",
        "[30,40)",
        "10.0.0.5",
        b"\x03seed",
        "broken cache",
        3,
    ),
]

WATCHERS = [(1, 1), (1, 2), (2, 1), (3, 2), (3, 3)]

INSERT_TEAM = "INSERT INTO shop_team (id, name) VALUES (%s, %s)"
INSERT_TICKET = """
INSERT INTO shop_ticket (
    id, code, title, status, priority, price, effort, created, closed_at,
    labels, scores, meta, active, review, weight, peer, payload, document, owner_id
) VALUES (
    %s, %s, %s, %s, %s, %s::numeric, %s::interval, %s::timestamptz, %s::timestamptz,
    %s::text[], %s::int[], %s::jsonb, %s::daterange, %s::tstzrange, %s::int4range,
    %s::inet, %s::bytea, to_tsvector('english', %s), %s
)
"""
INSERT_WATCHER = "INSERT INTO shop_ticket_watchers (ticket_id, team_id) VALUES (%s, %s)"


def seed(apps, schema_editor) -> None:
    cursor = schema_editor.connection.cursor()
    for team in TEAMS:
        cursor.execute(INSERT_TEAM, list(team))
    for ticket in TICKETS:
        cursor.execute(INSERT_TICKET, list(ticket))
    for watcher in WATCHERS:
        cursor.execute(INSERT_WATCHER, list(watcher))
    # explicit ids do not move the sequences, so put them where the seeds end: the
    # first row a test creates then has the same id on every run
    cursor.execute("SELECT setval(pg_get_serial_sequence('shop_ticket', 'id'), %s)", [len(TICKETS)])
    cursor.execute("SELECT setval(pg_get_serial_sequence('shop_team', 'id'), %s)", [len(TEAMS)])


def unseed(apps, schema_editor) -> None:
    cursor = schema_editor.connection.cursor()
    cursor.execute("DELETE FROM shop_ticket_watchers")
    cursor.execute("DELETE FROM shop_ticket")
    cursor.execute("DELETE FROM shop_team")


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
