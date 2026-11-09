# Changelog

Notable changes to racetrap. Versions follow [semantic versioning](https://semver.org).

## Unreleased

### Added

- racetrap saves the tables before it starts, restores them before every order, and leaves the database as it found it. `setup` SQL is now optional.
- Pairs of requests that share no table that either one writes are skipped. `--all-pairs` tests every pair.
- An order that races is run a second time, and reported only if it races again.
- `racetrap init` reads OpenAPI documents in YAML as well as JSON.
- `--json PATH` writes the results as JSON, for scripts and dashboards.
- A Dockerfile, so teams without Python can run racetrap. CI builds the image and runs it against the ticket example.
- A reference page for the scenario file, the command line, the JSON output, the GitHub Action and Docker.
- "What a pass means" in the README: which orders racetrap tries, and what a pass does not cover.
- A security policy.

### Changed

- Holds that only repeat a one at a time run are left out. The ticket example now takes 3 orders for each pair, down from 5.
- `setup` strings may hold several statements.
- A simpler, sharper logo, and a new demo in the README.
- CI covers Postgres 14 to 18, Python 3.11 to 3.14, and Linux, Windows and macOS.
- The proxy is tested with psycopg2, next to asyncpg and psycopg 3.
- ruff checks the code in CI, and the test run reports coverage.

## 0.2.0 (2026-11-09)

### Added

- `racetrap init --openapi` writes a starting scenario from an OpenAPI document: one request for every POST, PUT, PATCH and DELETE, with path parameters filled and example bodies built from the schemas.
- A GitHub Action. `uses: w-rahul/racetrap@v0.2.0` installs racetrap, runs a scenario, uploads the HTML report, and fails the job on a race.

## 0.1.0 (2026-11-09)

First release.

- A Postgres proxy that holds one request before each of its SQL statements while another request runs.
- Each outcome is checked against running the two requests one at a time, in both orders.
- An HTML report for each race: both requests as lanes, the hold, the outcome and the fix.
- Exit code 1 when any pair races.
