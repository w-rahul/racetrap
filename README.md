<p align="center">
  <img src="https://raw.githubusercontent.com/w-rahul/racetrap/main/docs/icon.svg" width="96" alt="">
</p>

<h1 align="center">racetrap</h1>

<p align="center">Finds race conditions in web APIs by forcing the worst order of their SQL statements.</p>

<p align="center">
  <a href="https://pypi.org/project/racetrap/"><img src="https://img.shields.io/pypi/v/racetrap?color=E0483E&label=pypi" alt="PyPI"></a>
  <a href="https://github.com/w-rahul/racetrap/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/w-rahul/racetrap/test.yml?label=tests" alt="Tests"></a>
  <a href="https://github.com/w-rahul/racetrap/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-8D8A84" alt="MIT license"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/w-rahul/racetrap/main/docs/demo.gif" width="100%" alt="racetrap catching a double booking, then passing once the fix is in">
</p>

## Why it exists

```python
async with conn.transaction():
    seats = await conn.fetchval("SELECT seats_left FROM shows WHERE id = $1", show_id)
    if seats < 1:
        raise HTTPException(409, "sold out")
    await conn.execute("UPDATE shows SET seats_left = $1 WHERE id = $2", seats - 1, show_id)
    await conn.execute("INSERT INTO bookings (show_id, user_name) VALUES ($1, $2)", show_id, user)
```

This passes every test and runs inside a transaction. Still, when two people book the last seat at the same moment, both get it. At Postgres's default isolation level both requests read `seats_left = 1` before either one writes.

Tests send one request at a time, so they never see it. In production it shows up rarely, under load, and nobody can reproduce it. racetrap makes it show up on demand, in a test, with the exact order of statements that caused it.

## What it does

racetrap sits between your app and Postgres as a proxy. For every pair of requests:

1. It runs them one at a time, in both orders. Those are the correct outcomes.
2. It runs them again, holding the first request right before each of its SQL statements in turn while the second one runs.
3. Any result that matches neither one at a time run is a race, reported with the order that caused it.

Each forced order gives the same result on every run. There is no load test and no luck involved.

There is nothing to reset by hand. racetrap saves your tables before it starts, puts them back before every order, and leaves the database as it found it. It also skips work that cannot change the answer: pairs of requests that share no tables, and holds that would only repeat a one at a time run.

```
$ racetrap examples/tickets/buggy.toml
racetrap 0.2.0
proxy  127.0.0.1:6499 -> localhost:5432
app    http://127.0.0.1:8765

  alice books  vs  alice books   3 orders  RACE
  alice books  vs  bob books     3 orders  RACE
  bob books    vs  alice books   3 orders  RACE
  bob books    vs  bob books     3 orders  RACE

4 of 4 pairs race. 38 runs in 5.0s.
Report: racetrap-report.html
```

![The report for one race: both requests as lanes, the hold, the outcome and the fix](https://raw.githubusercontent.com/w-rahul/racetrap/main/docs/report.png)

## What it does not do

- **It does not replace transactions, locks or tests.** You keep writing normal code. racetrap shows where the transaction you have is not enough, and the fix stays a line you write: `FOR UPDATE`, a conditional `UPDATE ... WHERE`, a unique constraint, or `SERIALIZABLE` with a retry.
- **It never changes your code, and it is not for production.** Point it at a test database you can wipe. It only watches and delays SQL.
- **It is not a load test.** It does not fire thousands of requests and hope. It forces each order once, and repeats an order that races to confirm it.
- **It does not see everything.** Postgres only, two requests at a time, and locks held outside Postgres, like Redis or an in-process mutex, are invisible to it.

## What a pass means

A pass is a bounded guarantee, not a proof that an API has no races.

For every pair of requests in the scenario, and for each request paired with itself, racetrap forces every order of this shape: one request stops right before one of its SQL statements, the other runs until it finishes or waits on a lock, and then the first one continues. A pass means none of those orders gave a result that differs from running the two requests one at a time, starting from the data in the database when racetrap starts, at the isolation level your app uses.

Two kinds of work are left out because they cannot change that answer. A stop is skipped while the first request has only opened a transaction, or has nothing left but handing its connection back, since both are one at a time runs. A pair is skipped when the SQL shows the two requests share no table that either one writes, following foreign keys. A request that calls your own functions, reads a view, or touches a table with triggers is never skipped, and `--all-pairs` turns pair skipping off.

That shape covers the common bugs: lost updates, check then write like the double booking above, write skew between two transactions, and double submits.

A pass says nothing about:

- races that need three or more requests, or more than one switch between the two
- starting data other than what the database holds when racetrap starts
- effects racetrap does not compare, such as response bodies, emails, queues and caches
- statements the app sends after it has answered, which racetrap warns about
- anything guarded outside Postgres

A reported race is never a prediction. racetrap ran that order against your app and saw the responses and rows shown in the report. A false alarm is still possible when a value changes between two correct runs in a way racetrap did not notice, and `ignore` fixes that.

## Benchmark

`benchmarks/burst.py` does what people try without racetrap: it fires alice and bob at the same instant, 1,000 times, on the ticket example. On one Windows machine it caught the double booking in 68.5%, 81.6% and 79.8% of bursts across three runs, and the rate changed every time. racetrap caught it on every run, in all 4 pairs, named the exact order of statements, and checked the fix against every order it forced.

## Try it

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) and a Postgres you can wipe.

```sh
git clone https://github.com/w-rahul/racetrap
cd racetrap
uv sync
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
uv run racetrap examples/tickets/buggy.toml
uv run racetrap examples/tickets/fixed.toml
```

The first run finds the race. The second one has `FOR UPDATE` on the read and passes.

To run it on your own app, install it and start a scenario from your OpenAPI document:

```sh
pip install racetrap
racetrap init --openapi http://127.0.0.1:8000/openapi.json
```

`init` reads JSON or YAML and writes `racetrap.toml` with one request for every POST, PUT, PATCH and DELETE, filled with example bodies from your schemas. Check the example bodies and run `racetrap racetrap.toml`.

No Python on the machine? The repository has a Dockerfile, and the [reference](https://github.com/w-rahul/racetrap/blob/main/docs/reference.md#docker) shows how to run it.

## Scenario file

```toml
app = "http://127.0.0.1:8000"
database = "${DATABASE_URL}"
command = "uvicorn shop:app --port 8000"

[[request]]
name = "alice buys"
path = "/items/1/buy"
json = { user = "alice" }

[[request]]
name = "bob buys"
path = "/items/1/buy"
json = { user = "bob" }
```

| Key | Meaning |
| --- | --- |
| `app` | Base URL of the app. |
| `database` | The real Postgres. racetrap also connects to it directly, to save, restore and compare the tables. |
| `[[request]]` | `name`, `path`, and optional `method`, `json` and `headers`. |
| `command` | Optional. Starts the app with `DATABASE_URL` pointing at the proxy. |
| `init` | Optional SQL run once before the app starts. |
| `setup` | Optional SQL run before every order. With it, racetrap resets the data your way and does not save or restore tables. |
| `invariants` | Optional SQL that must return true after every order. |
| `ignore` | Optional `table.column` entries to leave out of the comparison. |
| `refs` | Optional id references without a foreign key, like `{ "orders.user_id" = "users.id" }`. |
| `proxy`, `database_env`, `schema`, `stall_ms` | Defaults `127.0.0.1:6499`, `DATABASE_URL`, `public` and `1000`. |

`${NAME}` is read from the environment. Each request is also paired with itself, which catches double submits. The [reference](https://github.com/w-rahul/racetrap/blob/main/docs/reference.md) covers every key, the command line and the JSON results.

## Run it in CI

```yaml
jobs:
  races:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:18
        env:
          POSTGRES_PASSWORD: postgres
        ports: ["5432:5432"]
        options: --health-cmd pg_isready --health-interval 2s --health-retries 30
    env:
      DATABASE_URL: postgresql://postgres:postgres@localhost:5432/postgres
    steps:
      - uses: actions/checkout@v7
      - run: pip install -r requirements.txt
      - uses: w-rahul/racetrap@v0.2.0
        with:
          scenario: racetrap.toml
```

The job fails when any pair races, and the HTML report is uploaded as an artifact. Install your app's own dependencies before the racetrap step, so `command` can start it. Outside the action, `racetrap racetrap.toml --json results.json` writes the results for scripts.

## How it decides

The check is serializability. Every outcome must match running the two requests one after the other, in some order. racetrap compares the HTTP status of each request and the rows of every table.

Values the database fills in differ between correct runs, so ids, timestamps and random values are left out, and a reference to a generated id is compared by the row it points to. A deadlock or an unretried serialization failure that turns into a 500 counts as a race. racetrap stops instead of passing if the app dies, and the exit code is 1 when any pair races, so it can fail a CI job.

## Compatibility

| | Tested in CI |
| --- | --- |
| Postgres | 14, 15, 16, 17, 18 |
| Python, to run racetrap | 3.11 to 3.14 |
| Systems | Linux, Windows, macOS |
| Drivers | asyncpg, psycopg 3, psycopg2 |

Your app can be in any language. racetrap speaks the Postgres protocol and never looks at the client. node-postgres was checked by hand, and other drivers are not in CI yet.

Two things are not supported yet. The proxy speaks plain TCP on both sides, so the app must not require TLS to reach it, and the test database must accept connections without TLS. A connection pooler such as PgBouncer between racetrap and Postgres is untested.

## Research behind it

Finding these bugs from database traces comes from [ACIDRain](https://www.bailis.org/papers/acidrain-sigmod2017.pdf) (SIGMOD 2017), which found 22 attacks in 12 popular e-commerce apps. [RaceDB](https://par.nsf.gov/biblio/10617180) (IEEE S&P 2025) confirms candidates by replaying them. racetrap takes the practical part of both: watch the real SQL, force the bad order, and prove the race with a run you can repeat.

## License

MIT
