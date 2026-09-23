"""Shared fixtures for the capquery test suite.

The integration tests and the benchmark drive a small Django project
(``tests/demo_project``) through a real pytest run in a subprocess, against a
real postgres.  When the ``pgserver`` wheel is available an embedded postgres is
started automatically; otherwise the connection settings have to be provided
through environment variables.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytest_plugins = ("pytester",)

#: the demo project is a Django project for the integration tests, not part of
#: this suite: it has its own pytest.ini and is copied into a temporary directory
collect_ignore = ["demo_project"]

HERE = Path(__file__).resolve().parent
DEMO_SOURCE = HERE / "demo_project"


@pytest.fixture(scope="session")
def postgres():
    """Connection settings of the postgres server used by the demo project."""
    external = os.environ.get("CAPQUERY_TEST_DB_HOST")
    if external:
        yield {
            "host": external,
            "port": os.environ.get("CAPQUERY_TEST_DB_PORT", ""),
            "user": os.environ.get("CAPQUERY_TEST_DB_USER", "postgres"),
            "password": os.environ.get("CAPQUERY_TEST_DB_PASSWORD", ""),
        }
        return

    pgserver = pytest.importorskip(
        "pgserver",
        reason="no postgres: install pgserver or set CAPQUERY_TEST_DB_HOST/PORT/USER/PASSWORD",
    )
    data_dir = Path(os.environ.get("CAPQUERY_TEST_DB_DIR", "/tmp/capquery-pgdata"))
    pgserver.get_server(str(data_dir), cleanup_mode=None)
    yield {"host": str(data_dir), "port": "", "user": "postgres", "password": ""}


@pytest.fixture(scope="session")
def postgres_admin(postgres):
    """A connection to the maintenance database, for creating and dropping test databases."""
    psycopg = pytest.importorskip("psycopg")
    connection = psycopg.connect(
        host=postgres["host"],
        port=postgres["port"] or None,
        user=postgres["user"],
        password=postgres["password"] or None,
        dbname="postgres",
        autocommit=True,
    )
    yield connection
    connection.close()


@pytest.fixture
def drop_test_database(postgres_admin):
    """Drop ``test_<name>`` databases so that every run starts from scratch."""

    def drop(database_name: str) -> None:
        postgres_admin.execute(f'DROP DATABASE IF EXISTS "test_{database_name}" WITH (FORCE)')

    yield drop


@pytest.fixture
def demo_env(postgres, drop_test_database, monkeypatch):
    """Point the demo project at the test server and a fresh database name."""

    def configure(database_name: str = "capquery_demo") -> str:
        drop_test_database(database_name)
        monkeypatch.setenv("CAPQUERY_DEMO_DB_HOST", postgres["host"])
        monkeypatch.setenv("CAPQUERY_DEMO_DB_PORT", postgres["port"])
        monkeypatch.setenv("CAPQUERY_DEMO_DB_USER", postgres["user"])
        monkeypatch.setenv("CAPQUERY_DEMO_DB_PASSWORD", postgres["password"])
        monkeypatch.setenv("CAPQUERY_DEMO_DB_NAME", database_name)
        return database_name

    configure()
    return configure


@pytest.fixture
def demo(pytester):
    """A copy of the demo Django project inside pytester's temporary directory."""
    for item in sorted(DEMO_SOURCE.iterdir()):
        if item.name in {"__pycache__", ".pytest_cache", "captures"}:
            continue
        target = pytester.path / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=shutil.ignore_patterns("__pycache__", "captures"))
        else:
            shutil.copy(item, target)
    return pytester.path


@pytest.fixture
def connect(postgres):
    """Open a direct psycopg connection to a database of the test server."""
    psycopg = pytest.importorskip("psycopg")

    def open_connection(database_name: str):
        return psycopg.connect(
            host=postgres["host"],
            port=postgres["port"] or None,
            user=postgres["user"],
            password=postgres["password"] or None,
            dbname=database_name,
            autocommit=True,
        )

    return open_connection
