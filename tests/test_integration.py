"""End to end tests: a real Django suite runs twice against a real postgres.

Run 1 records the captures, run 2 replays them.  The assertions on the numbers
come from the ``capquery:`` lines the plugin prints in the terminal summary.

``-k "not ignored"`` deselects the one test of the demo that is marked
``capquery_ignore``: such a test always runs against postgres, so its presence
keeps the migration phase real (that behaviour has its own test below).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from capquery.paths import state_file
from capquery.yaml_io import read_capture, write_capture
from helpers import run_demo, summarize

ORDERS_COUNT = "tests/captures/test_orders.py/test_orders_count.yaml"
SEQUENCE_TEST = "tests/captures/test_orders.py/test_sequence_starts_from_the_seed.yaml"
ENUM_CAPTURE = "tests/captures/test_orders.py/test_an_enum_parameter_is_captured.yaml"
VARIANT_CAPTURE = "tests/captures/test_variant.py/test_variant.yaml"

NO_IGNORED = ("-k", "not ignored")

VARIANT_TEST = '''
import decimal
import os

from demo.shop.models import Order


def test_variant(db):
    threshold = decimal.Decimal(os.environ.get("CAPQUERY_VARIANT", "10"))
    assert Order.objects.filter(amount__gte=threshold).count() >= 1
'''

#: one more query for an existing test: its captures no longer answer it
CHANGED_QUERY_FROM = 'def test_orders_count(db):\n    assert Order.objects.count() == 3\n'
CHANGED_QUERY_TO = (
    'def test_orders_count(db):\n    assert Order.objects.count() == 3\n'
    '    assert Order.objects.filter(name="first").count() == 1\n'
)


def captures_of(root: Path) -> dict[str, str]:
    root = Path(root) / "tests" / "captures"
    return {str(path.relative_to(root)): path.read_text(encoding="utf-8") for path in root.rglob("*.yaml")}


def patch_orders_count(demo: Path) -> None:
    test_file = Path(demo) / "tests" / "test_orders.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8").replace(CHANGED_QUERY_FROM, CHANGED_QUERY_TO),
        encoding="utf-8",
    )


def test_the_second_run_replays_and_writes_nothing(pytester, demo, demo_env):
    first = run_demo(pytester)
    assert first.ret == 0, first.stdout
    summary = summarize(first)
    # one capture per test that queries the database: 16 managed tests, one of them
    # (test_without_queries) never queries anything
    assert summary.created == 15, str(summary)
    assert summary.replayed == 0
    assert summary.missed == 0
    assert summary.executed_postgres > 40, str(summary)
    assert (Path(demo) / ORDERS_COUNT).exists()
    assert (Path(demo) / "captures" / "migrations.yaml").exists()
    # a test marked capquery_ignore is not captured, a test without queries gets no file
    assert not (Path(demo) / "tests/captures/test_orders.py/test_ignored_is_not_captured.yaml").exists()
    assert not (Path(demo) / "tests/captures/test_orders.py/test_without_queries.yaml").exists()
    # writes are captured too: the test that only inserts has a capture of its INSERT
    sequence_capture = (Path(demo) / SEQUENCE_TEST).read_text(encoding="utf-8")
    assert 'INSERT INTO "shop_order"' in sequence_capture
    assert "rowcount: 1" in sequence_capture
    # a query parameter that is a subclass of str (Django's TextChoices) is stored as
    # the value postgres got, not as the object itself
    enum_capture = (Path(demo) / ENUM_CAPTURE).read_text(encoding="utf-8")
    enum_payload = yaml.safe_load(enum_capture)
    assert any(capture["params"] == ["first"] for capture in enum_payload["captures"])
    assert ["str"] in enum_payload["schemas"].values()
    assert "OrderName" not in enum_capture
    recorded = captures_of(demo)
    migrations = (Path(demo) / "captures" / "migrations.yaml").read_text(encoding="utf-8")

    second = run_demo(pytester, *NO_IGNORED)
    assert second.ret == 0, second.stdout
    summary = summarize(second)
    assert summary.replayed > 40, str(summary)
    assert summary.missed == 0, str(summary)
    # not a single statement of the tests reached postgres: reads and writes alike
    # came from the captures
    assert summary.executed_postgres == 0, str(summary)
    assert summary.migrations_replayed > 0, str(summary)
    assert summary.migrations_missed == 0, str(summary)
    assert summary.retried == []
    assert summary.deferred == []
    # not a single capture file changed
    assert captures_of(demo) == recorded
    assert (Path(demo) / "captures" / "migrations.yaml").read_text(encoding="utf-8") == migrations


def test_a_replayed_setup_never_applies_the_migration_writes(
    pytester, demo, demo_env, monkeypatch
):
    """The seeded rows exist for the tests, but the database never got them.

    The demo suite carries a test that reads the table with a plain driver instead of
    Django: capquery never intercepts it, so it reports what really is in postgres.
    On the replayed run it expects an empty table while the Django tests keep seeing
    the three rows the seed migration inserted — those came from the captures.
    """
    demo_env("capquery_writes")
    monkeypatch.setenv("CAPQUERY_EXPECT_RAW_ROWS", "3")
    first = run_demo(pytester, *NO_IGNORED)
    assert first.ret == 0, first.stdout
    assert summarize(first).executed_postgres > 40, str(summarize(first))

    monkeypatch.setenv("CAPQUERY_EXPECT_RAW_ROWS", "0")
    second = run_demo(pytester, *NO_IGNORED)
    assert second.ret == 0, second.stdout
    summary = summarize(second)
    assert summary.replayed > 40, str(summary)
    assert summary.missed == 0, str(summary)
    assert summary.executed_postgres == 0, str(summary)
    assert summary.migrations_replayed > 0, str(summary)
    assert summary.migrations_missed == 0, str(summary)


def test_an_ignored_test_keeps_the_migration_phase_real(pytester, demo, demo_env):
    database = demo_env("capquery_ignored")
    assert run_demo(pytester).ret == 0

    second = run_demo(pytester)
    assert second.ret == 0, second.stdout
    summary = summarize(second)
    # the ignored test queries postgres, so it needs the rows the migrations write
    assert summary.migrations_replayed == 0, str(summary)
    assert "executed for real" in summary.migrations_line, summary.migrations_line
    assert summary.missed == 0, str(summary)
    assert database


def test_replay_answers_from_the_captures_even_if_postgres_disagrees(
    pytester, demo, demo_env, connect
):
    """The rows are changed behind the back of the recorded run, the replayed run still passes."""
    database = demo_env("capquery_changed")
    assert run_demo(pytester, "--reuse-db", *NO_IGNORED).ret == 0
    with connect(f"test_{database}") as connection:
        connection.execute("UPDATE shop_order SET name = 'CHANGED'")
        # move the sequence forward: only the sqlsequencereset of the plugin can undo this
        connection.execute("SELECT nextval('shop_order_id_seq')")
        connection.execute("SELECT nextval('shop_order_id_seq')")

    second = run_demo(pytester, "--reuse-db", *NO_IGNORED)
    assert second.ret == 0, second.stdout
    summary = summarize(second)
    assert summary.missed == 0, str(summary)
    assert summary.executed_postgres == 0, str(summary)
    assert summary.replayed > 40, str(summary)
    # the database is reused, so the migration phase is not replayed
    assert summary.migrations_replayed == 0


def test_a_changed_query_regenerates_the_captures(pytester, demo, demo_env):
    database = demo_env("capquery_rule2")
    assert run_demo(pytester, "--reuse-db", *NO_IGNORED).ret == 0
    path = Path(demo) / ORDERS_COUNT
    before = path.read_text(encoding="utf-8")
    patch_orders_count(demo)

    second = run_demo(pytester, "--reuse-db", *NO_IGNORED)
    assert second.ret == 0, second.stdout
    summary = summarize(second)
    assert summary.retried, str(summary)
    assert summary.updated >= 1
    assert path.read_text(encoding="utf-8") != before
    assert "name" in path.read_text(encoding="utf-8")

    third = run_demo(pytester, "--reuse-db", *NO_IGNORED)
    assert third.ret == 0
    assert summarize(third).missed == 0
    assert summarize(third).retried == []
    assert database  # the fixture configured a database


def test_a_regeneration_redoes_the_phase_when_it_came_from_the_captures(pytester, demo, demo_env):
    """A changed query in a fully replayed session: the phase is redone for real first."""
    demo_env("capquery_redo")
    assert run_demo(pytester, *NO_IGNORED).ret == 0
    path = Path(demo) / ORDERS_COUNT
    before = path.read_text(encoding="utf-8")
    patch_orders_count(demo)

    second = run_demo(pytester, *NO_IGNORED)
    assert second.ret == 0, second.stdout
    summary = summarize(second)
    assert summary.missed > 0, str(summary)
    assert summary.retried, str(summary)
    assert summary.updated >= 1
    assert path.read_text(encoding="utf-8") != before
    assert summary.deferred == [], str(summary)

    # the captures were recorded against a database that really got the seeds, so the
    # next run replays all of it again
    third = run_demo(pytester, *NO_IGNORED)
    assert third.ret == 0, third.stdout
    summary = summarize(third)
    assert summary.missed == 0, str(summary)
    assert summary.executed_postgres == 0, str(summary)
    assert summary.retried == []
    assert summary.migrations_missed == 0, str(summary)


def test_a_broken_capture_fails_the_test_then_regenerates_it(pytester, demo, demo_env):
    demo_env("capquery_rule3")
    assert run_demo(pytester, "--reuse-db", *NO_IGNORED).ret == 0
    path = Path(demo) / ORDERS_COUNT
    records = read_capture(path)
    counter = next(record for record in records if "COUNT" in record.sql.upper())
    counter.rows = [[{"t": "int", "v": 999}]]
    write_capture(path, records)
    assert "999" in path.read_text(encoding="utf-8")

    second = run_demo(pytester, "--reuse-db", *NO_IGNORED)
    assert second.ret == 0, second.stdout
    summary = summarize(second)
    assert any("test_orders_count" in nodeid for nodeid in summary.retried), str(summary)
    assert summary.updated >= 1
    assert "999" not in path.read_text(encoding="utf-8")

    third = run_demo(pytester, "--reuse-db", *NO_IGNORED)
    assert third.ret == 0
    assert summarize(third).missed == 0
    assert summarize(third).retried == []


def test_a_test_that_never_settles_loses_its_captures(pytester, demo, demo_env, monkeypatch):
    demo_env("capquery_unstable")
    (Path(demo) / "tests" / "test_variant.py").write_text(VARIANT_TEST, encoding="utf-8")
    args = (
        "tests/test_variant.py",
        "--capquery-min-tests=0",
        "--capquery-max-attempts=3",
        "--reuse-db",
    )
    capture = Path(demo) / VARIANT_CAPTURE

    monkeypatch.setenv("CAPQUERY_VARIANT", "10")
    assert run_demo(pytester, *args).ret == 0
    assert capture.exists()

    for variant in ("20", "30", "25"):
        monkeypatch.setenv("CAPQUERY_VARIANT", variant)
        result = run_demo(pytester, *args)
        assert result.ret == 0, result.stdout
        summary = summarize(result)
        assert summary.missed > 0, str(summary)
        assert summary.retried, str(summary)

    state = yaml.safe_load(state_file(Path(demo)).read_text(encoding="utf-8"))
    assert state["tests/test_variant.py::test_variant"]["unstable"] is True

    monkeypatch.setenv("CAPQUERY_VARIANT", "10")
    last = run_demo(pytester, *args)
    assert last.ret == 0, last.stdout
    summary = summarize(last)
    assert summary.unstable, str(summary)
    assert not capture.exists()
    assert summary.retried == []


def test_disabled_below_the_threshold(pytester, demo, demo_env):
    result = run_demo(pytester, "-k", "test_orders_count")
    assert result.ret == 0
    summary = summarize(result)
    assert summary.disabled is not None, str(summary)
    assert "1 database test" in summary.disabled
    assert not (Path(demo) / "tests" / "captures").exists()


def test_xdist_is_refused(pytester, demo, demo_env):
    pytest.importorskip("xdist")
    result = run_demo(pytester, "-n", "2")
    assert result.ret == 0
    summary = summarize(result)
    assert summary.disabled is not None, str(summary)
    assert "xdist" in summary.disabled
    assert not (Path(demo) / "tests" / "captures").exists()
