"""Prove that the captures of a replayed run do not change.

The script copies ``project/`` into a work directory, starts an embedded postgres
(``pgserver``) and runs the project's suite several times in a row.  After every run it
hashes every capture file and reads the plugin's own report; on the runs after the first
one it also reads the *postgres server log* for the statements that really arrived, which
is the one check the plugin cannot write for itself.

A run is only accepted when all of this holds:

* run 1 creates one capture file per managed test (so "nothing changed" cannot be true
  because nothing was captured);
* runs 2..n are byte-identical to run 1 (capture files and the state file);
* runs 2..n report ``missed == 0``, ``executed against postgres == 0`` and
  ``captures 0 created, 0 updated``;
* the migration phase is replayed on runs 2..n without a miss;
* the postgres log of runs 2..n holds no read and no write of the application's tables —
  only DDL and the bookkeeping (``django_migrations``, content types) every session does;
* a *control* step: one query of one test is changed on purpose, the run after that has to
  notice it, regenerate exactly that one capture file, and the run after that has to be
  stable again.  Without the control the stability above could be an artefact of the
  captures never being consulted.

Usage::

    .venv/bin/python verification/run.py                # 5 runs + control + probe session
    .venv/bin/python verification/run.py --runs 3 --show-log
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE / "project"
DEFAULT_WORK = Path(
    os.environ.get("CAPQUERY_VERIFY_WORK", "/opt/data/profiles/capquery/cache/scratch/capquery-verify")
)

ENABLED_RE = re.compile(r"capquery: (\d+) database test\(s\) for (\d+) managed test\(s\)")
STATEMENTS_RE = re.compile(
    r"statements: (\d+) replayed, (\d+) missed, (\d+) executed against postgres, (\d+) always sent"
)
CAPTURES_RE = re.compile(r"captures (\d+) created, (\d+) updated, (\d+) unchanged, (\d+) deleted")
MIGRATIONS_RE = re.compile(
    r"migrations: (?:not captured[^)]*\)|(\d+) statement\(s\) replayed, (\d+) missed, (\d+) recorded)"
)
PASSED_RE = re.compile(r"(\d+) passed")
RETRIED_RE = re.compile(r"capquery: retried tests: (.*)")
UNCAPTURED_RE = re.compile(r"was not captured: it holds values capquery cannot store")
RUNS_RE = re.compile(r"sequences reset: (\d+), skipped \(no table yet\): (\d+)")

LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ [A-Z]+ \[\d+\] ")
LOG_STATEMENT_RE = re.compile(r"(?:execute [^:]*|statement): (.*)$")
WRITE_RE = re.compile(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\b")
APP_TABLE_RE = re.compile(r'"(?:shop_ticket|shop_team|shop_comment|shop_ticket_watchers)"')
READ_RE = re.compile(r"^\s*\(*\s*(?:WITH|SELECT|TABLE)\b", re.IGNORECASE)

OK = "PASS"
BAD = "FAIL"

#: One query of one test, changed on purpose by the control step: the captures of that
#: test have to be regenerated, and nothing else in the project may change.
CONTROL_BEFORE = 'assert ids(Ticket.objects.filter(labels__contains=["bug"])) == [1, 2, 3, 5]'
CONTROL_AFTER = 'assert ids(Ticket.objects.filter(labels__contains=["cache"])) == [5]'
CONTROL_CAPTURE = "tests/captures/test_reads_postgres.py/test_array_contains_a_label.yaml"


class RunResult:
    def __init__(self, number: int, label: str, returncode: int, output: str) -> None:
        self.number = number
        self.label = label
        self.returncode = returncode
        self.output = output
        self.enabled = ENABLED_RE.search(output)
        self.statements = STATEMENTS_RE.search(output)
        self.captures = CAPTURES_RE.search(output)
        self.migrations = MIGRATIONS_RE.search(output)
        self.passed = PASSED_RE.search(output)
        self.retried = RETRIED_RE.search(output)
        self.uncaptured = UNCAPTURED_RE.findall(output)
        self.sequences = RUNS_RE.search(output)
        self.hashes: dict[str, str] = {}
        self.log_statements: list[str] = []

    # -- parsed numbers ------------------------------------------------
    @property
    def database_tests(self) -> int:
        return int(self.enabled.group(1)) if self.enabled else 0

    @property
    def managed(self) -> int:
        return int(self.enabled.group(2)) if self.enabled else 0

    @property
    def replayed(self) -> int:
        return int(self.statements.group(1)) if self.statements else 0

    @property
    def missed(self) -> int:
        return int(self.statements.group(2)) if self.statements else -1

    @property
    def executed(self) -> int:
        return int(self.statements.group(3)) if self.statements else -1

    @property
    def created(self) -> int:
        return int(self.captures.group(1)) if self.captures else -1

    @property
    def updated(self) -> int:
        return int(self.captures.group(2)) if self.captures else -1

    @property
    def unchanged(self) -> int:
        return int(self.captures.group(3)) if self.captures else -1

    @property
    def migrations_replayed(self) -> int:
        return int(self.migrations.group(1)) if self.migrations and self.migrations.group(1) else 0

    @property
    def migrations_missed(self) -> int:
        return int(self.migrations.group(2)) if self.migrations and self.migrations.group(2) else 0

    # -- the postgres log ---------------------------------------------
    @property
    def log_app_writes(self) -> list[str]:
        """Writes to the application's own tables: the ones that must never appear."""
        return [
            sql for sql in self.log_statements if WRITE_RE.search(sql) and APP_TABLE_RE.search(sql)
        ]

    @property
    def log_system_writes(self) -> list[str]:
        """Writes to Django's own bookkeeping tables (never cached by design)."""
        return [
            sql for sql in self.log_statements if WRITE_RE.search(sql) and not APP_TABLE_RE.search(sql)
        ]

    @property
    def log_app_reads(self) -> list[str]:
        return [
            sql
            for sql in self.log_statements
            if READ_RE.match(sql) and APP_TABLE_RE.search(sql) and "setval(" not in sql
        ]

    def summary_lines(self) -> list[str]:
        return [line for line in self.output.splitlines() if line.startswith("capquery:")]


def hash_tree(root: Path) -> dict[str, str]:
    """SHA256 of every file under ``root``, keyed by its path relative to it."""
    digests = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def tree_of(work: Path) -> dict[str, str]:
    """Every capture file of the project, keyed by its path in the project.

    The migration phase and the state file live in ``captures/``, the captures of the
    tests mirror the path of their module under ``tests/captures/``.
    """
    return hash_tree(work / "captures") | {
        f"tests/captures/{name}": digest
        for name, digest in hash_tree(work / "tests" / "captures").items()
    }


def read_log(offset: int, log_path: Path) -> tuple[str, int]:
    """The part of the postgres log written since ``offset``."""
    if not log_path.exists():
        return "", offset
    with log_path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
        return chunk.decode("utf-8", "replace"), handle.tell()


def parse_log(chunk: str) -> list[str]:
    """Every statement the server received, one entry per ``execute``/``statement``."""
    statements: list[str] = []
    current: list[str] | None = None
    for line in chunk.splitlines():
        if LOG_LINE_RE.match(line):
            match = LOG_STATEMENT_RE.search(line)
            if match:
                current = [match.group(1)]
                statements.append(match.group(1))
            elif current is not None and re.search(r"(ERROR|DETAIL|STATEMENT|HINT|CONTEXT):", line):
                current.append(line.split(": ", 1)[-1])
                statements.append(line.split(": ", 1)[-1])
            else:
                current = None
            continue
        if current is not None:
            current.append(line)
            statements[-1] = statements[-1] + "\n" + line
    return [" ".join(statement.split()) for statement in statements]


def start_postgres(pgdata: Path) -> dict:
    """Start (or reuse) the embedded postgres; return the connection settings."""
    import pgserver

    pgserver.get_server(str(pgdata), cleanup_mode=None)
    return {
        "host": str(pgdata),
        "port": "",
        "user": "postgres",
        "password": "",
        "dbname": "capquery_verify",
    }


def _connect(host: Path):
    import psycopg

    return psycopg.connect(host=str(host), user="postgres", dbname="postgres", autocommit=True)


def enable_statement_logging(host: Path) -> None:
    """Let the server itself record every statement it receives."""
    with _connect(host) as connection:
        connection.execute("ALTER SYSTEM SET log_statement = 'all'")
        connection.execute("SELECT pg_reload_conf()")


def prepare_work(work: Path) -> Path:
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(
        PROJECT,
        work,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "captures", "*.pyc"),
    )
    return work


def run_pytest(work: Path, env: dict, args: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
    )
    return completed.returncode, completed.stdout + completed.stderr


def run_session(
    work: Path,
    env: dict,
    log_path: Path,
    args: list[str],
    label: str,
    number: int = 0,
    show_log: bool = False,
) -> RunResult:
    """One pytest session of the project plus what postgres itself saw of it."""
    offset = log_path.stat().st_size if log_path.exists() else 0
    returncode, output = run_pytest(work, env, args)
    chunk, _ = read_log(offset, log_path)
    result = RunResult(number, label, returncode, output)
    result.log_statements = parse_log(chunk)
    result.hashes = tree_of(work)
    print(f"--- {label}: exit code {returncode}, {len(result.hashes)} capture file(s) ---")
    for line in result.summary_lines():
        print(f"    {line}")
    if returncode != 0:
        # pytest's own summary is the only useful diagnostic when a run fails
        index = output.find("short test summary info")
        tail = output[index:] if index != -1 else output
        for line in tail.splitlines()[-25:]:
            print(f"    ! {line}")
    if show_log:
        for statement in sorted({sql[:160] for sql in result.log_statements}):
            print(f"    | {statement}")
    print()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=5, help="how often to run the suite (default 5)")
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK, help="work directory (default %(default)s)")
    parser.add_argument("--pgdata", type=Path, default=Path("/tmp/capquery-pgdata"), help="postgres data dir")
    parser.add_argument("--show-log", action="store_true", help="print every statement the server received")
    parser.add_argument("--no-probe", action="store_true", help="skip the probe session")
    parser.add_argument("--no-control", action="store_true", help="skip the control step")
    args = parser.parse_args()

    database = start_postgres(args.pgdata)
    enable_statement_logging(args.pgdata)
    log_path = args.pgdata / "log"

    work = prepare_work(args.work)
    print(f"work directory: {work}")
    print(f"postgres:       {database['host']} (log: {log_path})")
    print()

    env = dict(os.environ)
    env.update(
        CAPQUERY_VERIFY_DB_HOST=database["host"],
        CAPQUERY_VERIFY_DB_PORT=database["port"],
        CAPQUERY_VERIFY_DB_USER=database["user"],
        CAPQUERY_VERIFY_DB_PASSWORD=database["password"],
        CAPQUERY_VERIFY_DB_NAME=database["dbname"],
    )

    results: list[RunResult] = []
    for number in range(1, args.runs + 1):
        # a different hash seed per run: parameters built from a set would reorder
        env["PYTHONHASHSEED"] = str(number)
        env["CAPQUERY_VERIFY_EXPECT_TICKETS"] = "5" if number == 1 else "0"
        results.append(
            run_session(work, env, log_path, ["tests"], f"run {number}", number=number, show_log=args.show_log)
        )

    failures: list[str] = []
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, condition, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))
        if not condition:
            failures.append(name)

    first, rest = results[0], results[1:]

    check("the plugin was active", first.database_tests > 5 and first.managed > 0,
          f"{first.database_tests} database test(s), {first.managed} managed")
    check("the suite passed on every run", all(r.returncode == 0 for r in results),
          ", ".join(f"run {r.number}: exit {r.returncode}" for r in results))
    check("run 1 created one capture per managed test", first.created == first.managed,
          f"{first.created} files created for {first.managed} managed test(s)")
    check("no capture file is missing", len(first.hashes) >= first.managed,
          f"{len(first.hashes)} file(s) on disk")
    check("run 1 recorded the migration phase",
          bool(first.migrations) and first.migrations.group(3) not in (None, "0"),
          first.migrations.group(0) if first.migrations else "no migrations line")

    for result in rest:
        prefix = f"run {result.number}"
        check(f"{prefix}: nothing was missed", result.missed == 0, f"missed={result.missed}")
        check(f"{prefix}: no data statement reached postgres", result.executed == 0, f"executed={result.executed}")
        check(f"{prefix}: no capture was written", result.created == 0 and result.updated == 0,
              f"created={result.created}, updated={result.updated}")
        check(f"{prefix}: the migration phase was replayed", result.migrations_replayed > 0 and result.migrations_missed == 0,
              result.migrations.group(0) if result.migrations else "no migrations line")
        check(f"{prefix}: the captures are byte-identical to run 1", result.hashes == first.hashes,
              _describe_diff(first.hashes, result.hashes))
        check(f"{prefix}: the server received no write of the application's tables", not result.log_app_writes,
              "; ".join(result.log_app_writes[:3]))
        check(f"{prefix}: the server received no read of the application's tables", not result.log_app_reads,
              "; ".join(result.log_app_reads[:3]))
        check(f"{prefix}: the tests still passed", bool(result.passed) and result.passed.group(1) == (first.passed.group(1) if first.passed else None),
              f"passed={result.passed.group(1) if result.passed else '?'}, first run={first.passed.group(1) if first.passed else '?'}")
        print(
            f"    (postgres still received {len(result.log_system_writes)} write(s) to Django's own "
            f"bookkeeping tables, which capquery never caches)"
        )

    if not args.no_control:
        control = run_control(work, env, log_path, first.hashes, show_log=args.show_log)
        if control is None:
            check("control: the query to change was found", False, "CONTROL_BEFORE is not in the test file")
        else:
            changed, settled = control["changed"], control["settled"]
            check("control: the changed query was noticed", changed.updated == 1,
                  f"updated={changed.updated}, retried={changed.retried.group(1) if changed.retried else 'nothing'}")
            check("control: the test was retried once",
                  bool(changed.retried) and "test_array_contains_a_label" in changed.retried.group(1),
                  changed.retried.group(1) if changed.retried else "no 'retried tests' line")
            check("control: the changed query reached postgres and was re-answered",
                  changed.missed >= 1 and any('"labels" @>' in sql for sql in changed.log_statements),
                  f"missed={changed.missed}, app statements in the log: {len(changed.log_app_reads)}")
            check("control: the suite still passes", changed.returncode == 0 and settled.returncode == 0,
                  f"exit {changed.returncode} / {settled.returncode}")
            check("control: no other test's capture changed",
                  [name for name in control["changed_files"] if name.startswith("tests/captures/")]
                  == [CONTROL_CAPTURE],
                  ", ".join(control["changed_files"]) or "nothing changed")
            check("control: the captures settle again after the regeneration",
                  not control["settled_diff"] and settled.missed == 0 and settled.executed == 0
                  and control["again"].missed == 0 and control["again"].executed == 0,
                  _summarise_churn(control, settled))

    if not args.no_probe:
        env_probe = dict(env, CAPQUERY_VERIFY_EXPECT_TICKETS="5")
        probe = run_session(work, env_probe, log_path, ["probe"], "probe session 1", show_log=args.show_log)
        phase = work / "captures" / "migrations.yaml"
        phase_first = hashlib.sha256(phase.read_bytes()).hexdigest() if phase.exists() else ""
        rows_first = applied_rows(phase)
        # the same session again, with nothing changed in between: the captures of a
        # session that has to record the migration phase must be stable too
        second = run_session(
            work, env_probe, log_path, ["probe"], "probe session 2 (nothing changed)", show_log=args.show_log
        )
        phase_second = hashlib.sha256(phase.read_bytes()).hexdigest() if phase.exists() else ""
        check("probe: the tests still pass", probe.returncode == 0 and second.returncode == 0,
              f"exit {probe.returncode} / {second.returncode}")
        check("probe: no capture file was written for the probe tests",
              not (work / "probe" / "captures").exists(),
              str(work / "probe" / "captures"))
        check("probe: the statements really went to postgres",
              any("shop_ticket" in sql for sql in probe.log_statements),
              f"{len(probe.log_statements)} statement(s) in the log")
        check("probe: the plugin reports every uncapturable test", len(probe.uncaptured) == probe.managed,
              f"{len(probe.uncaptured)} 'was not captured' line(s) for {probe.managed} managed test(s)")
        check("probe: two probe sessions leave the phase capture alone", phase_first == phase_second,
              _describe_phase_churn(rows_first, applied_rows(phase), second))
        print(
            f"    (the {probe.managed} probe tests hold a value capquery cannot store: they always run "
            f"against postgres, are reported in the summary, and because one of them does run against the "
            f"database the migration phase is recorded for real in every such session — its capture has to "
            f"come out byte-identical anyway)"
        )

    print("=" * 78)
    for name, ok, detail in checks:
        print(f"[{OK if ok else BAD}] {name}" + (f"  -- {detail}" if detail and not ok else ""))
    print("=" * 78)
    phase_capture_failures = [name for name in failures if "phase capture" in name or "settle again" in name]
    if failures:
        replay_failures = [name for name in failures if name not in phase_capture_failures]
        print(
            f"the replay path: "
            + ("PASS — repeated runs replay everything and change nothing"
               if not replay_failures
               else f"FAIL — {len(replay_failures)} check(s): {', '.join(replay_failures)}")
        )
        print(
            "the migration phase: "
            + ("PASS" if not phase_capture_failures
               else f"FAIL — {', '.join(phase_capture_failures)}")
        )
        if phase_capture_failures:
            print(
                "\nThe failing checks above are not of the replay path: they are what happens as soon as\n"
                "the plugin has to record the migration phase for real.  `captures/migrations.yaml` then\n"
                "holds the `applied` timestamps of `django_migrations`, so it is rewritten on every run,\n"
                "and a phase recorded in a process that already resolved postgres' own type oids is not\n"
                "the same file as one recorded from a cold start.  evidence and reproduction:\n"
                "verification/README.md"
            )
        return 1
    print(
        f"all {len(checks)} checks passed: {args.runs} runs of {first.managed} managed tests, "
        f"{len(first.hashes)} capture files, byte-identical after the recording run"
    )
    return 0


def _describe_diff(expected: dict[str, str], actual: dict[str, str]) -> str:
    changed = [name for name in sorted(set(expected) | set(actual)) if expected.get(name) != actual.get(name)]
    return ", ".join(changed[:5]) or "no difference"


def _summarise_churn(control: dict, settled: RunResult) -> str:
    """Why the captures did not settle: the phase capture is re-recorded on every run."""
    lines = [
        f"{', '.join(control['settled_diff'])} changed between two runs that changed nothing"
    ]
    rows_before, rows_after = control["rows_before"], control["rows_after"]
    for before, after in zip(rows_before, rows_after):
        if before != after:
            lines.append(f"the phase capture holds a row of the current run: {before} -> {after}")
            break
    if control["lost_sqls"]:
        lines.append(
            "the re-recorded phase lost/saw different statements than a fresh one: "
            + "; ".join(" ".join(sql.split())[:70] for sql in control["lost_sqls"][:4])
        )
    if settled.migrations:
        lines.append(f"the phase was not replayed: {settled.migrations.group(0)}")
    return " | ".join(lines)


def _describe_phase_churn(rows_before: list, rows_after: list, result: RunResult) -> str:
    """Why the phase capture is not stable: the rows it captures hold the time of the run."""
    for before, after in zip(rows_before, rows_after):
        if before != after:
            return (
                f"captures/migrations.yaml changed between the two sessions: one of its rows went "
                f"from {before} to {after}"
                + (f" | {result.migrations.group(0)}" if result.migrations else "")
            )
    return (
        "captures/migrations.yaml changed between the two sessions"
        + (f" | {result.migrations.group(0)}" if result.migrations else "")
    )


def run_control(
    work: Path,
    env: dict,
    log_path: Path,
    baseline: dict[str, str],
    show_log: bool = False,
) -> dict | None:
    """Change one query of one test and watch the captures be regenerated.

    Without this step the stability of the runs above could be an artefact of the
    captures never being looked at: the plugin has to notice the change (the stored
    answer is a miss), retry the test, store the new answer — and then be stable again.
    The last part is the interesting one: regenerating *any* capture makes the plugin
    record the migration phase for real, and that is where the phase capture stops
    being stable (see the return value and ``verification/README.md``).
    """
    path = work / "tests" / "test_reads_postgres.py"
    text = path.read_text()
    if CONTROL_BEFORE not in text:
        return None
    path.write_text(text.replace(CONTROL_BEFORE, CONTROL_AFTER))

    phase = work / "captures" / "migrations.yaml"
    before = {"rows": applied_rows(phase), "sqls": phase_sqls(phase)}

    print("--- control: one query of one test is changed on purpose ---")
    # regenerating a capture makes the plugin redo the migration phase for real, so the
    # database holds the rows the seed migration writes on this run
    env = dict(env, CAPQUERY_VERIFY_EXPECT_TICKETS="0,5")
    changed = run_session(work, env, log_path, ["tests"], "control run A (after the change)", show_log=show_log)
    after_a = {"rows": applied_rows(phase), "sqls": phase_sqls(phase)}
    changed_files = _difference(baseline, changed.hashes)

    # run B replays the regenerated capture and clears the regeneration counter of the
    # test, so the state file may change once more; run C is what "settled" means
    settled = run_session(work, env, log_path, ["tests"], "control run B (nothing changed since)", show_log=show_log)
    after_b = {"rows": applied_rows(phase), "sqls": phase_sqls(phase)}
    settled_diff = _difference(changed.hashes, settled.hashes)

    again = run_session(work, env, log_path, ["tests"], "control run C (still nothing changed)", show_log=show_log)
    final_diff = _difference(settled.hashes, again.hashes)
    return {
        "changed": changed,
        "settled": settled,
        "again": again,
        "changed_files": changed_files,
        "settled_diff": final_diff,
        "lost_sqls": [sql for sql in after_a["sqls"] if sql not in before["sqls"]]
        + [sql for sql in before["sqls"] if sql not in after_a["sqls"]],
        "rows_before": after_b["rows"],
        "rows_after": applied_rows(phase),
    }


def applied_rows(phase: Path) -> list:
    """The captured rows of the ``django_migrations`` read of the migration phase."""
    import yaml

    if not phase.exists():
        return []
    data = yaml.safe_load(phase.read_text(encoding="utf-8")) or {}
    for capture in data.get("captures", []):
        sql = capture.get("sql", "")
        if "django_migrations" in sql and "applied" in sql:
            return capture.get("rows", [])
    return []


def phase_sqls(phase: Path) -> list[str]:
    """The statements the migration-phase capture holds."""
    import yaml

    if not phase.exists():
        return []
    data = yaml.safe_load(phase.read_text(encoding="utf-8")) or {}
    return [capture.get("sql", "") for capture in data.get("captures", [])]


def _difference(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]


if __name__ == "__main__":
    sys.exit(main())
