<img src="docs/icon.svg" width="72" alt="">

# racetrap

Finds race conditions in web APIs by forcing the worst order of their SQL statements.

![racetrap catching a double booking, then passing once the fix is in](docs/demo.gif)

Two requests that hit the same row at the same moment can both win, and your tests will never show it. racetrap sets the trap: it holds one request at each step and lets the other run, until the bug shows up or every order it tried matches a serial run.

## The bug

```python
async with pool.acquire() as conn, conn.transaction():
    seats = await conn.fetchval("SELECT seats_left FROM shows WHERE id = $1", show_id)
    if not seats:
        raise HTTPException(409, "sold out")
    await conn.execute("UPDATE shows SET seats_left = $1 WHERE id = $2", seats - 1, show_id)
    await conn.execute("INSERT INTO bookings (show_id, user_name) VALUES ($1, $2)", show_id, user)
```

This passes every test. It is inside a transaction. And when two people book the last seat at the same moment, both get it.

The transaction does not help. At Postgres's default isolation level both requests read `seats_left = 1` before either one writes. Tests send one request at a time, so they never see it. In production it shows up rarely, under load, and nobody can reproduce it.

## It does not replace transactions

racetrap is a test tool, not a library. You keep writing transactions, locks and normal code, and racetrap never changes any of it. It runs your real app against a real database, shows where the transaction you have is not enough, and proves that the fix you choose holds.

The fix stays in your code, and it is usually one line: a `FOR UPDATE`, a conditional `UPDATE ... WHERE`, a unique constraint, or `SERIALIZABLE` with a retry.

## What racetrap does

racetrap sits between your app and Postgres as a proxy. Your code does not change, and it works with any language or framework that talks to Postgres.

For each pair of requests A and B:

1. It runs A then B, and B then A, one at a time. These are the only correct outcomes.
2. For every statement A sends, it holds A right before that statement, lets B run, then releases A.
3. If B waits on a lock A holds, racetrap sees it in `pg_blocking_pids` and releases A at once.
4. If the responses or the final rows match neither one at a time run, no serial order can produce them. That is a race.

Each forced order runs once and gives the same result every time. There is no load test and no luck involved.

```
$ racetrap examples/tickets/buggy.toml
racetrap 0.1.0
proxy  127.0.0.1:6499 -> localhost:5432
app    http://127.0.0.1:8765

  alice books  vs  alice books   5 orders  RACE
  alice books  vs  bob books     5 orders  RACE
  bob books    vs  alice books   5 orders  RACE
  bob books    vs  bob books     5 orders  RACE

4 of 4 pairs race. 34 runs in 4.8s.
Report: racetrap-report.html
```

The report shows, for each race, what both serial orders return, what the forced order returned, the final rows, the full statement timeline with the hold and the release, and how to fix it.

![racetrap report for the ticket example](docs/report.png)

Add `FOR UPDATE` to the read and run it again:

```
$ racetrap examples/tickets/fixed.toml
  ...
No races in 4 pairs. 34 runs in 4.8s.
```

## Try it

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/) and a Postgres you can wipe.

```sh
git clone https://github.com/w-rahul/racetrap
cd racetrap
uv sync
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
uv run racetrap examples/tickets/buggy.toml
```

To use it on your own app, install the command:

```sh
uv tool install git+https://github.com/w-rahul/racetrap
```

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
| `database` | The real Postgres. racetrap uses it directly for setup and snapshots. |
| `setup` | SQL run before every order. It must put the data back in the same state. |
| `[[request]]` | `name`, `path`, and optional `method`, `json` and `headers`. The method is POST when `json` is set, else GET. |
| `command` | Optional. Starts the app with `DATABASE_URL` pointing at the proxy. Without it, start your app against the proxy yourself. |
| `init` | Optional SQL run once before the app starts, such as creating tables. |
| `invariants` | Optional SQL that must return true after every order, like `SELECT count(*) <= 1 FROM orders`. |
| `ignore` | Optional columns to leave out of the comparison, as `table.column`. |
| `refs` | Optional references that have no foreign key, like `{ "memberships.user_id" = "users.id" }`. |
| `proxy` | Where the proxy listens. Default `127.0.0.1:6499`. |
| `database_env` | The variable `command` gets the proxy URL in. Default `DATABASE_URL`. |
| `schema` | Schema to compare. Default `public`. |
| `stall_ms` | How long B may sit idle before racetrap releases A. Default 1000. |

`${NAME}` in `app`, `database` and `command` is read from the environment.

Each request is also paired with itself, which catches double submits.

## How it decides

The check is serializability. Every outcome racetrap sees must match running the two requests one after the other, in some order. It compares the HTTP status of each request and the rows of every table in the schema.

Some values differ between correct runs: ids from sequences, timestamps, random tokens. racetrap leaves out columns whose default is a sequence, an identity, a clock or a random function. It also runs every request, and every pair, twice from the same state and leaves out any column that changed between the two runs. You can add more with `ignore`.

Ids are harder. When two requests insert at once, which one gets id 1 depends on timing, so a join table like `memberships(user_id, account_id)` can hold different numbers in a correct run. racetrap replaces each reference to a generated id with the row it points to, then compares. A membership that links alice to bob's account is still caught. It reads references from foreign keys, and `refs` covers the ones without a key.

A deadlock or an unretried serialization failure that turns into a 500 is a race too, and the report names it. An invariant that fails even when the requests run one at a time is reported as a warning, not a race.

racetrap refuses to pass silently. It stops if the app exits or stops answering, and it warns when a statement could not be tested because it was sent after the response or its text changed between runs.

The exit code is 1 when any pair races, so it can fail a CI job.

## Limits

- Postgres only, over plain TCP between the app and the proxy.
- Two requests at a time. A race that needs three is not explored.
- Locks outside Postgres, like Redis or an in-process mutex, are invisible. racetrap waits `stall_ms` and then releases A.
- It holds between statements, not inside one.
- Before testing, racetrap watches the app sit idle for one second and leaves out any SQL it sends on its own, like a poller. Background work that runs less often than that counts as part of whichever request is running.
- Every run snapshots every table in the schema. Use a small test database.
- The app needs a connection pool of at least two, or B can never start while A is held.

## Prior work

The idea of finding these bugs from database traces comes from [ACIDRain](https://www.bailis.org/papers/acidrain-sigmod2017.pdf) (Warszawski and Bailis, SIGMOD 2017), which found 22 attacks in 12 popular e-commerce apps. [RaceDB](https://par.nsf.gov/biblio/10617180) (IEEE S&P 2025) confirms candidates by replaying them. [IsoDiff](http://www.vldb.org/pvldb/vol13/p2773-gan.pdf) studies anomalies under weak isolation. racetrap takes the practical part of these: watch the real SQL, force the bad order, and prove the race with a run you can repeat.

Related tools: [ChaosSQL](https://github.com/bregaldahq/chaossql) and [Deadheat](https://github.com/sanjayrawatt/deadheat) also put a proxy in front of the database. They check a rule you write and widen or randomize the timing. racetrap needs no rule, because it compares against serial runs, and it holds A at every statement in turn, so the same app gives the same answer on every run.

## License

MIT
