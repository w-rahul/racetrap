# Reference

- [Scenario file](#scenario-file)
- [Command line](#command-line)
- [pytest](#pytest)
- [JSON results](#json-results)
- [GitHub Action](#github-action)
- [Docker](#docker)

## Scenario file

A scenario is a TOML file. It names the app, the database and the requests to race.

```toml
app = "http://127.0.0.1:8000"
database = "${DATABASE_URL}"
command = "uvicorn shop:app --port 8000"

[[request]]
name = "alice buys"
path = "/items/1/buy"
json = { user = "alice" }
headers = { authorization = "Bearer alice-token" }

[[request]]
name = "bob buys"
path = "/items/1/buy"
json = { user = "bob" }
headers = { authorization = "Bearer bob-token" }
```

### Keys

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `app` | yes | | Base URL of the app. Starts with `http://` or `https://`. |
| `database` | yes | | URL of the real Postgres. The proxy forwards to it, and racetrap connects to it directly to save, restore and compare the tables. See [TLS](#tls) for `sslmode`. |
| `[[request]]` | yes | | The requests to race. At least one. See below. |
| `[[login]]` | no | | Requests sent once before the run, to log users in. See [Logging in](#logging-in). |
| `command` | no | | Command that starts the app. racetrap runs it in the scenario file's folder, with the database URL pointing at the proxy, and stops it at the end. |
| `database_env` | no | `DATABASE_URL` | The environment variable `command` receives the proxy URL in. |
| `init` | no | `[]` | List of SQL statements run once, before the app starts. Good for creating tables and seed rows. |
| `setup` | no | `[]` | List of SQL statements run before every order. They must put the data back in the same state each time. With `setup`, racetrap does not save or restore tables. See [How the data is reset](#how-the-data-is-reset). |
| `invariants` | no | `[]` | List of SQL queries that must return true after every order, like `SELECT count(*) <= 1 FROM orders`. A failure is reported as a race, unless it also fails in both one at a time runs. |
| `ignore` | no | `[]` | List of `table.column` entries to leave out of the comparison. |
| `refs` | no | `{}` | Id references that have no foreign key, like `{ "orders.user_id" = "users.id" }`. racetrap compares such a column by the row it points to. |
| `bodies` | no | `true` | Compare response bodies as well as status codes. Fields named like ids, tokens and timestamps are left out, and so is any field that changes between two identical runs. |
| `proxy` | no | `127.0.0.1:6499` | Address the proxy listens on. |
| `schema` | no | `public` | The Postgres schema whose tables are compared. |
| `stall_ms` | no | `1000` | How long the second request may send nothing before racetrap releases the first. This is how a lock outside Postgres ends a wait. |

`${NAME}` in `app`, `database` and `command` is replaced with the environment variable `NAME`. A variable that is not set is an error.

### Requests

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `name` | yes | | A unique label, used in the output and the report. |
| `path` | yes | | Path and query string, starting with `/`. |
| `method` | no | `POST` with `json`, else `GET` | The HTTP method. |
| `json` | no | | The request body, sent as JSON. |
| `headers` | no | `{}` | Extra headers, such as `authorization`. Give each request its own user to race two users. |

racetrap pairs every request with every other one, in both roles, and with itself, which catches double submits. See [What is skipped](#what-is-skipped) for the pairs it leaves out.

### Logging in

A `[[login]]` is a request racetrap sends once, before the first order. It takes the same keys as a request, plus `capture`, which names the values to keep from the response.

```toml
[[login]]
name = "alice"
path = "/login"
json = { email = "alice@example.com", password = "secret" }
capture = { token = "data.access_token", cookie = "header:set-cookie" }

[[request]]
name = "alice buys"
path = "/items/1/buy"
json = { quantity = 1 }
headers = { authorization = "Bearer ${alice.token}", cookie = "${alice.cookie}" }
```

- A capture is a path into the JSON body, with dots between keys and list positions, like `data.tokens.0.value`.
- `header:` reads a response header instead. For `set-cookie` it keeps only the `name=value` part of each cookie, ready to send back in a `cookie` header.
- A request uses a captured value as `${login.name}` in its path, headers or JSON body. A name no login captures is an error when the scenario loads.
- Logins run before racetrap saves the tables, so the sessions they create are part of the data every order starts from.
- A login that does not answer with a 2xx status stops the run.

### How the data is reset

Every order must start from the same data. Without `setup`, racetrap takes care of that:

1. Before the first order it copies every table of the schema into a schema named `racetrap_snapshot`, and notes the state of every sequence.
2. Before each order it empties the tables, copies the rows back and resets the sequences, in one transaction.
3. At the end it restores the data once more and drops `racetrap_snapshot`, so the database is left as it was found.

The data that counts is what the database holds when racetrap starts: your own seed, or rows inserted by `init`.

Restoring switches off triggers and foreign key checks for its own transaction, so tables can go back in any order. That needs a superuser on the test database, which the standard Postgres image gives you. Without one, write `setup` SQL instead.

### What is skipped

racetrap leaves out work that cannot change the answer.

- **Holds.** A request is not held while it has only opened a transaction, or when it has nothing left but handing its connection back. Letting the other request run at those points is the same as a one at a time run, which is already checked.
- **Pairs.** A pair is skipped when neither request writes a table the other one touches. racetrap reads the table names from the SQL each request sent, adds the tables whose rows changed, and follows foreign keys: a write to a child reads its parent, and a delete or update can cascade into children. Every doubt keeps the pair. A request is never skipped when it calls a function of your own, reads a view, touches a table with triggers or rules, or sends SQL that names no table racetrap knows.

Skipped pairs are listed in the output and the report. `--all-pairs` tests every pair.

### How the app is started

With `command`, racetrap starts the app itself. It refuses to run if something already answers at `app`, so an old process is never tested by mistake.

Without `command`, start the app yourself with its database URL pointing at the proxy. Start racetrap first: it opens the proxy, then waits up to 30 seconds for the app to answer.

The app needs a connection pool of at least two, or the second request cannot start while the first is held.

### TLS

racetrap opens TLS to the database when the `database` URL asks for it. It reads `sslmode` the way libpq does.

| `sslmode` | What the proxy does |
| --- | --- |
| `disable` | Plain TCP. |
| `allow`, `prefer`, or not set | TLS when the server offers it, without checking its certificate. Plain TCP when it does not. |
| `require` | TLS, without checking the certificate. A server that offers no TLS is an error. |
| `verify-ca` | TLS, and the certificate must come from a trusted authority. |
| `verify-full` | The same, and the certificate must also match the host name. |

For the two verify modes, `sslrootcert` names the authority's certificate file. Without it, racetrap trusts the authorities your system trusts. Client certificates (`sslcert` and `sslkey`) are not sent yet.

The app always talks to the proxy in plain TCP, on the same machine. When racetrap starts the app, the URL it hands over has `sslmode=disable` in place of the one you set, and no `channel_binding`. When you start the app yourself, turn TLS off for its connection to the proxy.

## Command line

```
racetrap SCENARIO [--report PATH] [--json PATH] [--junit PATH] [--all-pairs] [--workers N]
racetrap init --openapi SOURCE [--app URL] [-o FILE] [--force]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--report PATH` | `racetrap-report.html` | Where to write the HTML report. |
| `--json PATH` | | Also write the results as JSON. |
| `--junit PATH` | | Also write the results as JUnit XML, one test case per pair. A racing pair is a failure, and a skipped pair is skipped. |
| `--all-pairs` | | Test every pair, even two requests that share no tables. |
| `--workers N` | `1` | Run N pairs at a time. See [Workers](#workers). |
| `init --openapi SOURCE` | | Path or URL of an OpenAPI document, in JSON or YAML. |
| `init --app URL` | from the URL or the document's servers | Base URL to put in the scenario. |
| `init -o FILE` | `racetrap.toml` | The scenario file to write. |
| `init --force` | | Overwrite the file if it exists. |

`init` writes one request for every POST, PUT, PATCH and DELETE. It fills path parameters and builds example bodies from the schemas, using the document's own examples and defaults where it has them.

### Workers

`--workers N` splits the pairs between N workers that run at the same time. Each worker has its own:

- **Database.** The first worker uses the database itself. Every other worker gets a copy named `racetrap_1_<database>`, `racetrap_2_<database>` and so on, made with `CREATE DATABASE ... TEMPLATE` after `init` has run, and dropped at the end.
- **Proxy.** On the next port: 6499, 6500, 6501.
- **App.** On the next port after the one in `app`. racetrap rewrites that port number wherever it appears in `command`, and sets the `PORT` environment variable for apps that take their port from there.

It needs `command`, a port in `app`, and a database user that may create databases. Postgres only copies a database that nobody else is connected to, so close other connections to the test database first.

Each worker logs in and learns the requests by itself, so the gain grows with the number of pairs. On the ticket example with six users, 36 pairs took 40.8 s with one worker, 14.4 s with four and 11.2 s with eight, on one Windows machine. The verdicts were the same every time.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | No pair races. |
| 1 | At least one pair races. |
| 2 | racetrap could not run: a bad scenario, an app that did not start, or a database error. |

## pytest

racetrap comes with a pytest plugin. Name a scenario file and it runs as tests, one per pair:

```
$ pytest racetrap.toml
racetrap.toml::alice buys vs alice buys PASSED
racetrap.toml::alice buys vs bob buys FAILED
```

- A racing pair fails, with the order that raced and the path of the HTML report. The report is written as `racetrap-report-<scenario>.html` in the folder pytest was started from.
- A pair racetrap skips is a skipped test, and racetrap's warnings show up as pytest warnings.
- Only files named on the command line or in `testpaths` are run. A scenario empties tables, so a plain `pytest` never picks one up by its name alone.
- The scenario runs once, when its first test starts. Choosing a single pair with `-k` still runs the whole scenario.

## JSON results

`--json results.json` writes this shape. The example is cut to one pair and one race.

```json
{
  "racetrap": "0.5.0",
  "scenario": "buggy.toml",
  "app": "http://127.0.0.1:8765",
  "runs": 38,
  "seconds": 5.02,
  "racing_pairs": 4,
  "warnings": [],
  "ignored_columns": ["bookings.id"],
  "pairs": [
    {
      "a": "alice books",
      "b": "bob books",
      "skipped": false,
      "orders": 3,
      "responses": {
        "a_then_b": { "A": 200, "B": 409 },
        "b_then_a": { "B": 200, "A": 409 }
      },
      "races": [
        {
          "held_before": "UPDATE shows SET seats_left = $1 WHERE id = $2",
          "occurrence": 1,
          "responses": { "A": 200, "B": 200 },
          "reasons": [
            "responses match no serial order",
            "final data matches no serial order"
          ]
        }
      ]
    }
  ]
}
```

- `orders` is how many orders racetrap forced for the pair.
- `responses` holds the HTTP status of each request in the two one at a time runs.
- Each entry in `races` is one forced order that no one at a time run can explain. `held_before` is the statement request A was held before, and `occurrence` says which time A sent it.
- A pair with an empty `races` list passed.
- A pair with `skipped` set to true was not run, because the two requests share no table that either one writes. Its `responses` is null.

## GitHub Action

```yaml
- uses: w-rahul/racetrap@v0.5.0
  with:
    scenario: racetrap.toml
```

| Input | Default | Meaning |
| --- | --- | --- |
| `scenario` | | Path to the scenario file. Required. |
| `report` | `racetrap-report.html` | Where to write the HTML report. |
| `upload-report` | `true` | Upload the report as a workflow artifact. |
| `artifact-name` | `racetrap-report` | Name of that artifact. Change it when a job runs the action twice. |
| `package` | `racetrap` | What to install, like `racetrap==0.5.0`. |
| `args` | | Extra command line options, like `--workers 4` or `--junit results.xml`. |

The job needs a Postgres it can reach and your app's own dependencies installed, so `command` can start the app. The step fails when any pair races.

## Docker

The repository has a Dockerfile, and Docker can build it straight from GitHub:

```sh
docker build -t racetrap https://github.com/w-rahul/racetrap.git
docker run --rm --network host -v "$PWD:/work" -e DATABASE_URL racetrap racetrap.toml
```

The container cannot start your app, so leave `command` out of the scenario and start the app yourself, as described under [How the app is started](#how-the-app-is-started). CI runs the image this way against the ticket example.

`--network host` works on Linux. On Docker Desktop for Mac and Windows, set `proxy = "0.0.0.0:6499"` in the scenario, publish the port with `-p 6499:6499`, and use `host.docker.internal` as the host in `app` and `database`. That setup is not covered by CI.
