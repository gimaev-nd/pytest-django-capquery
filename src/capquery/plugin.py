"""The pytest plugin: configuration, capture/replay orchestration, retries, report."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import pytest

from . import interceptor
from .interceptor import PASSTHROUGH, RECORD, REPLAY, CaptureContext
from .migrations import MigrationsPhase
from .paths import capture_file, migrations_file, state_file
from .state import StateFile
from .store import CaptureStore
from .yaml_io import CaptureFileError, read_capture, write_capture, delete_capture

DEFAULT_MIN_TESTS = 5
DEFAULT_MAX_ATTEMPTS = 3

#: Fixtures that mean "this test talks to the database".
DB_FIXTURES = frozenset({"db", "transactional_db"})

PLUGIN_NAME = "django-capquery-plugin"
IGNORE_MARKER = "capquery_ignore"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("django-capquery", "django-capquery: capture and replay SQL results")
    group.addoption(
        "--capquery-min-tests",
        dest="capquery_min_tests",
        action="store",
        default=None,
        metavar="N",
        help=(
            "capture and replay only when more than N database tests are collected "
            f"(default: {DEFAULT_MIN_TESTS}; env CAPQUERY_MIN_TESTS)"
        ),
    )
    group.addoption(
        "--capquery-max-attempts",
        dest="capquery_max_attempts",
        action="store",
        default=None,
        metavar="N",
        help=(
            "how many times a test may be regenerated before it is marked unstable "
            f"(default: {DEFAULT_MAX_ATTEMPTS}; env CAPQUERY_MAX_ATTEMPTS)"
        ),
    )
    parser.addini(
        "capquery_min_tests",
        "capture and replay only when more than this number of database tests is collected",
        default=str(DEFAULT_MIN_TESTS),
    )
    parser.addini(
        "capquery_max_attempts",
        "how many times a test may be regenerated before it is marked unstable",
        default=str(DEFAULT_MAX_ATTEMPTS),
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", f"{IGNORE_MARKER}: exclude this test from capquery capture and replay"
    )
    if config.pluginmanager.get_plugin(PLUGIN_NAME) is None:
        config.pluginmanager.register(CapQueryPlugin(config), PLUGIN_NAME)


def get_plugin(config: pytest.Config) -> Optional["CapQueryPlugin"]:
    plugin = config.pluginmanager.get_plugin(PLUGIN_NAME)
    return plugin if isinstance(plugin, CapQueryPlugin) else None


# --------------------------------------------------------------------------- #
# session fixture: the migration phase is finished (and redone if needed) here
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session", autouse=True)
def capquery_db_setup(request):
    """Runs right after pytest-django created the test databases.

    ``django_db_setup`` is requested dynamically so that a test suite without
    Django settings (this plugin's own unit tests, for instance) is not forced to
    configure Django.
    """
    plugin = get_plugin(request.config)
    if plugin is None or not plugin.enabled:
        yield
        return
    from pytest_django.lazy_django import django_settings_is_configured

    if not django_settings_is_configured():
        yield
        return
    request.getfixturevalue("django_db_setup")
    if plugin.migrations.needs_clean_rerun:
        blocker = request.getfixturevalue("django_db_blocker")
        with blocker.unblock():
            plugin.migrations.redo_without_cache()
    plugin.migrations.finish_phase()
    yield


def _is_db_test(item: pytest.Item) -> bool:
    return bool(DB_FIXTURES.intersection(item.fixturenames))


def _xdist_running(config: pytest.Config) -> bool:
    if hasattr(config, "workerinput"):
        return True
    return bool(getattr(config.option, "numprocesses", None))


def _resolve_int(config: pytest.Config, option: str, env: str, ini: str, default: int) -> int:
    value = config.getoption(option, default=None)
    if value is None:
        value = os.environ.get(env)
    if value is None:
        value = config.getini(ini)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise pytest.UsageError(f"capquery: invalid value for {option}/{env}/{ini}: {value!r}") from exc


class CapQueryPlugin:
    """State of one pytest session."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.rootdir = Path(str(config.rootpath))
        self.min_tests = _resolve_int(
            config, "capquery_min_tests", "CAPQUERY_MIN_TESTS", "capquery_min_tests", DEFAULT_MIN_TESTS
        )
        self.max_attempts = _resolve_int(
            config,
            "capquery_max_attempts",
            "CAPQUERY_MAX_ATTEMPTS",
            "capquery_max_attempts",
            DEFAULT_MAX_ATTEMPTS,
        )
        self.store = CaptureStore()
        self.state = StateFile(state_file(self.rootdir))
        self.migrations = MigrationsPhase(self)
        self.migrations_path = migrations_file(self.rootdir)
        self.enabled = False
        self.reason = "collection did not happen"
        self.disabled_early = _xdist_running(config)
        if self.disabled_early:
            self.disable("pytest-xdist is not supported, capquery is disabled")
        self.counters: Counter = Counter()
        self.warnings: list[str] = []
        self.original_database_names: dict[str, str] = self._read_original_database_names()
        self.capture_paths: dict[str, Path] = {}
        self.managed: dict[str, pytest.Item] = {}
        self.unstable: list[str] = []
        self.retried: dict[str, int] = {}
        self.unsupported: list[str] = []
        self.deferred: list[str] = []
        #: True when a test of this session will run against the database instead of
        #: the captures, so the migration phase has to be done for real
        self.real_setup_needed = False

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _read_original_database_names() -> dict[str, str]:
        try:
            from django.conf import settings as django_settings

            return {
                alias: str(config["NAME"])
                for alias, config in django_settings.DATABASES.items()
                if config.get("NAME")
            }
        except Exception:  # pragma: no cover - settings are configured by pytest-django
            return {}

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def disable(self, reason: str) -> None:
        self.enabled = False
        self.reason = reason

    # -- hooks -----------------------------------------------------------
    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        if _xdist_running(config):
            self.disable("pytest-xdist is not supported, capquery is disabled")

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Count the collected database tests once ``-k`` / ``-m`` were applied."""
        if self.disabled_early:
            return
        if self.config.getoption("collectonly", default=False):
            self.disable("--collect-only was requested, nothing to capture")
            return
        items = list(session.items)
        db_items = [item for item in items if _is_db_test(item)]
        if len(db_items) <= self.min_tests:
            self.disable(
                f"{len(db_items)} database test(s) collected, capquery needs more than {self.min_tests}"
            )
            return
        for item in db_items:
            if item.get_closest_marker(IGNORE_MARKER) is not None:
                continue
            self.managed[item.nodeid] = item
            self.capture_paths[item.nodeid] = capture_file(self.rootdir, item.nodeid)
        self.enabled = True
        self.reason = f"{len(db_items)} database test(s) for {len(self.managed)} managed test(s)"
        self.unstable = [nodeid for nodeid in self.managed if self.state.is_unstable(nodeid)]
        self.real_setup_needed = self._needs_real_setup(db_items)
        interceptor.install()
        self.migrations.install()
        self._load_store()

    def _needs_real_setup(self, db_items: list[pytest.Item]) -> bool:
        """Does anything in this session have to see the real database?

        A test that runs against postgres (it has no captures yet, its captures were
        dropped as unstable, or it is excluded from capquery) needs the rows the
        migrations write, so the migration phase must not be answered from the
        captures in this session.
        """
        for item in db_items:
            if item.get_closest_marker(IGNORE_MARKER) is not None:
                return True
            if self.state.is_unstable(item.nodeid):
                return True
            if not self._capture_path(item).exists():
                # A test without captures will run against the database. Unless a
                # previous run proved that it captures nothing (a test that only uses
                # the ``db`` fixture without querying), it needs the real rows.
                if not self.state.captures_nothing(item.nodeid):
                    return True
        return False

    def _capture_path(self, item: pytest.Item) -> Path:
        return self.capture_paths.get(item.nodeid) or capture_file(self.rootdir, item.nodeid)

    def _db_blocker(self) -> Any:
        """pytest-django's blocker, needed to touch the database between two tests."""
        try:
            from pytest_django.plugin import blocking_manager_key
        except Exception:  # pragma: no cover - pytest-django is a dependency
            return None
        return self.config.stash.get(blocking_manager_key, None)

    def _make_phase_real(self) -> bool:
        """Make sure the database holds what the migrations write.

        A capture recorded while the migration phase was answered from the captures
        would store the answers of a database that never got the rows of the
        migrations, so the phase is redone for real first.  Returns False when that
        is not possible (a caller then refuses to record anything).
        """
        if not self.migrations.replayed_any:
            return True
        blocker = self._db_blocker()
        if blocker is None:
            return False
        try:
            with blocker.unblock():
                self.migrations.redo_without_cache()
            self.migrations.finish_phase()
        except Exception as exc:
            self.warn(f"capquery: cannot redo the migration phase for real: {exc!r}")
            return False
        return True

    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: Optional[pytest.Item]) -> Optional[bool]:
        if not self.enabled or item.nodeid not in self.capture_paths:
            return None
        self._run_with_retries(item, nextitem)
        return True

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.state.save()
        self.store.close()

    def pytest_terminal_summary(self, terminalreporter, exitstatus: int, config: pytest.Config) -> None:
        write = terminalreporter.write_line
        if not self.enabled:
            write(f"capquery: disabled ({self.reason})")
        else:
            write(f"capquery: {self.reason}")
            missed = self.counters["missed"]
            write(
                f"capquery: statements: {self.counters['replayed']} replayed, {missed} missed, "
                f"{self.counters['db_statements'] - missed} executed against postgres, "
                f"{self.counters['system_statements']} always sent "
                f"(schema, transaction control)"
            )
            migrations = self.migrations.describe()
            if migrations:
                write(f"capquery: {migrations}")
            if self.counters["sequences_reset"] or self.counters["sequences_skipped"]:
                write(
                    f"capquery: sequences reset: {self.counters['sequences_reset']}, "
                    f"skipped (no table yet): {self.counters['sequences_skipped']}"
                )
            created = self.counters["created"]
            updated = self.counters["updated"]
            unchanged = self.counters["unchanged"]
            deleted = self.counters["deleted"]
            write(
                f"capquery: captures {created} created, {updated} updated, "
                f"{unchanged} unchanged, {deleted} deleted"
            )
            if self.deferred:
                write(
                    "capquery: tests that need the real migration phase (captures kept, "
                    "the next run will run the migrations for real): "
                    + ", ".join(self.deferred)
                )
            if self.retried:
                retried = ", ".join(f"{nodeid} ({count + 1} attempts)" for nodeid, count in self.retried.items())
                write(f"capquery: retried tests: {retried}")
            if self.unstable:
                write(f"capquery: unstable tests (captures removed): {', '.join(self.unstable)}")
            for nodeid in self.unsupported:
                write(f"capquery: {nodeid} was not captured, its results hold untypable values")
        for message in self.warnings:
            write(message)
        write("", flush=False)

    # -- internals -------------------------------------------------------
    def _load_store(self) -> None:
        for nodeid, path in self.capture_paths.items():
            if not path.exists():
                continue
            try:
                records = read_capture(path)
            except CaptureFileError as exc:
                self.warn(f"capquery: ignoring a broken capture file: {exc}")
                continue
            self.store.add_records(str(path), records)
        if self.migrations_path.exists():
            try:
                self.store.add_records(
                    str(self.migrations_path), read_capture(self.migrations_path)
                )
            except CaptureFileError as exc:
                self.warn(f"capquery: ignoring a broken capture file: {exc}")

    def _remaining_attempts(self, nodeid: str) -> int:
        return max(0, self.max_attempts - self.state.attempts(nodeid))

    def _prepare(self, item: pytest.Item, path: Path, attempt: int) -> CaptureContext:
        nodeid = item.nodeid
        if attempt > 0:
            mode = RECORD
        elif self.state.is_unstable(nodeid):
            mode = PASSTHROUGH
        elif self.store.known_context(str(path)) or path.exists():
            mode = REPLAY
        else:
            mode = RECORD
        return CaptureContext(key=str(path), mode=mode, store=self.store)

    def _run_with_retries(self, item: pytest.Item, nextitem: Optional[pytest.Item]) -> None:
        from _pytest.runner import runtestprotocol

        nodeid = item.nodeid
        path = self.capture_paths[nodeid]
        if self.state.is_unstable(nodeid) and path.exists():
            # captured once, when the test was marked unstable: on disk it is only
            # dead weight, the test never replays anything again
            delete_capture(path)
            self.store.forget_context(str(path))
            self.counters["deleted"] += 1
        item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        attempt = 0
        while True:
            ctx = self._prepare(item, path, attempt)
            with interceptor.activate(ctx):
                reports = runtestprotocol(item, log=False, nextitem=nextitem)
            self._aggregate(ctx)
            decision = self._decide(ctx, reports)
            if decision == "regenerate":
                if not self._make_phase_real():
                    # The database never got what the migrations write, so a real run of
                    # this test would compare against the wrong rows: keep the captures,
                    # ask the next session for a real migration phase and report the
                    # failure that made the decision instead of hiding it.
                    self.state.set_requires_real(self.migrations.state_key)
                    if nodeid not in self.deferred:
                        self.deferred.append(nodeid)
                    self.warn(
                        f"capquery: {nodeid} cannot be regenerated in this session: the "
                        f"migration phase came from the captures, so the database does not "
                        f"hold what the migrations write. The captures were kept; run the "
                        f"tests again and the phase will be done for real."
                    )
                    decision = "done"
                elif self._remaining_attempts(nodeid) > 0:
                    self.state.note_regeneration(nodeid, self.max_attempts)
                    self.retried[nodeid] = self.retried.get(nodeid, 0) + 1
                    attempt += 1
                    continue
                if not self.state.is_unstable(nodeid):
                    self.state.mark_unstable(nodeid)
                self._drop_captures(item, path, ctx)
                decision = "unstable"
            elif decision == "save":
                self._save(item, path, ctx)
            elif ctx.mode == REPLAY and not ctx.misses:
                self.state.clear_attempts(nodeid)
            for report in reports:
                item.ihook.pytest_runtest_logreport(report=report)
            break
        item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)

    def _decide(self, ctx: CaptureContext, reports: list[Any]) -> str:
        failed = any(report.failed for report in reports)
        call_reports = [report for report in reports if report.when == "call"]
        passed = bool(call_reports) and not failed and all(report.passed for report in call_reports)
        skipped = any(report.skipped for report in reports)
        if ctx.unsupported:
            return "unsupported"
        if skipped:
            return "done"
        if ctx.mode == REPLAY:
            if ctx.misses:
                # rule 2: not every query was answered from the captures
                return "regenerate"
            if not passed and ctx.hits:
                # rule 3: the test failed while using the captures
                return "regenerate"
            return "done"
        if ctx.mode == RECORD:
            # rule 1: captures appear after a successful run only
            return "save" if passed else "done"
        return "done"

    def _save(self, item: pytest.Item, path: Path, ctx: CaptureContext) -> None:
        if ctx.unsupported:
            self._note_unsupported(item, ctx)
            return
        if self.migrations.replayed_any and ctx.db_statements:
            # Recording this run would store the answer of a database that never got
            # the rows of the migrations: never overwrite a capture with that.  A test
            # that did not touch postgres at all is fine — there is nothing to record
            # for it anyway.
            self.state.set_requires_real(self.migrations.state_key)
            if item.nodeid not in self.deferred:
                self.deferred.append(item.nodeid)
            self.warn(
                f"capquery: {item.nodeid} was not captured: the migration phase came from "
                f"the captures, so the database does not hold what the migrations write. "
                f"Run the tests again and the phase will be done for real."
            )
            return
        status = write_capture(path, ctx.recorded)
        if status == "absent":
            if not ctx.recorded:
                # nothing to capture: remember that, so the next session does not keep
                # the migration phase real for the sake of this test
                self.state.set_captures_nothing(item.nodeid)
            return
        self.state.set_captures_nothing(item.nodeid, False)
        self.counters[status] += 1
        if status == "deleted":
            self.store.forget_context(str(path))
        else:
            self.store.replace_context(str(path), ctx.recorded)

    def _drop_captures(self, item: pytest.Item, path: Path, ctx: CaptureContext) -> None:
        status = delete_capture(path)
        if status == "deleted":
            self.counters["deleted"] += 1
        self.store.forget_context(str(path))
        if item.nodeid not in self.unstable:
            self.unstable.append(item.nodeid)
        self.warn(
            f"capquery: {item.nodeid} is unstable, its captures were removed and it will always "
            f"run against the database"
        )

    def _note_unsupported(self, item: pytest.Item, ctx: CaptureContext) -> None:
        if item.nodeid not in self.unsupported:
            self.unsupported.append(item.nodeid)
        self.warn(f"capquery: {item.nodeid}: {ctx.unsupported}")

    def _aggregate(self, ctx: CaptureContext) -> None:
        self.counters["replayed"] += ctx.hits
        self.counters["missed"] += ctx.misses
        self.counters["db_statements"] += ctx.db_statements
        self.counters["system_statements"] += ctx.system_statements
