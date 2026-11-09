from pathlib import Path

import pytest

from conftest import DSN, needs_db
from deadheat.cli import run, run_async
from deadheat.report import render
from deadheat.scenario import load

TICKETS = Path(__file__).parent.parent / "examples" / "tickets"


@needs_db
def test_finds_the_double_booking(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    scenario = load(TICKETS / "buggy.toml")
    result = run_async(run(scenario))
    assert len(result.pairs) == 4 and all(p.races for p in result.pairs)
    assert "4 of 4 pairs race" in render(scenario, result)


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
