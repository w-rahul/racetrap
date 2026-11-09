<img src="docs/icon.svg" width="72" alt="">

# racetrap

Finds race conditions in web APIs by forcing the worst order of their SQL statements.

![racetrap catching a double booking, then passing once the fix is in](docs/demo.gif)

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

```
$ racetrap examples/tickets/buggy.toml
racetrap 0.1.0
proxy  127.0.0.1:6499 -> localhost:5432
app    http://127.0.0.1:8765

  alice books  vs  alice books   5 orders  RACE
  alice books  vs  bob books     5 orders  RACE
  bob books    vs  alice books   5 orders  RACE
  bob books    vs  bob books     5 orders  RACE

4 of 4 pairs race. 34 runs in 4.7s.
Report: racetrap-report.html
```

![The report for one race: both requests as lanes, the hold, the outcome and the fix](docs/report.png)

## What it does not do

- **It does not replace transactions, locks or tests.** You keep writing normal code. racetrap shows where the transaction you have is not enough, and the fix stays a line you write: `FOR UPDATE`, a conditional `UPDATE ... WHERE`, a unique constraint, or `SERIALIZABLE` with a retry.
- **It never changes your code, and it is not for production.** Point it at a test database you can wipe. It only watches and delays SQL.
- **It is not a load test.** It does not fire thousands of requests and hope. It forces each order once.
- **It does not see everything.** Postgres only, two requests at a time, and locks held outside Postgres, like Redis or an in-process mutex, are invisible to it.

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

To run it on your own app, install the command with `uv tool install git+https://github.com/w-rahul/racetrap` and write a scenario file.

## Scenario file

```toml
app = "http://127.0.0.1:8000"
database = "${DATABASE_URL}"
command = "uvicorn shop:app --port 8000"

setup = [
    "TRUNCATE orders, stock RESTART IDENTITY",
    "INSERT INTO stock VALUES (1, 1)",
]

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
| `database` | The real Postgres, used directly for setup and snapshots. |
| `setup` | SQL run before every order. It must reset the data. |
| `[[request]]` | `name`, `path`, and optional `method`, `json` and `headers`. |
| `command` | Optional. Starts the app with `DATABASE_URL` pointing at the proxy. |
| `init` | Optional SQL run once before the app starts. |
| `invariants` | Optional SQL that must return true after every order. |
| `ignore` | Optional `table.column` entries to leave out of the comparison. |
| `refs` | Optional id references without a foreign key, like `{ "orders.user_id" = "users.id" }`. |
| `proxy`, `database_env`, `schema`, `stall_ms` | Defaults `127.0.0.1:6499`, `DATABASE_URL`, `public` and `1000`. |

`${NAME}` is read from the environment. Each request is also paired with itself, which catches double submits.

## How it decides

The check is serializability. Every outcome must match running the two requests one after the other, in some order. racetrap compares the HTTP status of each request and the rows of every table.

Values the database fills in differ between correct runs, so ids, timestamps and random values are left out, and a reference to a generated id is compared by the row it points to. A deadlock or an unretried serialization failure that turns into a 500 counts as a race. racetrap stops instead of passing if the app dies, and the exit code is 1 when any pair races, so it can fail a CI job.

## Prior work

Finding these bugs from database traces comes from [ACIDRain](https://www.bailis.org/papers/acidrain-sigmod2017.pdf) (SIGMOD 2017), which found 22 attacks in 12 popular e-commerce apps. [RaceDB](https://par.nsf.gov/biblio/10617180) (IEEE S&P 2025) confirms candidates by replaying them. [ChaosSQL](https://github.com/bregaldahq/chaossql) and [Deadheat](https://github.com/sanjayrawatt/deadheat) also proxy the database, but check a rule you write and randomize the timing. racetrap needs no rule and gives the same answer on every run.

## License

MIT
