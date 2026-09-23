"""The save path: a capture that cannot be written must not break the session.

A ``RepresenterError`` raised out of ``pytest_runtest_protocol`` aborts the whole
run — every test after the failing one is lost, so the plugin reports a value it
cannot store and keeps the captures it already has instead.  Both places that write
a capture file are covered: the save of one test and the save of the migration
phase.
"""

from __future__ import annotations

import enum
from types import SimpleNamespace

from capquery.interceptor import RECORD, CaptureContext
from capquery.plugin import CapQueryPlugin
from capquery.records import Record
from capquery.store import CaptureStore
from capquery.yaml_io import write_capture


class _Status(str, enum.Enum):
    """A ``str`` subclass: the shape Django's ``models.TextChoices`` has."""

    FIRST = "first"


class _Item:
    """The parts of a pytest item the save path touches."""

    nodeid = "tests/test_orders.py::test_orders_count"


class _Config:
    """The parts of a pytest config ``CapQueryPlugin`` reads."""

    def __init__(self, rootpath):
        self.rootpath = rootpath
        self.option = SimpleNamespace()

    def getoption(self, name, default=None):
        return default

    def getini(self, name):
        return "5"


def _plugin(tmp_path) -> CapQueryPlugin:
    return CapQueryPlugin(_Config(tmp_path))


def _records(params: list) -> list[Record]:
    return [
        Record(
            hash="9f2c",
            n=0,
            sql="SELECT id FROM shop_order WHERE name = %s",
            params=params,
            rowcount=1,
            columns=["id"],
            rows=[[{"t": "int", "v": 1}]],
        )
    ]


def _context(path, records: list[Record]) -> CaptureContext:
    ctx = CaptureContext(key=str(path), mode=RECORD, store=CaptureStore())
    ctx.recorded.extend(records)
    return ctx


def test_an_unstorable_capture_is_reported_instead_of_aborting_the_run(tmp_path):
    plugin = _plugin(tmp_path)
    path = tmp_path / "tests" / "captures" / "test_orders.py" / "test_orders_count.yaml"
    unstorable = _records([{"t": "str", "v": _Status.FIRST}])

    plugin._save(_Item(), path, _context(path, unstorable))  # must not raise

    assert not path.exists()
    assert plugin.unsupported == [_Item.nodeid]
    assert any("cannot be stored" in message for message in plugin.warnings)


def test_an_unstorable_capture_keeps_the_file_and_the_store_of_the_test(tmp_path):
    plugin = _plugin(tmp_path)
    path = tmp_path / "capture.yaml"
    write_capture(path, _records([{"t": "str", "v": "first"}]))
    plugin.store.add_records(str(path), _records([{"t": "str", "v": "first"}]))
    before = path.read_text(encoding="utf-8")

    plugin._save(_Item(), path, _context(path, _records([{"t": "str", "v": _Status.FIRST}])))

    assert path.read_text(encoding="utf-8") == before
    assert plugin.counters["updated"] == 0
    assert plugin.store.known_context(str(path))


def test_a_capture_that_can_be_written_is_still_saved(tmp_path):
    plugin = _plugin(tmp_path)
    path = tmp_path / "capture.yaml"

    plugin._save(_Item(), path, _context(path, _records([{"t": "str", "v": "first"}])))

    assert path.exists()
    assert plugin.counters["created"] == 1
    assert plugin.unsupported == []
    assert plugin.warnings == []
    assert plugin.store.known_context(str(path))


class _Hook:
    """The parts of ``item.ihook`` the retry loop calls."""

    def pytest_runtest_logstart(self, **kwargs):
        pass

    def pytest_runtest_logreport(self, **kwargs):
        pass

    def pytest_runtest_logfinish(self, **kwargs):
        pass


def test_an_uncapturable_test_is_reported_instead_of_being_skipped_silently(tmp_path, monkeypatch):
    """A context that saw a value capquery cannot store is reported.

    The decision ``unsupported`` has to reach ``_note_unsupported``: it is what puts the
    test into the terminal summary ("was not captured: it holds values capquery cannot
    store").  It used to fall through every branch of the retry loop, so the test simply
    never got a capture file and nothing in the output said why.
    """
    plugin = _plugin(tmp_path)
    plugin.enabled = True
    path = tmp_path / "tests" / "captures" / "test_orders.py" / "test_orders_count.yaml"
    plugin.capture_paths[_Item.nodeid] = path
    ctx = CaptureContext(key=str(path), mode=RECORD, store=CaptureStore())
    ctx.note_unsupported("capquery cannot store values of type psycopg.types.json.Jsonb")

    monkeypatch.setattr(plugin, "_prepare", lambda item, path, attempt: ctx)
    monkeypatch.setattr("_pytest.runner.runtestprotocol", lambda item, log, nextitem: [])

    item = SimpleNamespace(
        nodeid=_Item.nodeid,
        location=("tests/test_orders.py", 0, "test_orders_count"),
        ihook=_Hook(),
    )
    plugin._run_with_retries(item, None)

    assert plugin.unsupported == [_Item.nodeid]
    assert any("cannot store" in message for message in plugin.warnings)
    assert not path.exists()


class _Hook:
    """The parts of ``item.ihook`` the retry loop calls."""

    def pytest_runtest_logstart(self, **kwargs):
        pass

    def pytest_runtest_logreport(self, **kwargs):
        pass

    def pytest_runtest_logfinish(self, **kwargs):
        pass


def test_an_unstorable_migration_capture_is_reported_instead_of_failing_the_setup(tmp_path):
    """The migration phase is saved from a session fixture, so an exception there
    fails every test of the session rather than one."""
    plugin = _plugin(tmp_path)
    plugin.migrations._collected = _records([{"t": "str", "v": _Status.FIRST}])
    plugin.migrations.recorded_count = 1

    plugin.migrations.finish_phase()  # must not raise

    assert not plugin.migrations_path.exists()
    assert any("migration phase was not captured" in message for message in plugin.warnings)
