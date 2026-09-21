# pytest-django-capquery

`capquery` (short for *capture query*) is a pytest plugin for Django projects on
postgres. It records the statements a test sends to the database together with
their results — reads and writes alike — and on the next run it answers them from
the captures instead of the database.

```text
run 1:  test -> postgres -> result   (captures written to tests/captures/...)
run 2:  test -> captures -> result   (postgres is not touched at all)
```

A replayed `INSERT ... RETURNING id` yields the id it returned, a replayed
`UPDATE` or `DELETE` yields the number of affected rows, and a replayed `SELECT`
yields its rows. The plugin is meant for large suites where the same hundreds of
statements are executed on every run of every merge request.

## Requirements

* Python >= 3.10
* Django 4.2 LTS / 5.x
* pytest >= 7, pytest-django >= 4.5
* postgres (the only supported backend)
* dependencies: `pytest-django`, `PyYAML`

The library does not talk to the database on its own: it sits on top of the
connection that pytest-django prepares.

## Installation

```console
pip install pytest-django-capquery
```

Nothing to enable in `conftest.py`: the plugin is registered through the
`pytest11` entry point.

## How it works

### Interception

`connection.execute_wrapper` cannot be used for a replay: the value a wrapper
returns is discarded and the caller always reads rows from the real cursor
(`CursorWrapper._execute_with_wrappers` returns the result of `_execute`). So the
plugin patches `django.db.backends.utils.CursorWrapper` itself:

* `execute` / `executemany` — look the statement up in the captures (replay) or
  record it (capture);
* `fetchone` / `fetchmany` / `fetchall` / `__iter__` — serve the rows of a
  replayed statement from the buffer;
* `description` and `rowcount` — describe the buffered result.

`CursorDebugWrapper` inherits from `CursorWrapper` and overrides none of these
methods, so it is covered as well.

Every *data* statement is captured and replayed: `SELECT`, `WITH ... SELECT`,
`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`, `VALUES`, `TABLE` — and
`executemany` as a whole. Next to the rows, the record stores the row count, which
is what Django returns from `QuerySet.update()` and `QuerySet.delete()`.

Two groups are never captured; they are executed against postgres every run:

* **schema DDL** — `CREATE` / `ALTER` / `DROP TABLE` and friends. The test database
  is really created and really gets the schema of the project, so a statement that
  is not in the captures (a new or edited test) still finds a usable database;
* **everything else that is not a data statement** — transaction control
  (`SAVEPOINT`, `RELEASE`, `SET CONSTRAINTS`), maintenance (`VACUUM`, `ANALYZE`),
  administration and anything the plugin does not recognise. Being conservative
  here is on purpose: an unknown statement is never replayed, so it cannot silently
  lose its side effect.

Django's own migration bookkeeping (`django_migrations`) belongs to the second
group even though it is an ordinary `INSERT`: it records *when* a migration was
applied, so its parameter changes on every run and could never be replayed. Its
`SELECT`s are still replayed like any other read.

Because writes are replayed, the database does **not** contain what the captures
say while a replayed test runs. That is the point (no round trip, no transaction
of the test touching the data), but it has consequences: a query that is *not* in
the captures runs against a database the replayed writes never touched. The
plugin refuses to record anything in that situation and asks for a session with a
real migration phase instead — see *Behaviour rules* and the terminal summary.

### Modes of one test

| Situation | Mode |
| --- | --- |
| no capture file yet | capture: every data statement goes to postgres, the result is written |
| capture file exists | replay: every data statement is answered from the file |
| a statement of the test was not found in the file (a *miss*) | the statement goes to postgres, the test is retried in capture mode |
| the test failed while using the captures | the test is retried in capture mode |
| test marked `capquery_ignore`, or marked unstable | passthrough: no capture, no replay |

### Behaviour rules

1. A test without captures gets them after a successful run.
2. A test whose captures do not cover all of its queries (a query went to the
   database) is retried; if the retry passes, the captures are regenerated.
3. A test whose captures exist but which failed is retried; if the retry passes,
   the captures are regenerated. The retry runs without the cache, so it cannot
   be masked by stale captures. Retrying happens only when the test actually used
   the cache (`hits > 0`: a failure before the first replayed query cannot be
   caused by the captures), otherwise a genuinely broken test would be executed
   three times on every run.
4. Only the reports of the final attempt are reported, so a retry that succeeds
   does not produce a failure in the output.
5. A capture is never recorded against a database that did not get what the
   migrations write. When a retry is needed in a session whose migration phase was
   answered from the captures, the phase is redone for real first (`--reuse-db`
   keeps the databases, so this is a fresh `CREATE DATABASE` plus a real `migrate`).
   If that is not possible, the captures are left alone, the test is listed in the
   summary and the next run does the migrations for real — nothing is ever
   overwritten with the answers of a half-set-up database.

### The migration phase decides how a test may run

Because a replayed migration phase does not write the seed rows to the database,
the plugin asks at collection time whether anything in the session has to see the
real database: a test without captures yet, an unstable test, or a test marked
`capquery_ignore` (it never goes through the plugin, so its queries are real).
When that is the case the phase is executed — and recorded — for real, and the
`migrations:` line says why. A session in which every test is replayed keeps the
phase in the captures, seeds included.

A test that issues no capturable statement at all (`test_without_queries` below) is
remembered in the state file, so it does not keep the phase real forever.

### Regeneration bookkeeping

The number of consecutive regenerations is stored in
`<rootdir>/captures/.capquery-state.yaml`:

```yaml
tests/tests/test_orders.py::test_orders:
  attempts: 1
  unstable: false
  requires_real: false
  captures_nothing: false
```

`requires_real` is raised on the migration phase (and on the test that asked for it)
when a run had to touch the database while the phase came from the captures; the
next session then does the migrations for real. `captures_nothing` marks a test
that produced no capture at all, so it is not treated as "a test without captures"
that needs the real database.

When the limit (`--capquery-max-attempts`, default 3) is reached the test is
marked `unstable`: its capture file is deleted, it is listed in the terminal
summary and from then on it always runs against the real database. The counter is
reset as soon as a session replays the test without regenerating anything;
deleting the file resets everything.

## Configuration

| Meaning | CLI | ini | environment | default |
| --- | --- | --- | --- | --- |
| enable only when more than N database tests are collected | `--capquery-min-tests=N` | `capquery_min_tests` | `CAPQUERY_MIN_TESTS` | 5 |
| how many regenerations a test may have | `--capquery-max-attempts=N` | `capquery_max_attempts` | `CAPQUERY_MAX_ATTEMPTS` | 3 |

Priority: CLI > environment > ini. With five or fewer database tests the plugin
is completely inactive (no capture, no replay) — there is nothing to win for a
handful of tests.

```ini
# pytest.ini
[pytest]
capquery_min_tests = 50
```

*Database tests* are the collected tests that use the `db` or `transactional_db`
fixture, after `-k` / `-m` are applied.

## Capture files

Captures are stored next to the tests they belong to: one yaml file per test,
mirroring the path of the test module, with the test name (and the parameter id
of a parametrized test) as the file name:

```text
<rootdir>/tests/captures/shop/test_orders.py/test_orders.yaml
```

A test module that lives in the rootdir keeps its captures in
`<rootdir>/captures/`. Test names are sanitized, so two tests can never collide.

```yaml
- hash: 4f0b7c1c...   # sha256 of (sql, params)
  n: 0                # sequence number of this execution of the same (sql, params)
  sql: SELECT "shop_order"."id", "shop_order"."name" FROM "shop_order" WHERE "shop_order"."id" = %s
  params: [{t: int, v: 1}]
  rowcount: 1         # rows of a SELECT, affected rows of a write
  columns: [id, name]
  rows:
    - [{t: int, v: 1}, {t: str, v: first}]
```

Writes look exactly the same, with their `RETURNING` rows:

```yaml
- hash: 90864b49...
  n: 0
  sql: INSERT INTO "shop_order" ("name", "amount") VALUES (%s, %s) RETURNING "shop_order"."id"
  params: [{t: str, v: fresh}, {t: decimal, v: '1.00'}]
  rowcount: 1
  columns: [id]
  rows:
    - [{t: int, v: 4}]
```

Values are typed (`{t: <type>, v: <value>}`) so that a replay returns exactly the
same Python objects the test compared during the recording run: `int`, `bool`,
`float`, `str`, `decimal`, `date`, `time`, `datetime`, `timedelta`, `uuid`,
`bytes` (base64), `list`, `tuple`, `dict`, `None`. `NaN` / `Infinity` are stored
as strings.

A subclass of one of these types is stored as the base type: Django's
`models.TextChoices` and `models.IntegerChoices`, `enum.StrEnum` and driver
scalars are subclasses, and PyYAML refuses a subclass of a builtin outright. A
member of a choice field is therefore written as the value postgres received
(`{t: str, v: draft}`) — not as the display form the enum prints, and not as the
object itself. A value the plugin cannot type (say a custom adapter object) makes
the test uncapturable: it is reported in the summary and runs against the
database. A capture that cannot be written is treated the same way, so a value
the plugin cannot store never ends the session: the previous captures of that
test are kept.

The hash is computed as

```text
sha256(sql + b"\x00" + stable_json(params))
```

The idea of hashing the `(sql, params)` pair comes from
[django-cacheops](https://github.com/Suor/django-cacheops)
(`cacheops/query.py:_cache_key` hashes md5 of the SQL with the parameters
applied); no code is copied and cacheops is not a dependency.

### Repeated executions of the same query

If a test executes the same `(sql, params)` several times and gets different rows
(for instance a `SELECT` before and after an `INSERT`), the results are stored as
a list in execution order and the N-th execution is answered with the N-th stored
result.

### In-memory sqlite

At session start every capture file is read once into an in-memory sqlite
database.  sqlite is the source of truth, and a dictionary index next to it
spares the plugin a query plus a json decode for every single replayed
statement:

```sql
CREATE TABLE captures (
    ctx TEXT, hash TEXT, n INTEGER, sql TEXT, params TEXT, rowcount INTEGER,
    columns TEXT, rows BLOB,
    PRIMARY KEY (ctx, hash, n)
)
```

The SQL text is kept for diagnostics only — lookups are by hash.

## Migrations

The whole database setup phase is captured into
`<rootdir>/captures/migrations.yaml` and replayed on the next run, exactly like the
statements of a test: the reads of the executor, and the `INSERT`s of the seed
migrations with them. Schema DDL is never replayed — the test database is really
created and really gets the schema of the project, so a statement that is not in
the captures still finds a usable database. The consequence is that a replayed
session runs the tests against a database whose tables exist but are empty; the
captures are what the tests see.

The phase that is captured is Django's `setup_databases` call, not just
`MigrationExecutor.migrate`: the `migrate` command emits the post migrate signal
(content types, permissions) *after* the executor returns, and pytest-django runs
the whole database setup inside the protocol of the first test of the session.
Capturing only the executor would put those statements into that test's capture
file, and the next run would then see a miss there — a miss means a retry, and a
retry means the test runs a second time against the database rather than the
captures.

Before the first `migrate` of a session the sequences of the test database are
reset with `sqlsequencereset`, so that the ids a *recording* run sees start from 1
on every run and captures stay stable. On a database created from scratch there is
nothing to reset yet (the tables do not exist) — that case is handled silently.

The post migrate signal (Django's content types and permissions) is never captured
and always executed for real: it builds its `IN (...)` parameter lists from a
`set`, so the same statement carries differently ordered parameters in every
process and could never be replayed.

If a replay of the migration phase misses a query, the phase cannot be trusted
(the database state may differ from the state the cache was recorded in), so the
test databases are dropped, re-created and migrated again with the cache off, and
`migrations.yaml` is rewritten from that clean run. Repeated misses over sessions
make the phase `unstable` through the usual attempt counter, after which
migrations are simply executed for real.

### Problems of caching the migration phase (researched)

* **The phase is not a test.** Everything that queries the database while the
  test database is being prepared (the executor, the post migrate signal, the
  introspection of `sqlsequencereset`) has to be captured under the migration
  context, otherwise it leaks into the captures of the first test and makes them
  depend on the state of the database instead of the state of the test.

* **Reading `django_migrations` from the cache.** The executor decides which
  migrations to apply by reading `django_migrations`. A cached answer is only
  correct for the state it was recorded in, i.e. for a database created from
  scratch. That is why the phase is replayed only when the test database is
  created from scratch, and why the plugin asks postgres whether
  `test_<database>` already exists (`pg_database`) instead of trusting
  `--reuse-db`: with an existing database the cached reads would make the
  executor skip migrations that are actually missing, or try to create a
  `django_migrations` table that is already there.
* **A miss cannot be repaired in place.** A partially replayed phase mixes cached
  and real reads, so the resulting schema/data state is not guaranteed to be the
  same as in an uncached run — hence the "redo the whole phase from scratch"
  rule.
* **The sequence reset must happen before the migrations**, but before the
  migrations the tables do not exist yet on a fresh database. The plugin runs the
  command at the very start of the phase: on a reused database it does something
  useful, on a fresh one it is a no-op.
* **The reset and the introspection queries of the command itself are not
  captured** (the interceptor is suspended while they run), otherwise a replayed
  `sqlsequencereset` would be answered from the cache and would not reset
  anything, and the introspection SELECTs would pollute `migrations.yaml`.

## What is in the terminal summary

```text
capquery: 42 database test(s) for 42 managed test(s)
capquery: statements: 3150 replayed, 0 missed, 0 executed against postgres, 1296 always sent (schema, transaction control)
capquery: migrations: 78 statement(s) replayed, 0 missed, 0 recorded
capquery: sequences reset: 0, skipped (no table yet): 8
capquery: captures 2 created, 1 updated, 0 unchanged, 0 deleted
capquery: retried tests: tests/test_orders.py::test_create (2 attempts)
capquery: unstable tests (captures removed): tests/test_orders.py::test_random
```

The `statements:` line is the honest measure of a replay: `missed` and `executed
against postgres` are zero when every data statement of every test was answered
from the captures, and `always sent` counts the DDL and the transaction control of
that session (it is never zero — the schema is really built).

## Benchmark

`tests/test_benchmark.py` (run with `pytest -m benchmark -s`) times two loops
inside the demo project: 400 aggregated reads over 2000 rows, and 200
create/update/delete cycles (600 statements).

```text
400 reads over 2000 rows: postgres 302 ms, captures 107 ms (2.8x faster)
200 create+update+delete cycles (600 statements): postgres 258 ms, captures 104 ms (2.5x faster)
```

The plugin pays a small CPU cost per statement (hash, lookup, decode) and the
loops above are mostly Python work of the ORM, so the win comes from the work the
server does not have to do: a cheap `SELECT` on a tiny table is dominated by the
Python side and will show little improvement, while anything that plans, scans,
aggregates or writes will.

## Limitations

* postgres only;
* `pytest-xdist` (`-n`) is not supported: the plugin disables itself with a
  message, because workers would write the same capture files;
* the database is not modified by a replayed test, so a statement that is not in
  the captures (or a test that never had them) runs against a database whose writes
  were skipped. The plugin handles that by asking for a real migration phase (see
  above) rather than by recording the result, but a suite that mixes replayed and
  non-replayed tests pays for a real phase;
* a test that writes a value which differs on every run (`auto_now_add`, a `uuid4`
  default, `timezone.now`) never matches its capture: the statement counts as a
  miss, so the test is regenerated a few times and then marked unstable. Recording
  such a test is pointless by nature — the values it sends are new every time;
* a statement is never replayed if it is not a data statement or if it holds more
  than one statement (a `;` in the middle); such statements are simply executed;
* a replayed `SELECT` is fully materialised in memory (Django does not use
  server-side cursors in the supported versions, so this matches what the ORM
  does anyway);
* with `DEBUG = True` / `force_debug_cursor` the SQL text of a replayed query is
  not available to `last_executed_query`, so the query log shows the statement
  without its text; query counting still works;
* an expected `IntegrityError` (or any other error from the database) cannot be
  replayed: only successful statements are captured, so on a replay run the
  statement succeeds instead of raising. Mark such tests with `capquery_ignore`;
* tests with `transaction=True` are captured like any other, but they truncate
  tables between tests; if that turns out to make captures unstable, mark them
  with `capquery_ignore`;
* async tests and multiple databases (`db_alias`) are not covered by the test
  suite yet — a capture context is keyed by test (or by the migration phase), not
  by alias;
* the plugin hashes and decodes every captured statement, so a replay of a very
  cheap query is not necessarily faster than the database; see the benchmark.

## Debugging

* `CAPQUERY_TRACE=/tmp/capquery.log` appends every statement the plugin saw:
  `REC` (recorded), `REPLAY` (served from the captures), `MISS` (not found,
  executed on postgres), `SYSTEM` (never captured: DDL, transaction control) and
  `UNSUPPORTED` (holds a value the plugin cannot type);
* deleting `<rootdir>/captures/.capquery-state.yaml` forgets the regeneration
  counters, including the tests that were marked unstable;
* the integration tests keep the demo project they ran in when they fail, so the
  inner output and the captures can be inspected.

## Development

```console
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]" pytest
uv pip install --python .venv/bin/python pgserver   # embedded postgres for the integration tests
.venv/bin/python -m pytest -m "not benchmark"
```

Tests are split in two halves: `tests/test_*.py` for the plugin's own units, and
the integration tests which drive `tests/demo_project` (a real Django project with
a data migration) through `pytester` — a recorded run, a replayed run, a broken
capture, a changed query, a query-less test, an ignored test, a test that never
settles, `--reuse-db`, `-n`, the settings priority and the benchmark.

The demo suite is the specification of what a replay has to reproduce: reads of
every value type, `UPDATE`/`DELETE` row counts, `bulk_create`, a raw
`INSERT ... RETURNING`, `executemany`, and one test that reads the table with a
plain driver instead of Django — it proves that the database really was not
touched, while the Django tests keep seeing the rows of the seed migration.

The integration tests and the benchmark need a postgres server. They use the
`pgserver` wheel when it is installed (an embedded postgres that needs no
root rights); otherwise set `CAPQUERY_TEST_DB_HOST`, `CAPQUERY_TEST_DB_PORT`,
`CAPQUERY_TEST_DB_USER`, `CAPQUERY_TEST_DB_PASSWORD` to point at an existing
server.  Each integration test works in its own database (`test_capquery_*`),
which is dropped before the test starts.

`.dev/demo_run.py` starts the embedded postgres and runs pytest on the demo
project by hand, which is the quickest way to look at the plugin's output while
working on it:

```console
.venv/bin/python .dev/demo_run.py -q -s tests/demo_project
```

```console
.venv/bin/python -m pytest -m benchmark -s
```

## License

MIT
