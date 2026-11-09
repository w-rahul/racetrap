# Reference

- [Scenario file](#scenario-file)
- [Command line](#command-line)
- [JSON results](#json-results)
- [GitHub Action](#github-action)
- [Docker](#docker)

## Scenario file

A scenario is a TOML file. It names the app, the database, the SQL that resets the data, and the requests to race.

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
| `database` | yes | | URL of the real Postgres. racetrap connects to it directly for setup and snapshots, and the proxy forwards to it. |
| `setup` | yes | | List of SQL statements run before every order. They must put the data back in the same state each time. |
| `[[request]]` | yes | | The requests to race. At least one. See below. |
| `command` | no | | Command that starts the app. racetrap runs it in the scenario file's folder, with the database URL pointing at the proxy, and stops it at the end. |
| `database_env` | no | `DATABASE_URL` | The environment variable `command` receives the proxy URL in. |
| `init` | no | `[]` | List of SQL statements run once, before the app starts. Good for creating tables. |
| `invariants` | no | `[]` | List of SQL queries that must return true after every order, like `SELECT count(*) <= 1 FROM orders`. A failure is reported as a race, unless it also fails in both one at a time runs. |
| `ignore` | no | `[]` | List of `table.column` entries to leave out of the comparison. |
| `refs` | no | `{}` | Id references that have no foreign key, like `{ "orders.user_id" = "users.id" }`. racetrap compares such a column by the row it points to. |
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

racetrap tries every ordered pair, and pairs each request with itself, which catches double submits. Five requests make 25 pairs.

### How the app is started

With `command`, racetrap starts the app itself. It refuses to run if something already answers at `app`, so an old process is never tested by mistake.

Without `command`, start the app yourself with its database URL pointing at the proxy. Start racetrap first: it opens the proxy, then waits up to 30 seconds for the app to answer.

The app needs a connection pool of at least two, or the second request cannot start while the first is held.

## Command line

```
racetrap SCENARIO [--report PATH] [--json PATH]
racetrap init --openapi SOURCE [--app URL] [-o FILE] [--force]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--report PATH` | `racetrap-report.html` | Where to write the HTML report. |
| `--json PATH` | | Also write the results as JSON. |
| `init --openapi SOURCE` | | Path or URL of an OpenAPI document, in JSON or YAML. |
| `init --app URL` | from the URL or the document's servers | Base URL to put in the scenario. |
| `init -o FILE` | `racetrap.toml` | The scenario file to write. |
| `init --force` | | Overwrite the file if it exists. |

`init` writes one request for every POST, PUT, PATCH and DELETE. It fills path parameters and builds example bodies from the schemas, using the document's own examples and defaults where it has them.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | No pair races. |
| 1 | At least one pair races. |
| 2 | racetrap could not run: a bad scenario, an app that did not start, or a database error. |

## JSON results

`--json results.json` writes this shape. The example is cut to one pair and one race.

```json
{
  "racetrap": "0.2.0",
  "scenario": "buggy.toml",
  "app": "http://127.0.0.1:8765",
  "runs": 34,
  "seconds": 4.63,
  "racing_pairs": 4,
  "warnings": [],
  "ignored_columns": ["bookings.id"],
  "pairs": [
    {
      "a": "alice books",
      "b": "bob books",
      "orders": 5,
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

## GitHub Action

```yaml
- uses: w-rahul/racetrap@v0.2.0
  with:
    scenario: racetrap.toml
```

| Input | Default | Meaning |
| --- | --- | --- |
| `scenario` | | Path to the scenario file. Required. |
| `report` | `racetrap-report.html` | Where to write the HTML report. |
| `upload-report` | `true` | Upload the report as a workflow artifact. |
| `artifact-name` | `racetrap-report` | Name of that artifact. Change it when a job runs the action twice. |
| `package` | `racetrap` | What to install, like `racetrap==0.2.0`. |

The job needs a Postgres it can reach and your app's own dependencies installed, so `command` can start the app. The step fails when any pair races.

## Docker

The repository has a Dockerfile, and Docker can build it straight from GitHub:

```sh
docker build -t racetrap https://github.com/w-rahul/racetrap.git
docker run --rm --network host -v "$PWD:/work" -e DATABASE_URL racetrap racetrap.toml
```

The container cannot start your app, so leave `command` out of the scenario and start the app yourself, as described under [How the app is started](#how-the-app-is-started). CI runs the image this way against the ticket example.

`--network host` works on Linux. On Docker Desktop for Mac and Windows, set `proxy = "0.0.0.0:6499"` in the scenario, publish the port with `-p 6499:6499`, and use `host.docker.internal` as the host in `app` and `database`. That setup is not covered by CI.
