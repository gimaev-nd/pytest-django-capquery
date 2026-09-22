"""Raw SQL for the writes the ORM cannot prepare.

Django hands ``Jsonb``, ``Range`` and ``ipaddress`` objects to the driver for the
postgres-only fields, and none of them can be stored in a capture file — the plugin
then refuses to record the statement (``probe/test_unstorable_values.py`` shows that
side).  The writes here are the same statements with plain parameters and the casts
written out, which is exactly the SQL a capture file is expected to reproduce.
"""

from __future__ import annotations

import datetime
import uuid

UTC = datetime.timezone.utc

INSERT_TICKET = """
INSERT INTO shop_ticket (
    id, code, title, status, priority, price, effort, created, closed_at, labels, scores,
    meta, active, review, weight, peer, payload, document, owner_id
) VALUES (
    %s, %s, %s, %s, %s, %s::numeric, %s::interval, %s::timestamptz, NULL, %s::text[],
    %s::int[], %s::jsonb, %s::daterange, NULL, %s::int4range, %s::inet, %s::bytea,
    to_tsvector('english', %s), %s
) RETURNING id
"""


def ticket_row(
    *,
    id: int,
    code: str,
    title: str,
    status: str = "open",
    priority: int = 1,
    price: str = "1.00",
    labels: list[str] | None = None,
    scores: list[int] | None = None,
    meta: str = '{"team": "core"}',
    active: str = "[2024-01-01,2024-02-01)",
    weight: str = "[1,10)",
    peer: str = "10.1.1.1",
    payload: bytes | None = b"\x00new",
    effort: datetime.timedelta | None = datetime.timedelta(minutes=30),
    owner_id: int | None = 1,
) -> list:
    """The parameters of :data:`INSERT_TICKET` for one row."""
    return [
        id,
        uuid.UUID(code),
        title,
        status,
        priority,
        price,
        effort,
        datetime.datetime(2024, 6, 1, 12, 0, tzinfo=UTC),
        labels if labels is not None else ["new"],
        scores if scores is not None else [1],
        meta,
        active,
        weight,
        peer,
        payload,
        title,
        owner_id,
    ]


def insert_ticket(cursor, **kwargs) -> int:
    """Insert one ticket and return the id postgres returned."""
    cursor.execute(INSERT_TICKET, ticket_row(**kwargs))
    return cursor.fetchone()[0]
