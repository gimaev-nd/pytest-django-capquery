"""Which statements are captured and which ones always go to postgres.

capquery captures **every data statement** of a test, reads and writes alike: a
``SELECT``, an ``INSERT ... RETURNING id``, an ``UPDATE`` with its row count or a
``DELETE``.  Replaying a write means the test no longer needs a database at all,
which is the point of the plugin.

Two kinds of statements are never captured, they are always executed for real and
they never consume a capture entry:

* **schema DDL** — ``CREATE``/``ALTER``/``DROP TABLE`` and friends.  The test
  database is still created by Django and still has the schema of the project, so
  a query that cannot be answered from the captures (a new or edited test) still
  hits a sane database instead of an empty one;
* **everything else that is not a data statement** — transaction control
  (``SAVEPOINT``, ``RELEASE``, ``SET CONSTRAINTS``), maintenance (``VACUUM``,
  ``ANALYZE``), administration and anything capquery does not recognise.  Being
  conservative here is deliberate: an unknown statement is never replayed, so it
  cannot silently lose its side effect.

Django's own migration bookkeeping (``django_migrations``) is in the second group
even though it is an ordinary ``INSERT``: it records when a migration was applied
*now*, so its parameter changes on every run and it could never be replayed.  It
is written for real, its ``SELECT``s are still replayed like any other read.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "DATA",
    "SYSTEM",
    "classify",
    "is_data_statement",
    "leading_keyword",
    "system_tables",
]

#: Captured and replayed.
DATA = "data"
#: Always executed against postgres, never captured.
SYSTEM = "system"

#: Comments and whitespace may precede the keyword, and a statement may be wrapped
#: in parentheses (Django does that for unions).
_LEADING_RE = re.compile(
    r"^\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*\(*\s*([A-Za-z_][A-Za-z_0-9]*)",
    re.DOTALL,
)

#: A read the *driver* issues to resolve postgres type oids (``psycopg`` asks for the
#: oids of hstore/citext/… once per process).  Whether it appears does not depend on the
#: project but on whether the process already resolved that type, so caching one would
#: make the capture of a phase depend on the process that recorded it: the phase
#: re-recorded inside a session (a regeneration) would not issue them, the next session
#: started cold would, and the phase would be redone for real for ever.  They are
#: metadata reads of a handful of catalogue rows, so they always go to postgres.
_DRIVER_TYPE_LOOKUP_RE = re.compile(r"(?=.*\bpg_type\b)(?=.*\btyparray\b)", re.IGNORECASE | re.DOTALL)

#: The statement kinds whose result capquery stores and replays.
_DATA_KEYWORDS = frozenset(
    {
        "select",
        "values",
        "table",
        "with",  # CTEs: WITH ... SELECT / INSERT / UPDATE / DELETE
        "insert",
        "update",
        "delete",
        "merge",
        "truncate",
        "fetch",  # FETCH FIRST n ROWS (a cursor continuation)
    }
)


def leading_keyword(sql: str) -> str:
    """First keyword of a statement, lowercased (``""`` when there is none)."""
    if not isinstance(sql, str):
        return ""
    match = _LEADING_RE.match(sql)
    return match.group(1).lower() if match else ""


def classify(sql: str) -> str:
    """Return :data:`DATA` for a capturable statement, :data:`SYSTEM` otherwise."""
    keyword = leading_keyword(sql)
    if keyword not in _DATA_KEYWORDS:
        return SYSTEM
    if ";" in sql.rstrip().rstrip(";"):
        # more than one statement in one string: the second one may be anything, and
        # capquery only ever looks at the first keyword
        return SYSTEM
    if keyword in ("select", "with") and _DRIVER_TYPE_LOOKUP_RE.search(sql):
        # the driver resolving postgres type oids: the statement belongs to the
        # connection, not to the project (see _DRIVER_TYPE_LOOKUP_RE)
        return SYSTEM
    target = _write_target(sql)
    if target is not None and target in system_tables():
        return SYSTEM
    return DATA


def is_data_statement(sql: str) -> bool:
    """True when the statement may be captured and replayed."""
    return classify(sql) == DATA


_WRITE_TARGET_RE = re.compile(
    r'^\s*(?:insert\s+into|update|delete\s+from)\s+(?:only\s+)?"?([A-Za-z_][A-Za-z_0-9.]*)"?',
    re.IGNORECASE,
)


def _write_target(sql: str) -> Optional[str]:
    """Table a write statement touches, or None when it is not a plain write."""
    match = _WRITE_TARGET_RE.match(sql)
    if match is None:
        return None
    return match.group(1).split(".")[-1].strip('"').lower()


def system_tables() -> frozenset:
    """Tables capquery never captures writes of.

    ``django_migrations`` is postgres-side bookkeeping of the migration executor:
    every applied migration adds a row with the current timestamp, so the very same
    ``INSERT`` has different parameters on every run.  Recording it would only
    guarantee a miss on the next replay, so it is executed for real instead.  Reads of
    those tables *are* captured, with a fixed clock in their ``applied`` column
    (:mod:`capquery.migration_time`).
    """
    global _SYSTEM_TABLES
    if _SYSTEM_TABLES is None:
        tables = {"django_migrations"}
        try:  # pragma: no cover - depends on Django being importable
            from django.db.migrations.recorder import MigrationRecorder

            tables.add(MigrationRecorder.Migration._meta.db_table.lower())
        except Exception:
            pass
        _SYSTEM_TABLES = frozenset(tables)
    return _SYSTEM_TABLES


_SYSTEM_TABLES: Optional[frozenset] = None
