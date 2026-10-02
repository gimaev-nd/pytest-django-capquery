"""Which statements capquery captures and which ones always go to postgres."""

from __future__ import annotations

import pytest

from capquery.statements import DATA, SYSTEM, classify, is_data_statement, leading_keyword

DATA_STATEMENTS = [
    "SELECT 1",
    "select id from shop_order",
    '  \n\tSELECT "shop_order"."id" FROM "shop_order"',
    "-- a comment\nSELECT 1",
    "/* a comment */ SELECT 1",
    "(SELECT 1) UNION (SELECT 2)",
    'WITH rows AS (SELECT 1) SELECT * FROM rows',
    'INSERT INTO "shop_order" ("name") VALUES (%s) RETURNING "id"',
    "INSERT INTO shop_order (name) VALUES (%s) ON CONFLICT DO NOTHING",
    'UPDATE "shop_order" SET "name" = %s WHERE "id" = %s',
    'DELETE FROM "shop_order" WHERE "id" = %s',
    'TRUNCATE "shop_order"',
    "VALUES (1), (2)",
    "TABLE shop_order",
]

SYSTEM_STATEMENTS = [
    'CREATE TABLE "shop_order" ("id" serial PRIMARY KEY)',
    'CREATE DATABASE "test_demo"',
    'DROP TABLE "shop_order"',
    'ALTER TABLE "shop_order" ADD COLUMN "note" varchar(10)',
    'CREATE INDEX "shop_order_name" ON "shop_order" ("name")',
    "COMMENT ON TABLE shop_order IS 'x'",
    "SET CONSTRAINTS ALL IMMEDIATE",
    "SET CONSTRAINTS ALL DEFERRED",
    "SAVEPOINT s1",
    "RELEASE SAVEPOINT s1",
    "REVOKE ALL ON shop_order FROM PUBLIC",
    "GRANT SELECT ON shop_order TO PUBLIC",
    "VACUUM shop_order",
    "ANALYZE shop_order",
    "LISTEN capquery",
    "EXPLAIN SELECT 1",
    "DO $$ BEGIN END $$",
    "COPY shop_order FROM STDIN",
    "SELECT 1; DROP TABLE shop_order",
    "",
]


@pytest.mark.parametrize("sql", DATA_STATEMENTS)
def test_data_statements_are_capturable(sql):
    assert classify(sql) == DATA
    assert is_data_statement(sql) is True


@pytest.mark.parametrize("sql", SYSTEM_STATEMENTS)
def test_everything_else_is_sent_to_postgres(sql):
    assert classify(sql) == SYSTEM
    assert is_data_statement(sql) is False


def test_reads_and_writes_of_the_migration_table_are_treated_differently():
    """Django's migration bookkeeping is written for real, but read from captures."""
    assert classify("SELECT * FROM django_migrations") == DATA
    assert (
        classify('INSERT INTO "django_migrations" ("app", "name") VALUES (%s, %s)') == SYSTEM
    )
    assert classify('UPDATE "django_migrations" SET "app" = %s') == SYSTEM
    assert classify('DELETE FROM "django_migrations" WHERE "app" = %s') == SYSTEM
    # a table that merely ends with the same name is a different table
    assert classify('INSERT INTO "my_django_migrations" ("app") VALUES (%s)') == DATA


def test_leading_keyword_skips_comments_and_parentheses():
    assert leading_keyword("-- x\n  ( SELECT 1") == "select"
    assert leading_keyword("\n\n") == ""
    assert leading_keyword(1234) == ""


def test_the_driver_resolving_type_oids_is_never_cached():
    """psycopg asks for the oid of hstore/citext once per process.

    Whether that statement is issued depends on the *process* (a session that already
    resolved the type does not ask again), so caching it would make the capture of a
    migration phase depend on who recorded it and the phase would be redone for real on
    every run.
    """
    assert classify("SELECT oid, typarray FROM pg_type WHERE typname = %s") == SYSTEM
    assert (
        classify(
            "SELECT t.typname AS name, t.oid AS oid, t.typarray AS array_oid,"
            " t.oid::regtype::text AS regtype FROM pg_type t WHERE t.oid = %s"
        )
        == SYSTEM
    )
    # a read of the catalogue the project itself wrote is still a data statement
    assert classify("SELECT typname FROM pg_type") == DATA
    assert classify("SELECT oid FROM pg_class WHERE relname = %s") == DATA
