"""A fixed clock for Django's own migration bookkeeping.

``django_migrations.applied`` holds the moment a migration was applied — the current
time of the run that applied it.  Two consequences made captures unstable:

* the migration phase captures the executor's read of ``django_migrations``, so a phase
  that is recorded again (which happens in any session where *something* runs against
  the database: a test without captures, an unstable test, a test excluded from capquery
  or one whose values cannot be stored) wrote a file with the timestamps of that run —
  a one-line diff in every merge request, for ever;
* a test that reads the table itself would see a value that is new in every run.

The dates of the migrations carry no information for a test suite, so capquery stores a
*deterministic* one instead: the first migration of a session is "applied" on
2000-01-01 00:00:00 UTC and every migration seen after it one second later.  The
assignment order is the order the captures retrieve the rows in, which is the order the
migrations were applied in, so the same migration is given the same moment in every
statement and in every run::

    applied_migrations                     id  app             name              applied
    first read of the phase  ->            1  contenttypes    0001_initial      2000-01-01 00:00:00+00:00
                                           2  auth            0001_initial      2000-01-01 00:00:01+00:00
    later read, same rows    ->            same values again

A test sees the same values as the replay of that test, because the normalization is
applied to the rows the caller receives as well as to the ones that are stored.
"""

from __future__ import annotations

import datetime
from typing import Any, Optional

from . import statements

__all__ = [
    "MIGRATION_EPOCH",
    "MIGRATION_STEP",
    "normalize_row",
    "normalize_rows",
    "reset",
    "synthetic_moment",
]

#: "Applied" moment of the first migration of a session, and the step after it.
MIGRATION_EPOCH = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
MIGRATION_STEP = datetime.timedelta(seconds=1)

#: The moment each migration of this process was given, and the order they got it in.
_ASSIGNED: dict[tuple[str, str], datetime.datetime] = {}


def reset() -> None:
    """Start a session from the epoch: the next migration gets the first moment."""
    _ASSIGNED.clear()


def synthetic_moment(index: int) -> datetime.datetime:
    """The "applied" moment of the ``index``-th migration of the session."""
    return MIGRATION_EPOCH + index * MIGRATION_STEP


def normalize_rows(sql: str, columns: Any, rows: Any) -> Any:
    """The rows of a read of the migration bookkeeping with a fixed ``applied`` column.

    Anything else is handed back untouched: only a read of the migrations table that
    carries an ``applied`` column of datetimes is normalized.
    """
    index = _column_index(columns, "applied")
    if index is None or not rows or not _reads_migrations(sql):
        return rows
    app = _column_index(columns, "app")
    name = _column_index(columns, "name")
    return [
        normalize_row(row, index=index, key=_key(row, position, app, name))
        for position, row in enumerate(rows)
    ]


def normalize_row(row: Any, *, index: int, key: tuple[str, str]) -> Any:
    """One row, with a synthetic moment where it holds the time of the run."""
    values = list(row)
    if 0 <= index < len(values) and isinstance(values[index], datetime.datetime):
        values[index] = _moment_for(key)
    return tuple(values) if isinstance(row, tuple) else values


def _moment_for(key: tuple[str, str]) -> datetime.datetime:
    """The moment of a migration, assigned in the order the migrations are first seen."""
    moment = _ASSIGNED.get(key)
    if moment is None:
        moment = synthetic_moment(len(_ASSIGNED))
        _ASSIGNED[key] = moment
    return moment


def _key(row: Any, index: int, app: Optional[int], name: Optional[int]) -> tuple[str, str]:
    """What makes a row a migration: its ``(app, name)`` when the read carries them."""
    if app is not None and name is not None and app < len(row) and name < len(row):
        return (str(row[app]), str(row[name]))
    return ("#row", str(index))


def _column_index(columns: Any, wanted: str) -> Optional[int]:
    for position, column in enumerate(columns or ()):
        if str(column).lower() == wanted:
            return position
    return None


def _reads_migrations(sql: str) -> bool:
    if not isinstance(sql, str) or statements.leading_keyword(sql) not in ("select", "with"):
        return False
    lowered = sql.lower()
    return any(table in lowered for table in statements.system_tables())
