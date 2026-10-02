"""Reads the real database with a plain driver, outside Django and outside the plugin.

The plugin answers the tests from the captures, so whatever they "wrote" is not in
postgres.  This test is not managed by capquery (it does not use the ``db`` fixture) and
talks to postgres with psycopg directly, so it reports what really is in the table.

The expected number comes from the environment, because it depends on how the session
answered the migration phase: after a *replayed* phase the table holds nothing (the seeds
were answered from the captures), while a session that redid the phase for real has the
rows the seed migration writes.  ``run.py`` sets the value it expects.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from django.conf import settings


def test_the_real_database_only_holds_what_the_migrations_wrote(django_db_setup):
    expected = os.environ.get("CAPQUERY_VERIFY_EXPECT_TICKETS")
    if expected is None:
        pytest.skip("CAPQUERY_VERIFY_EXPECT_TICKETS is not set")
    allowed = {int(value) for value in expected.split(",")}
    config = settings.DATABASES["default"]
    with psycopg.connect(
        dbname=config["NAME"],
        host=config["HOST"] or None,
        port=config["PORT"] or None,
        user=config["USER"],
        password=config["PASSWORD"] or None,
    ) as raw:
        tickets = raw.execute("SELECT count(*) FROM shop_ticket").fetchone()[0]
    assert tickets in allowed, f"postgres holds {tickets} ticket(s), expected one of {sorted(allowed)}"
