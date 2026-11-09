import json
from pathlib import Path

import pytest

from conftest import DSN, needs_db
from racetrap.cli import main, run, run_async
from racetrap.scenario import load

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
