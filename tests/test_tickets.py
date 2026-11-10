import dataclasses
import json
from pathlib import Path
from xml.etree import ElementTree

import psycopg
import pytest

from conftest import DSN, needs_db
from racetrap.cli import main, run, run_async, split
from racetrap.explore import Pair, Result, RunError
from racetrap.report import junit
from racetrap.scenario import Request, Scenario, load

TICKETS = Path(__file__).parent.parent / "examples" / "tickets"


@needs_db
def test_finds_the_double_booking(monkeypatch, tmp_path):
    """The whole command: exit code 1, an HTML report and the JSON results."""
    monkeypatch.setenv("DATABASE_URL", DSN)
    report, results = tmp_path / "report.html", tmp_path / "results.json"
    assert main([str(TICKETS / "buggy.toml"), "--report", str(report), "--json", str(results)]) == 1
    assert "4 of 4 pairs race" in report.read_text(encoding="utf-8")
    data = json.loads(results.read_text(encoding="utf-8"))
    assert data["racing_pairs"] == 4 and len(data["pairs"]) == 4
    pair = next(p for p in data["pairs"] if (p["a"], p["b"]) == ("alice books", "bob books"))
    assert pair["responses"] == {"a_then_b": {"A": 200, "B": 409}, "b_then_a": {"B": 200, "A": 409}}
    race = pair["races"][0]
    assert race["held_before"].startswith("UPDATE shows")
    assert race["responses"] == {"A": 200, "B": 200}
    assert "final data matches no serial order" in race["reasons"]
    # the example has no setup SQL, so racetrap restored the data itself and left it as it found it
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT seats_left FROM shows").fetchall() == [(1,)]
        assert db.execute("SELECT count(*) FROM bookings").fetchone() == (0,)
        assert db.execute("SELECT to_regnamespace('racetrap_snapshot')").fetchone() == (None,)


@needs_db
@pytest.mark.parametrize("path", ["/shows/1/book-locked", "/shows/1/book-atomic"])
def test_both_fixes_pass(monkeypatch, path):
    monkeypatch.setenv("DATABASE_URL", DSN)
    scenario = load(TICKETS / "buggy.toml")
    for r in scenario.requests:
        r.path = path
    result = run_async(run(scenario))
    assert all(p.schedules for p in result.pairs), "every pair was explored"
    assert not any(p.races for p in result.pairs)


PAIRS = [("alice books", "alice books"), ("alice books", "bob books"), ("bob books", "alice books"), ("bob books", "bob books")]


@needs_db
def test_workers_find_the_same_races(monkeypatch, tmp_path):
    """Two workers, each with its own copy of the database and of the app."""
    monkeypatch.setenv("DATABASE_URL", DSN)
    results, xml = tmp_path / "results.json", tmp_path / "results.xml"
    args = ["--report", str(tmp_path / "report.html"), "--json", str(results), "--junit", str(xml), "--workers", "2"]
    assert main([str(TICKETS / "buggy.toml"), *args]) == 1
    pairs = json.loads(results.read_text(encoding="utf-8"))["pairs"]
    assert [(p["a"], p["b"]) for p in pairs] == PAIRS, "the pairs are back in their usual order"
    assert all(p["races"] for p in pairs)
    suite = ElementTree.parse(xml).getroot()
    assert suite.get("tests") == suite.get("failures") == "4"
    assert "alice books was held before its UPDATE shows" in suite.find("testcase/failure").text
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM pg_database WHERE datname LIKE 'racetrap\\_1\\_%'").fetchone() == (0,), "the copy is dropped"
        assert db.execute("SELECT seats_left FROM shows").fetchall() == [(1,)]


def test_each_worker_gets_its_own_database_proxy_and_app():
    scenario = Scenario(
        path=None,
        app="http://127.0.0.1:8000",
        database="postgresql://u:p@db:5432/shop?sslmode=require",
        requests=[Request("a", "GET", "/")],
        command="uvicorn app:app --port 8000",
    )
    first, second = split(scenario, ["racetrap_1_shop"])
    assert (first.app, first.database, first.proxy, first.command) == (scenario.app, scenario.database, scenario.proxy, scenario.command)
    assert second.app == "http://127.0.0.1:8001"
    assert second.command == "uvicorn app:app --port 8001"
    assert second.proxy == ("127.0.0.1", 6500)
    assert second.database == "postgresql://u:p@db:5432/racetrap_1_shop?sslmode=require"
    # a login writes tokens into the requests, so workers must not share them
    assert second.requests == first.requests and second.requests[0] is not first.requests[0]
    with pytest.raises(RunError, match="needs command"):
        split(dataclasses.replace(scenario, command=None), ["racetrap_1_shop"])


def test_junit_marks_a_skipped_pair():
    a = Request("a", "GET", "/a")
    scenario = Scenario(path=Path("shop.toml"), app="", database="", requests=[a])
    suite = ElementTree.fromstring(junit(scenario, Result([Pair(a, a, [], [], set(), skipped=True)], set(), {}, 0, 0.0, [])))
    assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("1", "0", "1")
    assert suite.find("testcase").get("name") == "a vs a" and suite.find("testcase/skipped") is not None


@needs_db
def test_pytest_runs_a_scenario_as_one_test_per_pair(pytester, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    result = pytester.runpytest(str(TICKETS / "buggy.toml"))
    result.assert_outcomes(failed=4)
    result.stdout.fnmatch_lines(["*alice books was held before its UPDATE shows*", "Report: *racetrap-report-buggy.html"])
    assert "4 of 4 pairs race" in (pytester.path / "racetrap-report-buggy.html").read_text(encoding="utf-8")
    pytester.runpytest(str(TICKETS / "fixed.toml")).assert_outcomes(passed=4)


def test_pytest_only_runs_scenarios_it_is_pointed_at(pytester):
    """A scenario empties tables, so a plain pytest run must not pick one up."""
    pytester.makefile(".toml", racetrap='app = "http://127.0.0.1:1"')
    assert pytester.runpytest().ret == pytest.ExitCode.NO_TESTS_COLLECTED
    result = pytester.runpytest("racetrap.toml")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*add at least one*request*"])
