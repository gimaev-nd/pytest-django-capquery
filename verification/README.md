# Verification: does capquery answer a replayed run with what it recorded?

An independent Django project with the postgres-only column types, its suite run several
times through the plugin, and a check of the one property the library promises: *the
capture files of a replayed run do not change*.

```console
.venv/bin/python verification/run.py                 # 5 runs + control + probe session
.venv/bin/python verification/run.py --runs 3 --show-log
.venv/bin/python verification/run.py --no-control --no-probe   # only the replay path
```

The same project is also run with **psycopg2** instead of psycopg 3, because Django
4.2/5.x runs on either driver and they prepare the postgres-only values differently:

```console
uv venv .venv-pg2 --python 3.12
uv pip install --python .venv-pg2/bin/python -e ".[dev]" psycopg2-binary pgserver
uv pip uninstall --python .venv-pg2/bin/python psycopg
.venv-pg2/bin/python verification/run_psycopg2.py     # 4 runs of the same suite
```

`run.py` starts the embedded postgres (`pgserver`), copies `project/` into a work
directory, runs the suite there `--runs` times and compares the SHA256 of every capture
file after each run.  It reads the plugin's own report *and* the postgres server log
(`log_statement = 'all'`, sliced between runs), so "postgres was not touched" is not taken
on the plugin's word — and a raw psycopg connection (outside Django, outside the plugin)
reports what really is in the table while the tests "see" the rows the seed migration
wrote.

The project is deliberately not the one in `tests/demo_project`: this one is a plain
Django project, the models carry `text[]`/`int[]`, `jsonb`, `daterange`/`tstzrange`/
`int4range`, `inet`, `bytea` and a `tsvector` with GIN indexes, and the tests use the
queries that are specific to postgres — `DISTINCT ON`, array containment and overlap,
json key lookups, range lookups, full text search, a scalar subquery, `EXISTS`,
`GROUP BY ... HAVING`, window functions, `UNION`, `CASE`, filtered aggregates — next to
the ordinary write paths and 15 tests that use the ORM the way a project does
(`Ticket.objects.get/create/delete`, `filter(meta__contains=…)`,
`filter(active__overlap=(a, b))`, `filter(peer=…)`).

## Result

```
48 checks, 5 runs of 77 managed tests, 79 capture files                 PASS
  run 1: 126 statements executed against postgres, 77 captures created
  run 2..5: 126 statements replayed, 0 missed, 0 executed against postgres,
            0 captures written, migration phase replayed (21 statements), exit code 0
  every capture file byte-identical to run 1 (state file included)
  postgres log of runs 2..5: no read and no write of the application's tables
  the raw psycopg connection sees 0 rows in shop_ticket while the tests see the seeds
  control step: changing one query is noticed, the test is retried, exactly that one
  file changes, and the captures settle again two runs later
  probe session: 8 tests that hold a value capquery cannot store are reported, write no
  capture file, and the migration phase they force is byte-identical across two sessions
```

`run_psycopg2.py` runs the same suite on the other driver Django 4.2/5.x supports:

```
77 managed tests, 79 capture files, runs 1..4                          PASS
  run 1: 77 captures created, 126 statements executed against postgres, nothing uncapturable
  runs 2..4: 126 statements replayed, 0 missed, 0 executed against postgres, 0 captures
             written, migration phase replayed (21 statements), exit code 0
  every capture file byte-identical to run 1
```

The suite is run with a different `PYTHONHASHSEED` per run, so a statement whose
parameters are ordered by a `set` would show up as a miss instead of a silent pass.

## How the checks avoid passing by accident

* **Non-vacuous**: run 1 has to create exactly one capture file per managed test
  (77/77), so "nothing changed" cannot mean "nothing was captured".
* **Control step**: the script changes one query of one test
  (`labels__contains=["bug"]` → `["cache"]`), and the plugin has to *notice*: the
  statement misses, the test is retried (`(2 attempts)`), the run passes, exactly that
  one capture file changes, and two runs later every file is identical again.
* **Independent evidence**: the postgres log and a raw psycopg connection, both outside
  Django and outside the plugin.
* **A separate probe session**: tests that hold a value which cannot be stored at all
  must still run, be reported, and leave nothing behind.

## Defects this project found, and their fixes

### 1. `captures/migrations.yaml` changed on every run that recorded the phase

The phase captures the executor's read of `django_migrations`, and that read returns the
`applied` timestamps of *this* run — so any session in which something runs against the
database (a test without captures, an unstable test, a `capquery_ignore` test, a test
whose values cannot be stored) rewrote the file, for ever, with a one-line diff of
timestamps:

```
$ pytest probe; cp captures/migrations.yaml /tmp/one
$ pytest probe; cp captures/migrations.yaml /tmp/two
$ diff /tmp/one /tmp/two
281,296c281,296
<       - [1, contenttypes, 0001_initial, '2026-09-22T08:34:25.226307+00:00']
>       - [1, contenttypes, 0001_initial, '2026-09-22T08:34:28.641025+00:00']
```

Any project with one such test had `captures/migrations.yaml` in every merge request —
the pollution the plugin exists to prevent — and the phase was really executed (seeds
included) in every one of those sessions.

A second, related instability showed up when the phase was re-recorded **inside a session
that already talked to postgres** (which is what a regeneration does): the phase then did
not issue `psycopg`'s own type lookups

```
SELECT oid, typarray FROM pg_type WHERE typname = %s      -- for hstore / citext
```

so the file lost those two statements; the next session, started from a cold process, did
issue them, missed them in the capture, and redid the phase for real again — every
session, alternating the file.

**Fixed** by a fixed clock for the bookkeeping (`capquery.migration_time`: the first
migration of a session is "applied" on 2000-01-01 00:00:00 UTC, every next one a second
later, and the normalized rows are handed to the caller as well) and by classifying the
driver's oid lookups as never cached.  Checks: `probe: two probe sessions leave the phase
capture alone` and `control: the captures settle again after the regeneration`.

### 2. Values Django prepares for postgres-only columns could not be stored, silently

`probe/` used to contain the code a normal project writes, and every one of its
statements carried a value capquery's encoder rejected, because Django hands the driver
an adapter object instead of a plain value:

| Query | Parameter or row the driver receives | was |
| --- | --- | --- |
| `create(meta={...})`, `filter(meta__contains={...})`, `filter(meta={...})`, `update(meta=...)` | `psycopg.types.json.Jsonb` | cannot store |
| `filter(meta__team="core")`, `filter(meta__level__gte=2)` | `Jsonb` (the compared value) | cannot store |
| `filter(peer="10.0.0.1")` | `ipaddress.IPv4Address` | cannot store |
| `filter(active__overlap=(a, b))`, `filter(weight__contained_by=(a, b))` | `psycopg.types.range.Range` | cannot store |
| `values_list("active", flat=True)`, `Ticket.objects.get(...)`, `filter(...).delete()` | rows hold `Range` (`inet` is read as text, so it was fine) | cannot store |

A model with a `JSONField` could not be inserted through the ORM at all, and the plugin
said nothing: the decision `"unsupported"` fell through every branch of the retry loop, so
`_note_unsupported()` — the only thing that fills in the summary line the README promises
— was never reached.  Only `CAPQUERY_TRACE=<file>` showed the reason.

**Fixed** by teaching the encoder what those objects stand for (the payload of a JSON
wrapper, the text of an `ipaddress`, the bounds of a `Range` as a new `range` value whose
schema carries the bounds' own types) and by adding the missing branch that reports the
test.  The 15 tests moved into `tests/test_orm_postgres_values.py`: they are part of the
main suite now, and `run 1 created one capture per managed test` fails if any of them
stops being capturable.

### 3. psycopg2's range and `inet` values were not recognized

Django 4.2/5.x runs on psycopg2 as well, and that driver wraps the postgres-only values in
its own objects instead:

| Query | Parameter or row the driver receives | was |
| --- | --- | --- |
| `filter(peer="10.0.0.1")`, `create(peer=…)` | `psycopg2.extras.Inet` | cannot store |
| `filter(active__overlap=(a, b))`, `create(active=…)` | `psycopg2._range.DateRange` | cannot store |
| `filter(weight__contained_by=(a, b))` | `psycopg2._range.NumericRange` | cannot store |
| `Ticket.objects.get(...)`, `values_list("active", flat=True)` | rows hold `psycopg2._range.DateRange` | cannot store |

psycopg2's range classes have no public `bounds` — a private `_bounds` and the flags
`lower_inc` / `upper_inc` — so the check that decides "this object is a range" (a public
`bounds` holding one of the four postgres flags) never matched, and `Inet` is not an
`ipaddress` object either.  Every test of the project that touched a range or an address was
therefore reported (`was not captured`) and ran against postgres in every session — and
because such a session always has a test that really goes to the database, the migration
phase could never be replayed either.  An earlier run of `run_psycopg2.py`: 71 capture
files, 8 tests uncapturable, `migrations: … 21 recorded`.

**Fixed** by reading the bounds the way the driver that wraps them spells them
(`capquery.values._bounds_of`: the public `bounds` of psycopg, or the two flags of psycopg2)
and by recognizing the driver's own `inet` adapter (`capquery.values._ipaddress_text`:
`Inet.addr` is the text the server receives, a netmask included).  A capture decoded where
psycopg 3 is not installed is rebuilt as psycopg2's *generic* `Range`, which compares equal
to its `DateRange`/`NumericRange` (`_range_class`).  `run_psycopg2.py` checks it: 77 of 77
managed tests captured, then 126 statements replayed, 0 missed, 79 capture files
byte-identical.

### 4. What is still not cacheable

A value no capture describes: a project's own type that arrives with a dumper of its own
(the `Pixel` of the probe module), or a postgres type the driver loads into something the
plugin has no representation for (a multirange).  The probe session checks that this
failure mode stays safe (the test runs, pytest exits 0, no capture file appears) and
visible (the summary names the test and the type):

```text
capquery: probe/test_uncapturable_values.py::test_a_parameter_of_an_unknown_type was not captured: it holds values capquery cannot store
capquery: probe/test_uncapturable_values.py::test_a_parameter_of_an_unknown_type: capquery cannot store values of type test_uncapturable_values.Pixel
capquery: probe/test_uncapturable_values.py::test_a_result_of_an_unknown_type: result of "SELECT '{[1,3),[5,8)}'::int4multirange" holds a value capquery cannot store
```

`tasks/2.draft.md` is about the related, bigger case: values that are *new on every run*
(`auto_now_add`, `uuid4`, `DEFAULT now()`), which today cost a retry, a real migration
phase and a rewritten capture file before the test is marked unstable.

## Smaller observations

* On a REPLAY run the plugin reports `captures 0 created, 0 updated, 0 unchanged`:
  `unchanged` counts the saves whose content already matched, and a replay saves nothing.
  It is not evidence that nothing changed — the file hashes are.
* `Ticket.objects.filter(...)` in a *failing* assertion makes pytest evaluate the queryset
  for the failure message, which issues a full-row `SELECT ... LIMIT 21`.  On a model with
  postgres-only columns that statement had to be made storable too, or a failing test
  would turn itself into an uncapturable one.
* `TRUNCATE` of a table that the same transaction wrote fails in postgres
  ("cannot TRUNCATE ... because it has pending trigger events") — a property of
  `TestCase`'s transaction, not of the plugin; the suite truncates another table.

## Layout

```
verification/
  run.py                       orchestrator: runs the project, hashes captures, compares
  run_psycopg2.py              the same runs on psycopg2 (a venv without psycopg 3)
  project/
    pytest.ini                 DJANGO_SETTINGS_MODULE=demo.settings
    demo/settings.py           connection from CAPQUERY_VERIFY_DB_* environment
    demo/shop/models.py        Team, Comment (plain), Ticket (postgres-only columns)
    demo/shop/migrations/      the schema, and a deterministic seed (raw SQL with casts)
    tests/                     reads, queries, writes, the ORM on the postgres types,
                               and the raw-driver check of what postgres really holds
    probe/test_uncapturable_values.py   the project's own value type and a multirange
```

The seed migration writes its rows with raw SQL and explicit casts on purpose: it keeps
the migration-phase capture readable and independent of the ORM's parameter preparation.
Ids are explicit and the sequences are set afterwards, so a row created by a test has the
same id on every run.
