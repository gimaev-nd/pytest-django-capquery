"""The fixed clock of Django's migration bookkeeping.

``django_migrations.applied`` is the moment a run applied a migration, so a capture that
holds it is rewritten on every run that records the phase again.  capquery stores a
deterministic moment instead, and hands the same one to the caller of the recording run,
so a test and its replay see the very same values.
"""

from __future__ import annotations

import datetime

import pytest

from capquery import migration_time
from capquery.migration_time import MIGRATION_EPOCH, normalize_rows, synthetic_moment

SQL = 'SELECT "django_migrations"."id", "app", "name", "applied" FROM "django_migrations"'
COLUMNS = ["id", "app", "name", "applied"]
UTC = datetime.timezone.utc


def row(number: int, app: str, name: str, applied: datetime.datetime):
    return (number, app, name, applied)


@pytest.fixture(autouse=True)
def _session_clock():
    migration_time.reset()
    yield
    migration_time.reset()


def test_the_first_migration_of_the_session_is_applied_in_2000():
    rows = [row(1, "contenttypes", "0001_initial", datetime.datetime(2026, 9, 22, tzinfo=UTC))]

    [normalized] = normalize_rows(SQL, COLUMNS, rows)

    assert normalized[3] == MIGRATION_EPOCH == datetime.datetime(2000, 1, 1, tzinfo=UTC)
    assert synthetic_moment(0) == MIGRATION_EPOCH


def test_every_following_migration_is_one_second_later():
    rows = [
        row(1, "contenttypes", "0001_initial", datetime.datetime(2026, 9, 22, tzinfo=UTC)),
        row(2, "auth", "0001_initial", datetime.datetime(2026, 9, 22, tzinfo=UTC)),
        row(3, "auth", "0002_alter_permission_name_max_length", datetime.datetime(2026, 9, 22, tzinfo=UTC)),
    ]

    normalized = normalize_rows(SQL, COLUMNS, rows)

    assert [values[3] for values in normalized] == [
        MIGRATION_EPOCH,
        MIGRATION_EPOCH + datetime.timedelta(seconds=1),
        MIGRATION_EPOCH + datetime.timedelta(seconds=2),
    ]


def test_the_same_migration_keeps_its_moment_in_every_read():
    """The executor reads the table again after each migration it applies."""
    first_read = normalize_rows(
        SQL, COLUMNS, [row(1, "contenttypes", "0001_initial", datetime.datetime(2026, 1, 1, tzinfo=UTC))]
    )
    second_read = normalize_rows(
        SQL,
        COLUMNS,
        [
            row(1, "contenttypes", "0001_initial", datetime.datetime(2026, 2, 2, tzinfo=UTC)),
            row(2, "auth", "0001_initial", datetime.datetime(2026, 2, 2, tzinfo=UTC)),
        ],
    )

    assert first_read[0][3] == second_read[0][3]
    assert second_read[1][3] == MIGRATION_EPOCH + datetime.timedelta(seconds=1)


def test_two_sessions_assign_the_same_moments():
    """The value may not depend on the time of the run, only on the order of the rows."""
    rows = [
        row(1, "contenttypes", "0001_initial", datetime.datetime(2026, 9, 22, 5, 0, tzinfo=UTC)),
        row(2, "auth", "0001_initial", datetime.datetime(2026, 9, 22, 5, 1, tzinfo=UTC)),
    ]
    first = [values[3] for values in normalize_rows(SQL, COLUMNS, rows)]

    migration_time.reset()
    second = [values[3] for values in normalize_rows(SQL, COLUMNS, rows)]

    assert first == second
    assert all(moment.year == 2000 for moment in first)


def test_the_row_type_and_the_other_columns_are_kept():
    rows = [row(1, "contenttypes", "0001_initial", datetime.datetime(2026, 9, 22, tzinfo=UTC))]

    [normalized] = normalize_rows(SQL, COLUMNS, rows)

    assert isinstance(normalized, tuple)
    assert normalized[:3] == (1, "contenttypes", "0001_initial")
    assert rows[0][3].year == 2026  # the row the driver handed over is not modified


def test_a_read_without_the_applied_column_is_left_alone():
    rows = [(1, "contenttypes", "0001_initial")]
    assert normalize_rows(SQL, ["id", "app", "name"], rows) is rows


def test_a_statement_that_is_not_a_read_of_the_migrations_is_left_alone():
    applied = datetime.datetime(2026, 9, 22, tzinfo=UTC)
    rows = [row(1, "shop", "0001_initial", applied)]

    assert normalize_rows("SELECT id, app, name, applied FROM shop_pipeline", COLUMNS, rows) is rows
    assert normalize_rows('INSERT INTO "django_migrations" (app, name, applied) VALUES (%s, %s, %s)',
                          COLUMNS, rows) is rows


def test_a_null_or_non_datetime_applied_is_left_as_it_is():
    rows = [(1, "contenttypes", "0001_initial", None), (2, "auth", "0001_initial", "2026-09-22")]

    normalized = normalize_rows(SQL, COLUMNS, rows)

    assert normalized[0][3] is None
    assert normalized[1][3] == "2026-09-22"


def test_an_empty_result_is_left_alone():
    rows: list = []
    assert normalize_rows(SQL, COLUMNS, rows) is rows


def test_a_read_without_app_and_name_falls_back_to_the_position():
    rows = [
        (datetime.datetime(2026, 9, 22, tzinfo=UTC), 1),
        (datetime.datetime(2026, 9, 22, tzinfo=UTC), 2),
    ]

    normalized = normalize_rows('SELECT applied, id FROM "django_migrations"', ["applied", "id"], rows)

    assert [values[0] for values in normalized] == [
        MIGRATION_EPOCH,
        MIGRATION_EPOCH + datetime.timedelta(seconds=1),
    ]
