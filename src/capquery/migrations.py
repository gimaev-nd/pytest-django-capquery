"""The database setup phase: migrations and everything around them.

The whole setup phase is captured into ``<rootdir>/captures/migrations.yaml`` and
replayed on the next run, exactly like the statements of a test — the reads of the
executor included, but also the ``INSERT``s of the seed migrations.  Schema DDL is
never replayed: the test database is really created and really gets the schema of
the project, so a statement that cannot be answered from the captures still finds
a usable database.

The phase is the ``setup_databases`` call of Django's test utilities, not just
``MigrationExecutor.migrate``: the ``migrate`` command emits the post migrate
signal (content types, permissions) *after* the executor returned, and those
statements must not land in the captures of the first test of the session.  Since
session fixtures are set up inside the protocol of the first test, anything
outside this boundary would be recorded into that test's capture file.

Two things happen here that cannot be expressed with pytest-django's fixtures:

* before the first ``migrate`` of the session the sequences of the test database
  are reset with ``sqlsequencereset`` so that ids start from 1 again (otherwise
  captures would change on every run of a reused database and pollute merge
  requests);
* when a replay of the phase misses a query, the phase is redone from scratch
  without the cache and the captures are updated.

The post migrate signal (Django's content types and permissions) is explicitly left
out of the captures and always executed for real: it builds its ``IN (...)``
parameter lists from a ``set``, so the very same statement carries differently
ordered parameters in every process — nothing that could ever be replayed.

Replaying is only possible when the database is created from scratch: with
``--reuse-db`` the state at replay time does not match the state that was
recorded, so the phase falls back to plain execution.
"""

from __future__ import annotations

import io
from typing import Any, Optional

import pytest

from . import interceptor
from .interceptor import PASSTHROUGH, RECORD, REPLAY, CaptureContext
from .paths import MIGRATIONS_CONTEXT
from .records import Record

__all__ = ["MigrationsPhase"]


class MigrationsPhase:
    """Patches the database setup of Django for the lifetime of the session."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._original_migrate = None
        self._original_setup_databases = None
        self._original_post_migrate = None
        self._patched = False
        self._depth = 0
        self._started: set[str] = set()
        self.aliases: list[str] = []
        self._collected: list[Record] = []
        self.needs_clean_rerun = False
        self.passthrough_reason: Optional[str] = None
        self.real_reason: Optional[str] = None
        self.replayed_any = False
        self.missed_sqls: list[str] = []
        self.replayed = 0
        self.missed = 0
        self.recorded_count = 0
        self.state_key = MIGRATIONS_CONTEXT

    # -- installation ----------------------------------------------------
    def install(self) -> None:
        """Wrap ``setup_databases`` (the whole phase) and ``migrate`` (the reset)."""
        if self._patched:
            return
        from django.db.migrations.executor import MigrationExecutor
        from django.test import utils as django_test_utils

        from django.core.management.commands import migrate as migrate_command

        self._original_migrate = MigrationExecutor.migrate
        self._original_setup_databases = django_test_utils.setup_databases
        self._original_post_migrate = migrate_command.emit_post_migrate_signal
        phase = self

        def migrate(executor, *args, **kwargs):
            return phase.run_migrate(executor, args, kwargs)

        def setup_databases(*args, **kwargs):
            return phase.run_setup_databases(args, kwargs)

        def emit_post_migrate_signal(*args, **kwargs):
            # bookkeeping of Django itself, not of the project's migrations: it is
            # never captured (see the module docstring) and always executed
            with interceptor.suspend():
                return phase._original_post_migrate(*args, **kwargs)

        MigrationExecutor.migrate = migrate
        django_test_utils.setup_databases = setup_databases
        migrate_command.emit_post_migrate_signal = emit_post_migrate_signal
        self._patched = True

    def uninstall(self) -> None:
        if not self._patched:
            return
        from django.core.management.commands import migrate as migrate_command
        from django.db.migrations.executor import MigrationExecutor
        from django.test import utils as django_test_utils

        if self._original_migrate is not None:
            MigrationExecutor.migrate = self._original_migrate
        if self._original_setup_databases is not None:
            django_test_utils.setup_databases = self._original_setup_databases
        if self._original_post_migrate is not None:
            migrate_command.emit_post_migrate_signal = self._original_post_migrate
        self._patched = False

    # -- the phase itself -------------------------------------------------
    def run_setup_databases(self, args, kwargs):
        """Run Django's database setup with capture/replay enabled."""
        aliases = list(kwargs.get("aliases") or self._default_aliases())
        if not aliases:
            return self._original_setup_databases(*args, **kwargs)
        keepdb = bool(kwargs.get("keepdb", False))
        mode = self.mode_for(aliases[0], keepdb)
        ctx = CaptureContext(
            key=str(self.plugin.migrations_path), mode=mode, store=self.plugin.store
        )
        self._depth += 1
        try:
            with interceptor.activate(ctx):
                result = self._original_setup_databases(*args, **kwargs)
        finally:
            self._depth -= 1

        self.replayed += ctx.hits
        self.missed += ctx.misses
        if mode == REPLAY and ctx.replayed_statements:
            # the database does not hold what the migrations would have written
            self.replayed_any = True
        self.missed_sqls.extend(ctx.missed_sql)
        self.aliases = sorted(set(self.aliases) | set(aliases))
        if mode == RECORD:
            self._collected.extend(ctx.recorded)
            self.recorded_count = len(self._collected)
        if mode == REPLAY and ctx.misses:
            # "if the whole phase could not be served from the captures, redo it
            # from scratch without the cache and update the captures"
            self.needs_clean_rerun = True
        return result

    def run_migrate(self, executor, args, kwargs):
        """Reset the sequences once per alias, then run the migrations."""
        alias = executor.connection.alias
        if alias not in self._started:
            self._started.add(alias)
            self.reset_sequences(executor.connection)
        return self._original_migrate(executor, *args, **kwargs)

    @staticmethod
    def _default_aliases() -> list[str]:
        from django.db import connections

        return list(connections)

    def mode_for(self, alias: str, keepdb: bool) -> str:
        self.passthrough_reason = None
        self.real_reason = None
        if self.plugin.state.is_unstable(self.state_key):
            self.passthrough_reason = "the migration phase was marked unstable"
            return PASSTHROUGH
        fresh = self.is_fresh_database(alias, keepdb)
        if not fresh:
            # A database that already holds data was not created by this run: its
            # queries cannot go into a file meant for a fresh database, and the
            # executor would decide from cached reads.
            self.passthrough_reason = (
                "the test database already exists, its state does not match the recording"
            )
            return PASSTHROUGH
        if self.plugin.state.requires_real(self.state_key):
            # An earlier session replayed this phase while a test ran against the
            # database: do the migrations for real now and record them again.
            self.real_reason = "an earlier session asked for the real migration phase"
            self.plugin.state.set_requires_real(self.state_key, False)
            return RECORD
        if self.plugin.real_setup_needed:
            # Some test of this session runs against the database (it has no captures
            # yet, or it is excluded from capquery): it needs the rows the migrations
            # write, so the phase is done for real — and recorded again.
            self.real_reason = "some tests of this session run against the database"
            return RECORD
        if not self.plugin.migrations_path.exists():
            return RECORD
        return REPLAY

    def is_fresh_database(self, alias: str, keepdb: bool) -> bool:
        """True when this run creates the test database from scratch."""
        if not keepdb:
            return True
        return not self.test_database_exists(alias)

    def test_database_exists(self, alias: str) -> bool:
        """Ask postgres whether the test database of this alias is already there."""
        from django.db import connections

        connection = connections[alias]
        name = connection.creation._get_test_db_name()
        with interceptor.suspend():
            try:
                with connection.creation._nodb_cursor() as cursor:
                    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
                    return cursor.fetchone() is not None
            except Exception as exc:
                self.plugin.warn(f"capquery: cannot check whether {name} exists: {exc!r}")
                return True

    # -- helpers ----------------------------------------------------------
    def reset_sequences(self, connection) -> None:
        """``sqlsequencereset`` before migrations, as required for stable captures.

        The command builds ``setval(...)`` calls for every model with an automatic
        primary key without asking whether the tables exist yet (postgres derives
        the sequence with ``pg_get_serial_sequence``).  On a database created from
        scratch the tables do not exist, so every statement is executed on its own
        and the ones pointing at missing tables are skipped quietly.
        """
        from django.apps import apps as django_apps
        from django.core.management import call_command
        from django.db import DatabaseError, transaction

        labels = [config.label for config in django_apps.get_app_configs()]
        buffer = io.StringIO()
        try:
            with interceptor.suspend():
                call_command(
                    "sqlsequencereset",
                    *labels,
                    database=connection.alias,
                    stdout=buffer,
                    verbosity=0,
                )
        except Exception as exc:
            self.plugin.warn(f"capquery: sqlsequencereset failed for {connection.alias}: {exc}")
            return

        statements = self._split_statements(buffer.getvalue())
        reset = skipped = 0
        with interceptor.suspend():
            for statement in statements:
                try:
                    with transaction.atomic(using=connection.alias):
                        with connection.cursor() as cursor:
                            cursor.execute(statement)
                    reset += 1
                except DatabaseError:
                    skipped += 1
        self.plugin.counters["sequences_reset"] += reset
        self.plugin.counters["sequences_skipped"] += skipped

    @staticmethod
    def _split_statements(sql: str) -> list[str]:
        statements = []
        for chunk in sql.split(";"):
            statement = chunk.strip()
            if not statement or statement.upper() in {"BEGIN", "COMMIT"}:
                continue
            statements.append(statement)
        return statements

    def finish_phase(self) -> None:
        """Write the captures collected in record mode and count attempts."""
        from .yaml_io import CaptureFileError, write_capture

        if self._collected and self.recorded_count:
            try:
                status = write_capture(self.plugin.migrations_path, self._collected)
            except CaptureFileError as exc:
                # a value capquery cannot store must not end the run: the phase did
                # happen for real, only its captures are not written
                self.plugin.warn(f"capquery: the migration phase was not captured: {exc}")
            else:
                self.plugin.counters[f"migrations_{status}"] += 1
                if status in ("created", "updated"):
                    self.plugin.store.replace_context(
                        str(self.plugin.migrations_path), self._collected
                    )
        if self.needs_clean_rerun:
            self.plugin.state.note_regeneration(self.state_key, self.plugin.max_attempts)
        else:
            self.plugin.state.clear_attempts(self.state_key)

    def redo_without_cache(self) -> None:
        """Destroy and recreate the test databases, migrating with the cache off.

        The captures collected by this clean run are written by ``finish_phase``.
        """
        from django.db import connections

        self.plugin.warn(
            "capquery: redoing the migration phase from scratch without the cache, so that "
            "the database holds what the migrations write"
        )
        self.needs_clean_rerun = False
        if self.missed_sqls:
            self.plugin.warn(
                "capquery: queries the migration replay did not find:\n  "
                + "\n  ".join(self.missed_sqls)
            )
        self._collected = []
        ctx = CaptureContext(
            key=str(self.plugin.migrations_path), mode=RECORD, store=self.plugin.store
        )
        with interceptor.activate(ctx):
            for alias in list(self.aliases):
                connection = connections[alias]
                database_name = connection.settings_dict["NAME"]
                original_name = self.plugin.original_database_names.get(alias)
                connection.close()
                with interceptor.suspend():
                    connection.creation._destroy_test_db(database_name, verbosity=0)
                    if original_name:
                        # create_test_db() prefixes the current name, so put the
                        # original one back, exactly as Django's teardown does
                        connection.settings_dict["NAME"] = original_name
                self._started.discard(alias)
                connection.creation.create_test_db(
                    verbosity=0, autoclobber=True, serialize=False, keepdb=False
                )
        self._collected.extend(ctx.recorded)
        self.recorded_count = len(self._collected)
        # the database really got what the migrations write now
        self.replayed_any = False

    def describe(self) -> Optional[str]:
        if not self.aliases:
            return None
        if not (self.replayed or self.missed or self.recorded_count) and self.passthrough_reason:
            return f"migrations: not captured ({self.passthrough_reason})"
        line = (
            f"migrations: {self.replayed} statement(s) replayed, "
            f"{self.missed} missed, {self.recorded_count} recorded"
        )
        if self.real_reason:
            line += f" (executed for real: {self.real_reason})"
        return line


def _warn_usage(message: str) -> None:  # pragma: no cover - convenience helper
    raise pytest.UsageError(message)
