"""One self contained HTML report: for each race, the order, the outcome and the fix."""

import html
import json
import re
from collections import Counter
from datetime import datetime
from typing import Any
from xml.etree import ElementTree

from . import __version__
from .explore import POOL_RESET, Event, Pair, Result, Schedule, normalize, shape
from .scenario import Column, Scenario

Fks = dict[Column, Column]

LOCKING_READ = re.compile(r"\bFOR\s+(NO\s+KEY\s+UPDATE|UPDATE|SHARE|KEY\s+SHARE)\b", re.I)
VERB = re.compile(r"\s*([A-Za-z]+)")
COUNTED = re.compile(r"^(?:SELECT|UPDATE|DELETE|INSERT \d+|MERGE|COPY|FETCH|MOVE) (\d+)$")
ROWS = re.compile(r"^\d+ rows?$")


def render(scenario: Scenario, result: Result) -> str:
    racing = [p for p in result.pairs if p.races]
    calm = [p for p in result.pairs if not p.races]
    # B vs A tells the same story as A vs B, so it becomes a note on that card
    keys = {(p.a.name, p.b.name) for p in racing}
    shown = [p for p in racing if p.a is p.b or (p.b.name, p.a.name) not in keys or p.a.name < p.b.name]
    shown.sort(key=lambda p: p.a is p.b)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        '<header class="top">',
        f'<div class="brand">{ICON}<span>racetrap</span><span class="ver">{__version__}</span></div>',
        f'<div class="file">{esc(scenario.path.name)}<span>{stamp}</span></div>',
        "</header>",
    ]
    if racing:
        parts.append(f'<h1 class="bad">{len(racing)} of {len(result.pairs)} pairs race</h1>')
        parts.append(
            '<p class="intro">These requests can end in a state that running them one at a time never reaches. '
            "Each card shows the order racetrap forced, what broke, and where your code needs a fix.</p>"
        )
    else:
        parts.append(f"<h1>No races in {len(result.pairs)} pairs</h1>")
        parts.append('<p class="intro">Every order racetrap forced ended the same way as running the requests one at a time.</p>')
    ignored = ", ".join(f"{t}.{c}" for t, c in sorted(result.ignored)) or "none"
    facts = [("Runs", result.runs), ("Time", f"{result.seconds:.1f} s"), ("App", scenario.app), ("Ignored columns", ignored)]
    parts.append('<dl class="facts">' + "".join(f"<div><dt>{k}</dt><dd>{esc(v)}</dd></div>" for k, v in facts) + "</dl>")
    if result.warnings:
        items = "".join(f"<li>{esc(w)}</li>" for w in result.warnings)
        parts.append(f'<section class="warn"><h2>Check these</h2><ul>{items}</ul></section>')
    parts += [
        race_card(p, result.fks, mirrored=(p.b.name, p.a.name) in keys and p.a is not p.b)
        for p in shown
    ]
    if calm:
        items = "".join(
            f'<div class="pass"><span><b>{esc(p.a.name)}</b> vs <b>{esc(names(p)[1])}</b></span>'
            f"<em>{passed(p)}</em></div>"
            for p in calm
        )
        parts.append(f'<section class="passed"><h2>Passed</h2><div class="pass-grid">{items}</div></section>')
    parts.append(
        '<footer>racetrap checks the transactions and locks you already wrote. '
        "It does not replace them, and it never changes your code.</footer>"
    )
    return PAGE.replace("{body}", "\n".join(parts))


def passed(pair: Pair) -> str:
    if pair.skipped:
        return "skipped, the two share no table that either one writes"
    return f"{len(pair.schedules)} orders, all serializable"


def summary(scenario: Scenario, result: Result) -> dict[str, Any]:
    """The result as plain data, for --json."""
    return {
        "racetrap": __version__,
        "scenario": scenario.path.name,
        "app": scenario.app,
        "runs": result.runs,
        "seconds": round(result.seconds, 2),
        "racing_pairs": sum(bool(p.races) for p in result.pairs),
        "warnings": result.warnings,
        "ignored_columns": [f"{t}.{c}" for t, c in sorted(result.ignored)],
        "pairs": [
            {
                "a": p.a.name,
                "b": p.b.name,
                "skipped": p.skipped,
                "orders": len(p.schedules),
                "responses": {"a_then_b": p.serials[0].statuses, "b_then_a": p.serials[1].statuses} if p.serials else None,
                "races": [
                    {"held_before": r.hold[0], "occurrence": r.hold[1], "responses": r.outcome.statuses, "reasons": r.reasons}
                    for r in p.races
                ],
            }
            for p in result.pairs
        ],
    }


def explain(pair: Pair) -> str:
    """Why a pair races, as plain text."""
    return "\n".join(
        f"{pair.a.name} was held before its {short(r.hold[0])}{f' (time {r.hold[1]})' if r.hold[1] > 1 else ''} while {pair.b.name} ran: "
        f"{', '.join(r.reasons)}. A answered {r.outcome.statuses['A']}, B answered {r.outcome.statuses['B']}."
        for r in pair.races
    )


def junit(scenario: Scenario, result: Result) -> str:
    """The result as JUnit XML, one test case per pair."""
    racing = sum(bool(p.races) for p in result.pairs)
    skipped = sum(p.skipped for p in result.pairs)
    suite = ElementTree.Element(
        "testsuite", name="racetrap", tests=str(len(result.pairs)), failures=str(racing), errors="0", skipped=str(skipped), time=f"{result.seconds:.2f}"
    )
    for p in result.pairs:
        case = ElementTree.SubElement(suite, "testcase", classname=scenario.path.stem, name=f"{p.a.name} vs {p.b.name}")
        if p.skipped:
            ElementTree.SubElement(case, "skipped", message=passed(p))
        elif p.races:
            failure = ElementTree.SubElement(case, "failure", message=f"{len(p.races)} of {len(p.schedules)} orders race")
            failure.text = explain(p)
    return ElementTree.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"


def names(pair: Pair) -> tuple[str, str]:
    a, b = pair.a.name, pair.b.name
    return a, (f"{b} again" if pair.a is pair.b else b)


def race_card(pair: Pair, fks: Fks, mirrored: bool) -> str:
    race = pair.races[0]
    a, b = names(pair)
    sql, _ = race.hold
    mirror = " It also races with the roles swapped." if mirrored else ""
    rows = data_diff(pair, race, fks, a, b)
    return "\n".join([
        '<section class="card">',
        '<div class="card-head"><span class="tag">Race</span>'
        f'<h2>{esc(a)} <span class="vs">vs</span> {esc(b)}</h2>'
        f'<span class="count">{len(pair.races)} of {len(pair.schedules)} orders race</span></div>',
        f'<p class="lede">racetrap held <b>A</b> right before <code>{esc(short(sql, 80))}</code> '
        f"and let <b>B</b> run first.{mirror}</p>",
        lanes(race.outcome.events, sql, a, b),
        '<div class="split">',
        f'<div><h3>Outcome</h3>{outcome(pair, race, fks)}'
        f'<ul class="reasons">{"".join(f"<li>{esc(r)}</li>" for r in race.reasons)}</ul></div>',
        f'<div><h3>Fix it in your code</h3>{fix(pair, race, a, b)}</div>',
        "</div>",
        f"<details><summary>Rows in each order</summary>{rows}</details>" if rows else "",
        bodies(pair, race),
        f"<details><summary>Every statement, in order</summary>{timeline(race.outcome.events, sql, a, b)}</details>",
        "</section>",
    ])


def lanes(events: list[Event], held: str, a: str, b: str) -> str:
    """A and B as two tracks, each statement a chip in the order Postgres saw it."""
    steps: list[dict[str, Any]] = []
    by_conn: dict[int, dict[str, Any]] = {}
    gate: int | None = None
    released: int | None = None
    for e in events:
        if e.who == "bg":
            continue
        if e.kind in ("ok", "error"):
            if e.conn in by_conn:
                by_conn[e.conn]["results"].append(e)
            continue
        step: dict[str, Any] = {"who": e.who, "kind": e.kind, "text": e.text, "results": []}
        if e.kind == "hold":
            gate = len(steps)
        else:
            by_conn[e.conn] = step
            if gate is not None and released is None and e.who == "A" and shape(e.text) == held:
                released = len(steps)
                step["released"] = True
        steps.append(step)
    cells = [
        f'<div class="lane-name" style="grid-row:1"><b>A</b>{esc(a)}</div>',
        f'<div class="lane-name" style="grid-row:2"><b>B</b>{esc(b)}</div>',
        '<div class="track" style="grid-row:1"></div><div class="track" style="grid-row:2"></div>',
    ]
    if gate is not None:
        end = released + 2 if released is not None else len(steps) + 2
        cells.append(f'<div class="wait" style="grid-column:{gate + 2} / {end}"></div>')
    for i, s in enumerate(steps):
        row = 1 if s["who"] == "A" else 2
        if s["kind"] == "hold":
            cells.append(f'<div class="gate" style="grid-column:{i + 2}"><span>held</span></div>')
            continue
        match = VERB.match(s["text"])
        verb = "RESET" if POOL_RESET.search(s["text"]) else (match[1].upper() if match else "SQL")
        errors = [e.text[:5] for e in s["results"] if e.kind == "error"]
        res = f"error {errors[0]}" if errors else "" if verb == "RESET" else rows_label(s["results"])
        cls = "step" + (" released" if s.get("released") else "") + (" err" if errors else "") + (" quiet" if verb == "RESET" else "")
        mark = '<i>released</i>' if s.get("released") else ""
        cells.append(
            f'<div class="{cls}" style="grid-row:{row};grid-column:{i + 2}" title="{esc(s["text"])}">'
            f'{mark}<span class="verb">{esc(verb)}</span><span class="res">{esc(res)}</span></div>'
        )
    return (
        f'<div class="lanes-wrap"><div class="lanes" style="grid-template-columns:132px repeat({len(steps)}, minmax(62px, auto))">'
        + "".join(cells)
        + "</div></div>"
    )


def rows_label(results: list[Event]) -> str:
    """'1 row' from a CommandComplete tag. BEGIN and COMMIT say nothing new."""
    tag = results[0].text if results else ""
    counted = COUNTED.match(tag)
    if not counted:
        # the proxy already words a suspended portal as "1 row"
        return tag if ROWS.match(tag) else ""
    n = int(counted[1])
    return f"{n} row" + ("" if n == 1 else "s")


def outcome(pair: Pair, race: Schedule, fks: Fks) -> str:
    ab, ba = pair.serials
    forced = normalize(race.outcome.snapshot, pair.ignored, fks)
    serial = [normalize(s.snapshot, pair.ignored, fks) for s in pair.serials]
    rows: list[tuple[str, str | int, str | int, str | int, bool | None]] = [
        (f"A {esc(pair.a.name)}", status(ab.statuses["A"]), status(ba.statuses["A"]), status(race.outcome.statuses["A"]), None),
        (f"B {esc(names(pair)[1])}", status(ab.statuses["B"]), status(ba.statuses["B"]), status(race.outcome.statuses["B"]), None),
    ]
    for table, now in forced.items():
        if any(now == s[table] for s in serial):
            continue
        counts = [sum(s[table].values()) for s in serial]
        rows.append((f"{esc(table)} rows", counts[0], counts[1], sum(now.values()), True))
    body = []
    for label, x, y, z, changed in rows:
        bad = changed or z not in (x, y)
        body.append(f"<tr><th>{label}</th><td>{x}</td><td>{y}</td><td class=\"forced{' bad' if bad else ''}\">{z}</td></tr>")
    return (
        '<table class="outcome"><tr><th></th><th>A then B</th><th>B then A</th><th class="forced">racetrap\'s order</th></tr>'
        + "".join(body)
        + "</table>"
    )


def bodies(pair: Pair, race: Schedule) -> str:
    """What each request answered in each order, side by side."""
    orders = [("A then B", pair.serials[0], ""), ("B then A", pair.serials[1], ""), ("racetrap's order", race.outcome, " bad")]
    if not any(o.bodies.get(role) for _, o, _ in orders for role in "AB"):
        return ""
    out = []
    for role in "AB":
        cells = "".join(f'<div class="state{cls}"><h4>{label}</h4><pre>{esc(pretty(o.bodies.get(role)))}</pre></div>' for label, o, cls in orders)
        out.append(f'<p class="table-name">{role}</p><div class="states">{cells}</div>')
    return "<details><summary>Response bodies in each order</summary>" + "".join(out) + "</details>"


def pretty(body: str | None, limit: int = 600) -> str:
    """A body laid out for reading, cut to a sane length."""
    if not body:
        return ""
    try:
        body = json.dumps(json.loads(body), indent=2)
    except ValueError:
        pass
    return body if len(body) <= limit else body[:limit] + "\n…"


def data_diff(pair: Pair, schedule: Schedule, fks: Fks, a: str, b: str) -> str:
    ignored = pair.ignored
    forced = normalize(schedule.outcome.snapshot, ignored, fks)
    serial = [normalize(s.snapshot, ignored, fks) for s in pair.serials]
    out = []
    for table, rows in forced.items():
        if all(rows == s[table] for s in serial):
            continue
        cols = [c for c in schedule.outcome.snapshot[table][0] if (table, c) not in ignored]
        states = [("A then B", serial[0][table], ""), ("B then A", serial[1][table], ""), ("racetrap's order", rows, " bad")]
        cells = "".join(f'<div class="state{cls}"><h4>{label}</h4>{rows_table(cols, r)}</div>' for label, r, cls in states)
        out.append(f'<p class="table-name">{esc(table)}</p><div class="states">{cells}</div>')
    return "".join(out)


def rows_table(cols: list[str], rows: Counter[tuple[str, ...]], limit: int = 8) -> str:
    items = sorted(rows.elements())
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in row) + "</tr>" for row in items[:limit])
    more = f'<p class="more">and {len(items) - limit} more</p>' if len(items) > limit else ""
    if not items:
        body = f'<tr><td colspan="{len(cols)}" class="empty">no rows</td></tr>'
    return f'<table class="rows"><tr>{head}</tr>{body}</table>{more}'


def timeline(events: list[Event], held: str, a: str, b: str) -> str:
    rows: list[dict[str, Any]] = []
    open_rows: dict[int, dict[str, Any]] = {}
    waiting = False
    for e in (e for e in events if e.who != "bg"):
        if e.kind in ("ok", "error") and e.conn in open_rows:
            row = open_rows[e.conn]
            # racetrap polls for lock waits every 20 ms, so anything quicker
            # is plain concurrency and stays on its statement's row
            if row is rows[-1] or e.t - row["t"] < 0.015:
                row["results"].append(e)
                continue
        row = {"kind": e.kind, "who": e.who, "conn": e.conn, "text": e.text, "t": e.t, "results": []}
        if e.kind in ("ok", "error"):
            row["results"].append(e)
        if e.kind != "hold":
            open_rows[e.conn] = row
        row["released"] = waiting and e.kind == "sql" and e.who == "A" and shape(e.text) == held
        waiting = (waiting or e.kind == "hold") and not row["released"]
        rows.append(row)
    body = []
    t0 = rows[0]["t"] if rows else 0
    for r in rows:
        cell = cell_html(r)
        cols = (cell, "") if r["who"] == "A" else ("", cell)
        cls = ' class="held"' if r["kind"] == "hold" else ""
        ms = (r["t"] - t0) * 1000
        body.append(f'<tr{cls}><td class="t">{ms:.1f} ms</td><td>{cols[0]}</td><td>{cols[1]}</td></tr>')
    return (
        f'<table class="log"><tr><th></th><th>A {esc(a)}</th><th>B {esc(b)}</th></tr>'
        + "".join(body)
        + "</table>"
    )


def cell_html(row: dict[str, Any]) -> str:
    if row["kind"] == "hold":
        return f'<span class="flag">held before</span><code>{esc(short(row["text"]))}</code>'
    label = '<span class="flag">released</span>' if row["released"] else ""
    code = "" if row["kind"] != "sql" else f'<code title="{esc(row["text"])}">{esc(short(row["text"]))}</code>'
    results = ", ".join(
        f'<b class="err">{esc(e.text)}</b>' if e.kind == "error" else esc(e.text) for e in row["results"]
    )
    late = " late" if row["kind"] != "sql" else ""
    return label + code + (f'<span class="res{late}">{results}</span>' if results else "")


def fix(pair: Pair, schedule: Schedule, a: str, b: str) -> str:
    sql, n = schedule.hold
    seen, cut = 0, len(pair.trace)
    for i, s in enumerate(pair.trace):
        seen += s == sql
        if seen == n:
            cut = i
            break
    reads = [
        s for s in pair.trace[:cut]
        if s.lstrip().upper().startswith(("SELECT", "WITH")) and not LOCKING_READ.search(s)
    ]
    if reads:
        why = (
            f"<p>A read with <code>{esc(short(reads[-1], 70))}</code> and wrote based on it. "
            "B changed that data in between, so A's write used a stale value. "
            "The transaction is fine. It just does not lock what it reads.</p>"
        )
    else:
        why = "<p>The writes of A and B interleaved, and the transaction does not stop that on its own.</p>"
    return why + (
        '<ol class="fixes">'
        "<li>Lock the row you read: add <code>FOR UPDATE</code> to that <code>SELECT</code>.</li>"
        "<li>Check and write in one statement: <code>UPDATE ... WHERE stock &gt; 0 RETURNING ...</code>, "
        "and treat zero rows as a conflict.</li>"
        "<li>Run the transaction as <code>SERIALIZABLE</code> and retry on error 40001.</li>"
        "</ol>"
        '<p class="after">Run racetrap again after the fix. It passes when every order is serializable.</p>'
    )


def status(code: int) -> str:
    return "none" if code == 0 else esc(code)


def short(sql: str, limit: int = 110) -> str:
    flat = " ".join(sql.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def esc(value: object) -> str:
    return html.escape(str(value))


ICON = (
    '<svg viewBox="0 0 512 512" aria-hidden="true">'
    '<rect width="512" height="512" rx="112" fill="#0E0E10"/>'
    '<path d="M118 222 L146 90 L240 164 Z" fill="#8F8B84"/>'
    '<path d="M394 222 L366 90 L272 164 Z" fill="#8F8B84"/>'
    '<path d="M150 196 L160 128 L208 166 Z" fill="#1B1B1E"/>'
    '<path d="M362 196 L352 128 L304 166 Z" fill="#1B1B1E"/>'
    '<path d="M398 246 L486 212 L454 270 Z" fill="#E5483D"/>'
    '<path d="M400 270 L474 318 L414 306 Z" fill="#B3352C"/>'
    '<path d="M256 136 C320 136 368 160 390 208 L440 280 L394 300 C382 372 324 422 256 422 C188 422 130 372 118 300 L72 280 L122 208 C144 160 192 136 256 136 Z" fill="#DBD8D2"/>'
    '<path d="M256 300 C300 300 322 330 318 362 C312 398 284 422 256 422 C228 422 200 398 194 362 C190 330 212 300 256 300 Z" fill="#F4F2ED"/>'
    '<path d="M90 262 L188 212 L256 246 L324 212 L422 262 C402 308 332 322 256 292 C180 322 110 308 90 262 Z" fill="#E5483D"/>'
    '<path d="M164 254 Q200 250 230 266 Q216 290 182 284 Q166 272 164 254 Z" fill="#F7F5F0"/>'
    '<path d="M348 254 Q312 250 282 266 Q296 290 330 284 Q346 272 348 254 Z" fill="#F7F5F0"/>'
    '<circle cx="204" cy="269" r="9.5" fill="#0E0E10"/>'
    '<circle cx="308" cy="269" r="9.5" fill="#0E0E10"/>'
    '<path d="M236 326 Q256 318 276 326 Q270 346 256 350 Q242 346 236 326 Z" fill="#0E0E10"/>'
    "</svg>"
)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>racetrap report</title>
<style>
:root {
  --bg: #101113; --surface: #16171A; --raise: #1D1E22; --line: #2A2B30;
  --ink: #ECEAE5; --muted: #8D8A84; --faint: #5E5B56;
  --red: #E8564C; --red-soft: rgba(232,86,76,0.12); --red-line: rgba(232,86,76,0.5);
  --code: #222328;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #F6F5F2; --surface: #FFFFFF; --raise: #F2F0EC; --line: #E4E1DB;
    --ink: #1C1B19; --muted: #6B6761; --faint: #A39E96;
    --red: #C2342E; --red-soft: #FBEAE8; --red-line: rgba(194,52,46,0.45);
    --code: #F1EFEB;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 28px 28px 56px; }
code, .verb, .res, .t, .outcome td, .rows td { font-family: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, monospace; }
code { font-size: 12.5px; background: var(--code); padding: 2px 6px; border-radius: 5px; }
.top { display: flex; align-items: center; justify-content: space-between; padding-bottom: 22px; border-bottom: 1px solid var(--line); margin-bottom: 44px; }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 650; font-size: 17px; }
.brand svg { width: 30px; height: 30px; }
.brand .ver, .file span { color: var(--muted); font-weight: 400; font-size: 13px; }
.file { font-family: ui-monospace, Consolas, monospace; font-size: 13px; display: flex; gap: 14px; }
h1 { font-size: 46px; line-height: 1.05; margin: 0; font-weight: 680; letter-spacing: -0.025em; }
h1.bad { color: var(--red); }
.intro { color: var(--muted); max-width: 640px; margin: 14px 0 26px; font-size: 16px; }
.facts { display: flex; flex-wrap: wrap; gap: 10px 36px; margin: 0 0 40px; }
.facts div { min-width: 0; }
.facts dt { font-size: 12px; color: var(--muted); }
.facts dd { margin: 2px 0 0; font-size: 14px; font-family: ui-monospace, Consolas, monospace; overflow-wrap: anywhere; }
.warn { border: 1px solid var(--line); border-left: 3px solid var(--red); border-radius: 10px; padding: 14px 18px; margin-bottom: 28px; background: var(--surface); }
.warn h2 { font-size: 14px; margin: 0 0 6px; }
.warn ul { margin: 0; padding-left: 18px; color: var(--muted); font-size: 14px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 18px; padding: 26px 28px 18px; margin-bottom: 24px; }
.card-head { display: flex; align-items: center; gap: 12px; }
.card-head h2 { font-size: 21px; margin: 0; font-weight: 640; letter-spacing: -0.01em; }
.vs { color: var(--muted); font-weight: 400; margin: 0 2px; }
.tag { font-size: 12px; font-weight: 650; color: var(--red); background: var(--red-soft); padding: 3px 10px; border-radius: 999px; }
.count { margin-left: auto; color: var(--muted); font-size: 13px; }
.lede { color: var(--muted); margin: 8px 0 18px; }
.lede b { color: var(--ink); }
.lanes-wrap { overflow-x: auto; margin: 0 -28px; padding: 16px 28px 10px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); background: var(--bg); }
.lanes { display: grid; grid-template-rows: 74px 74px; align-items: center; column-gap: 6px; position: relative; min-width: max-content; }
.lane-name { grid-column: 1; font-size: 13px; color: var(--muted); display: flex; align-items: center; gap: 8px; padding-right: 10px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.lane-name b { width: 22px; height: 22px; border-radius: 6px; background: var(--ink); color: var(--bg); display: inline-flex; align-items: center; justify-content: center; font-size: 12px; flex: none; }
.track { grid-column: 2 / -1; height: 2px; background: var(--line); align-self: center; }
.step { position: relative; z-index: 1; justify-self: start; background: var(--raise); border: 1px solid var(--line); border-radius: 9px; padding: 6px 9px 5px; min-width: 60px; }
.step .verb { display: block; font-size: 11.5px; font-weight: 600; letter-spacing: 0.02em; }
.step .res { display: block; font-size: 10.5px; color: var(--muted); margin-top: 1px; white-space: nowrap; min-height: 14px; }
.step.quiet { opacity: 0.5; }
.step.released { border-color: var(--red-line); }
.step.err { border-color: var(--red); }
.step.err .res { color: var(--red); }
.step i { position: absolute; top: -17px; left: 2px; font-style: normal; font-size: 10.5px; color: var(--red); white-space: nowrap; }
.gate { grid-row: 1; z-index: 2; justify-self: center; width: 4px; height: 60px; border-radius: 2px; background: var(--red); position: relative; }
.gate span { position: absolute; top: -18px; left: 50%; transform: translateX(-50%); font-size: 10.5px; color: var(--red); white-space: nowrap; }
.wait { grid-row: 1; height: 2px; background: var(--red); align-self: center; z-index: 1; opacity: 0.7; }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 36px; margin: 22px 0 6px; }
h3 { font-size: 13px; color: var(--muted); font-weight: 600; margin: 0 0 10px; }
table { border-collapse: collapse; width: 100%; }
.outcome th, .outcome td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); font-size: 14px; }
.outcome th { font-weight: 500; color: var(--muted); font-size: 12.5px; }
.outcome tr th:first-child { color: var(--ink); font-size: 13.5px; font-weight: 500; }
.outcome .forced { background: var(--raise); }
.outcome .forced.bad { color: var(--red); background: var(--red-soft); font-weight: 600; }
.reasons { list-style: none; padding: 0; margin: 12px 0 0; display: flex; flex-wrap: wrap; gap: 6px; }
.reasons li { font-size: 12px; color: var(--red); border: 1px solid var(--red-line); border-radius: 999px; padding: 2px 10px; }
.fixes { padding-left: 20px; margin: 10px 0; }
.fixes li { margin: 5px 0; }
.after { color: var(--muted); font-size: 13.5px; margin: 8px 0 0; }
details { border-top: 1px solid var(--line); margin-top: 14px; padding-top: 12px; }
summary { cursor: pointer; color: var(--muted); font-size: 13.5px; }
summary:hover { color: var(--ink); }
.table-name { font-family: ui-monospace, Consolas, monospace; font-size: 13px; margin: 14px 0 8px; }
.states { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.state { border: 1px solid var(--line); border-radius: 10px; padding: 10px; overflow-x: auto; }
.state.bad { border-color: var(--red-line); background: var(--red-soft); }
.state h4 { font-size: 12px; margin: 0 0 6px; font-weight: 600; color: var(--muted); }
.state pre { margin: 0; font: 12px/1.5 ui-monospace, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.state.bad h4 { color: var(--red); }
.rows th, .rows td { text-align: left; padding: 4px 6px; font-size: 12.5px; border-bottom: 1px solid var(--line); }
.rows th { font-weight: 500; color: var(--muted); }
.empty, .more { color: var(--muted); font-size: 12px; }
.log { margin-top: 10px; table-layout: fixed; }
.log th, .log td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); vertical-align: top; font-size: 13px; }
.log th { font-weight: 500; color: var(--muted); }
.log th:first-child, .log td.t { width: 74px; }
.log td.t { color: var(--muted); font-size: 11.5px; white-space: nowrap; }
.log code { background: none; padding: 0; overflow-wrap: anywhere; }
.log tr.held td { background: var(--red-soft); }
.flag { display: block; color: var(--red); font-size: 11px; margin-bottom: 2px; }
.res.late::before { content: "after waiting: "; }
.res .err { color: var(--red); font-weight: 600; }
.log .res { display: block; color: var(--muted); font-size: 11.5px; }
.passed h2 { font-size: 15px; margin: 36px 0 12px; }
.pass-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; }
.pass { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; display: flex; flex-direction: column; gap: 2px; font-size: 14px; }
.pass b { font-weight: 600; }
.pass em { font-style: normal; color: var(--muted); font-size: 12.5px; }
footer { color: var(--muted); font-size: 13px; margin-top: 44px; padding-top: 18px; border-top: 1px solid var(--line); }
@media (max-width: 760px) {
  main { padding: 20px 16px 40px; }
  h1 { font-size: 32px; }
  .card { padding: 20px 16px 14px; }
  .lanes-wrap { margin: 0 -16px; padding: 6px 16px 10px; }
  .split, .states { grid-template-columns: 1fr; gap: 20px; }
  .count { display: none; }
}
</style>
</head>
<body><main>
{body}
</main></body>
</html>
"""
