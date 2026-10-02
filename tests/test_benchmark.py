"""Benchmark: a replayed run does not pay for the reads or for the writes.

The load test is written into the demo project at run time so that the committed
captures of the demo stay small.  It times two loops inside the test — aggregated
reads and a create/update/delete cycle — and writes the numbers to a file, so the
benchmark compares the statements themselves instead of the startup of Django.
Run it with ``pytest -m benchmark -s``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from helpers import run_demo, summarize

READS = 400
ROWS = 2000
WRITES = 200

LOAD_TEST = f'''
import time
from pathlib import Path

from demo.shop.models import Order

READS = {READS}
ROWS = {ROWS}
WRITES = {WRITES}


def test_load(db):
    Order.objects.bulk_create(
        [Order(name=f"row-{{index}}", amount=1) for index in range(ROWS)], batch_size=500
    )

    started = time.monotonic()
    for _ in range(READS):
        assert Order.objects.filter(name__startswith="row-").count() == ROWS
    reads = time.monotonic() - started

    started = time.monotonic()
    for index in range(WRITES):
        order = Order.objects.create(name=f"write-{{index}}", amount=index)
        Order.objects.filter(id=order.id).update(amount=index + 1)
        assert Order.objects.filter(id=order.id).delete()[0] == 1
    writes = time.monotonic() - started

    Path("load_seconds.txt").write_text(f"{{reads:.6f}} {{writes:.6f}}")
'''

ARGS = ("tests/test_benchmark_load.py", "--capquery-min-tests=0")


def clear_captures(demo: Path) -> None:
    shutil.rmtree(Path(demo) / "captures", ignore_errors=True)
    shutil.rmtree(Path(demo) / "tests" / "captures", ignore_errors=True)


def run_load(pytester, demo: Path, *args) -> tuple[object, tuple[float, float]]:
    result = run_demo(pytester, *ARGS, *args)
    reads, writes = (Path(demo) / "load_seconds.txt").read_text(encoding="utf-8").split()
    return result, (float(reads), float(writes))


@pytest.mark.benchmark
def test_a_replayed_run_is_faster_than_a_run_against_postgres(pytester, demo, demo_env):
    demo_env("capquery_benchmark")
    (Path(demo) / "tests" / "test_benchmark_load.py").write_text(LOAD_TEST, encoding="utf-8")

    record: list[tuple[float, float]] = []
    replay: list[tuple[float, float]] = []
    for _ in range(2):
        clear_captures(demo)
        record_result, record_seconds = run_load(pytester, demo, "--create-db")
        assert record_result.ret == 0, record_result.stdout
        record.append(record_seconds)

        replay_result, replay_seconds = run_load(pytester, demo, "--create-db")
        assert replay_result.ret == 0, replay_result.stdout
        summary = summarize(replay_result)
        # the whole point: not one statement of the load test reached postgres
        assert summary.missed == 0, str(summary)
        assert summary.executed_postgres == 0, str(summary)
        assert summary.replayed >= READS + 3 * WRITES, str(summary)
        replay.append(replay_seconds)

    best_record = (min(item[0] for item in record), min(item[1] for item in record))
    best_replay = (min(item[0] for item in replay), min(item[1] for item in replay))
    print(
        f"\n{READS} reads over {ROWS} rows: postgres {best_record[0] * 1000:.0f} ms, "
        f"captures {best_replay[0] * 1000:.0f} ms ({best_record[0] / best_replay[0]:.1f}x faster)"
    )
    print(
        f"{WRITES} create+update+delete cycles ({3 * WRITES} statements): "
        f"postgres {best_record[1] * 1000:.0f} ms, captures {best_replay[1] * 1000:.0f} ms "
        f"({best_record[1] / best_replay[1]:.1f}x faster)"
    )
    # a generous margin: the point is to catch a replay that stops being faster at
    # all, not to measure the machine (the loops are also mostly Python work)
    assert best_replay[0] < best_record[0] * 0.8, (
        f"the replayed reads took {best_replay[0] * 1000:.0f} ms, "
        f"postgres {best_record[0] * 1000:.0f} ms"
    )
    assert best_replay[1] < best_record[1] * 0.8, (
        f"the replayed writes took {best_replay[1] * 1000:.0f} ms, "
        f"postgres {best_record[1] * 1000:.0f} ms"
    )
