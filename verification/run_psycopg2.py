"""The verification project on psycopg2: repeated runs must replay everything.

Why a second script: `run.py` drives the project through `pytester`-independent subprocess
runs with the driver the repo develops against (psycopg 3, through Django's default
choice).  Django 4.2/5.x also runs on psycopg2, and that driver prepares the postgres-only
values differently — an `inet` parameter comes as `psycopg2.extras.Inet`, psycopg2's range
types expose no public `bounds` — so the encoder has to recognize those shapes too, or
every test holding a range or an address becomes uncapturable.

Run it with an interpreter of a venv that has psycopg2 and *not* psycopg3 (otherwise
Django picks psycopg3 and this would repeat `run.py`)::

    uv venv .venv-pg2 --python 3.12
    uv pip install --python .venv-pg2/bin/python -e ".[dev]" psycopg2-binary pgserver
    uv pip uninstall --python .venv-pg2/bin/python psycopg psycopg-binary
    .venv-pg2/bin/python verification/run_psycopg2.py            # 4 runs
    .venv-pg2/bin/python verification/run_psycopg2.py --runs 2

It copies `project/` into a work directory, starts the embedded postgres (`pgserver`) and
checks, per run: the suite passes, run 1 creates one capture file per managed test (so
nothing is silently uncapturable), runs 2..n replay every statement with `missed == 0`,
`executed against postgres == 0`, write no capture file and replay the migration phase,
and every capture file is byte-identical to run 1.

Two of the project's own tests are patched in the copy, because they use an API only
psycopg3 has (`Range.bounds`, `connection.execute`): the project is the same, its
assertions are written driver-agnostically.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE / "project"
DEFAULT_WORK = Path(
    os.environ.get("CAPQUERY_VERIFY_PG2_WORK", "/tmp/capquery-verify-psycopg2")
)

RAW_IMPORT_BEFORE = "import psycopg\nimport pytest\n"
RAW_IMPORT_AFTER = (
    "try:\n"
    "    import psycopg\n"
    "except ImportError:  # the project runs on psycopg2 too\n"
    "    import psycopg2 as psycopg\n"
    "import pytest\n"
)
RAW_QUERY_BEFORE = '        tickets = raw.execute("SELECT count(*) FROM shop_ticket").fetchone()[0]\n'
RAW_QUERY_AFTER = (
    '        cursor = raw if hasattr(raw, "execute") else raw.cursor()\n'
    '        cursor.execute("SELECT count(*) FROM shop_ticket")\n'
    "        tickets = cursor.fetchone()[0]\n"
)
BOUNDS_BEFORE = '    assert active.bounds == "[)"\n'
BOUNDS_AFTER = (
    '    # psycopg2 spells the bounds out as the flags of the two ends\n'
    '    assert getattr(active, "bounds", None) in (None, "[)")\n'
    "    assert (active.lower_inc, active.upper_inc) == (True, False)\n"
)


def hash_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def captures(work: Path) -> dict[str, str]:
    """Every capture file of the project, keyed by its path in the project."""
    return hash_tree(work / "captures") | {
        f"tests/captures/{name}": digest
        for name, digest in hash_tree(work / "tests" / "captures").items()
    }


def patch(work: Path, relative: str, before: str, after: str) -> None:
    path = work / relative
    text = path.read_text()
    if before not in text:
        raise SystemExit(f"{relative}: the text to patch is not there:\n{before}")
    path.write_text(text.replace(before, after))


def prepare(work: Path) -> Path:
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(
        PROJECT, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "captures", "*.pyc")
    )
    patch(work, "tests/test_no_database.py", RAW_IMPORT_BEFORE, RAW_IMPORT_AFTER)
    patch(work, "tests/test_no_database.py", RAW_QUERY_BEFORE, RAW_QUERY_AFTER)
    patch(work, "tests/test_orm_postgres_values.py", BOUNDS_BEFORE, BOUNDS_AFTER)
    return work


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=4, help="how often to run the suite (default 4)")
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK, help="work directory (default %(default)s)")
    parser.add_argument("--pgdata", type=Path, default=Path("/tmp/capquery-pgdata"), help="postgres data dir")
    args = parser.parse_args()

    import django
    import pgserver

    try:
        import psycopg  # noqa: F401

        print("! psycopg3 is importable in this interpreter: Django would use it, not psycopg2")
        return 2
    except ImportError:
        pass
    print(f"driver: Django {django.__version__} with psycopg2 (psycopg3 absent)")

    pgserver.get_server(str(args.pgdata), cleanup_mode=None)
    work = prepare(args.work)
    print(f"work directory: {work}\n")

    env = dict(
        os.environ,
        CAPQUERY_VERIFY_DB_HOST=str(args.pgdata),
        CAPQUERY_VERIFY_DB_PORT="",
        CAPQUERY_VERIFY_DB_USER="postgres",
        CAPQUERY_VERIFY_DB_PASSWORD="",
        CAPQUERY_VERIFY_DB_NAME="capquery_verify_psycopg2",
    )

    failures: list[str] = []
    baseline: dict[str, str] | None = None
    managed = 0
    for number in range(1, args.runs + 1):
        # a different hash seed per run, like run.py
        env["PYTHONHASHSEED"] = str(number)
        returncode, output = run_pytest(work, env, number)
        tree = captures(work)
        lines = [line for line in output.splitlines() if line.startswith("capquery:")]
        print(f"--- run {number}: exit code {returncode}, {len(tree)} capture file(s) ---")
        for line in lines:
            print(f"    {line}")
        if returncode != 0:
            print("    ! " + "\n    ! ".join(output.splitlines()[-20:]))
            failures.append(f"run {number} exited {returncode}")
        if number == 1:
            baseline = tree
            managed = _managed_tests(lines)
            check_creation(output, tree, managed, failures)
        else:
            changed = [name for name in sorted(set(baseline) | set(tree)) if baseline.get(name) != tree.get(name)]
            if changed:
                failures.append(f"run {number}: {len(changed)} capture file(s) changed: {', '.join(changed[:5])}")
                print(f"    ! captures changed: {', '.join(changed[:5])}")
            else:
                print("    (captures byte-identical to run 1)")
            check_replay(output, failures, number)
        print()

    print("=" * 78)
    if failures:
        print("psycopg2: FAIL")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(
        f"psycopg2: PASS — {args.runs} runs of {managed} managed tests, "
        f"{len(baseline or {})} capture files, byte-identical after the recording run"
    )
    return 0


def run_pytest(work: Path, env: dict, number: int) -> tuple[int, str]:
    env = dict(env, CAPQUERY_VERIFY_EXPECT_TICKETS="5" if number == 1 else "0")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _managed_tests(lines: list[str]) -> int:
    for line in lines:
        if "database test(s) for " in line:
            return int(line.split(" for ")[1].split()[0])
    return 0


def check_creation(output: str, tree: dict[str, str], managed: int, failures: list[str]) -> None:
    """Nothing may be silently uncapturable: run 1 records every managed test."""
    if f"captures {managed} created" not in output:
        failures.append(f"run 1 did not create one capture file per managed test ({managed})")
    if "was not captured" in output:
        for line in output.splitlines():
            if "was not captured" in line:
                failures.append(f"run 1 reports a test it cannot capture: {line.strip()}")


def check_replay(output: str, failures: list[str], number: int) -> None:
    """Every statement of runs 2..n is answered from the captures, phase included."""
    if "0 missed, 0 executed against postgres" not in output:
        failures.append(f"run {number}: a statement still went to postgres")
    if "captures 0 created, 0 updated" not in output:
        failures.append(f"run {number}: a capture file was written")
    phase = output.split("migrations:", 1)[-1].splitlines()[0] if "migrations:" in output else ""
    if "replayed" not in phase:
        failures.append(f"run {number}: the migration phase was not replayed")


if __name__ == "__main__":
    sys.exit(main())
