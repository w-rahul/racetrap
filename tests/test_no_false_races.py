import asyncio
from pathlib import Path

import pytest

from conftest import DSN, needs_db
from racetrap.cli import run, run_async
from racetrap.scenario import load

SCENARIO = """
app = "http://127.0.0.1:8766"
database = "${DATABASE_URL}"
command = 'python -m uvicorn shop:app --app-dir "APP_DIR" --port 8766 --log-level warning'
proxy = "127.0.0.1:6498"

init = [
    "DROP TABLE IF EXISTS bookings, memberships, users, accounts, shows, waitlist, counters, visits",
    "CREATE TABLE users (id serial PRIMARY KEY, name text NOT NULL)",
    "CREATE TABLE accounts (id serial PRIMARY KEY, name text NOT NULL)",
    "CREATE TABLE memberships (user_id int NOT NULL REFERENCES users, account_id int NOT NULL REFERENCES accounts)",
    "CREATE TABLE shows (id int PRIMARY KEY, seats_left int NOT NULL)",
    "CREATE TABLE waitlist (user_name text NOT NULL, joined_at timestamptz NOT NULL DEFAULT clock_timestamp())",
    "CREATE TABLE counters (id int PRIMARY KEY, n int NOT NULL)",
    "CREATE TABLE visits (id serial PRIMARY KEY, user_name text NOT NULL)",
]

setup = [
    "TRUNCATE memberships, users, accounts, shows, waitlist, counters, visits RESTART IDENTITY",
    "INSERT INTO shows VALUES (1, 1)",
    "INSERT INTO counters VALUES (1, 0)",
]

[[request]]
name = "alice"
path = "PATH"
json = { user = "alice" }

[[request]]
name = "bob"
path = "PATH"
json = { user = "bob" }
"""


@needs_db
@pytest.mark.parametrize("path", ["/signup", "/book", "/visit", "/take"])
def test_race_free_endpoint_passes(tmp_path, monkeypatch, path):
    monkeypatch.setenv("DATABASE_URL", DSN)
    text = SCENARIO.replace("APP_DIR", str(Path(__file__).parent)).replace("PATH", path)
    (tmp_path / "shop.toml").write_text(text, encoding="utf-8")
    scenario = load(tmp_path / "shop.toml")

    async def bounded():
        # a statement parked forever used to hang this run
        return await asyncio.wait_for(run(scenario), 120)

    result = run_async(bounded())
    if path == "/visit":
        # its writes happen after the response, so racetrap must say it could not test them
        assert any("not tested" in w for w in result.warnings)
    else:
        assert all(p.schedules for p in result.pairs), "every pair was explored"
    assert not [p for p in result.pairs if p.races]
    # B waiting on the app's lock is released after stall_ms, not after a 30 s timeout
    assert result.seconds < 30


@needs_db
def test_pairs_that_share_no_tables_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    text = SCENARIO.replace("PATH", "/signup", 1).replace("PATH", "/take").replace("APP_DIR", str(Path(__file__).parent))
    (tmp_path / "shop.toml").write_text(text, encoding="utf-8")
    result = run_async(asyncio.wait_for(run(load(tmp_path / "shop.toml")), 120))
    skipped = {(p.a.name, p.b.name) for p in result.pairs if p.skipped}
    # signing up writes users and accounts, taking writes counters, so only the same request can race with itself
    assert skipped == {("alice", "bob"), ("bob", "alice")}
    assert all(p.schedules for p in result.pairs if not p.skipped)
    assert not [p for p in result.pairs if p.races]
