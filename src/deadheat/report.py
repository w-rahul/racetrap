"""One self contained HTML file: for each race, the order, the outcome and the fix."""

import html
import re
from datetime import datetime

from . import __version__
from .explore import normalize

LOCKING_READ = re.compile(r"\bFOR\s+(NO\s+KEY\s+UPDATE|UPDATE|SHARE|KEY\s+SHARE)\b", re.I)


def render(scenario, result):
    racing = [p for p in result.pairs if p.races]
    calm = [p for p in result.pairs if not p.races]
    # B vs A tells the same story as A vs B, so it becomes a note on that card
    keys = {(p.a.name, p.b.name) for p in racing}
    shown = [p for p in racing if p.a is p.b or (p.b.name, p.a.name) not in keys or p.a.name < p.b.name]
    shown.sort(key=lambda p: p.a is p.b)
    if racing:
        verdict = f'<h1 class="race">{len(racing)} of {len(result.pairs)} pairs race</h1>'
    else:
        verdict = f"<h1>No races in {len(result.pairs)} pairs</h1>"
    ignored = ", ".join(f"{t}.{c}" for t, c in sorted(result.ignored)) or "none"
    meta = (
        f"{result.runs} runs in {result.seconds:.1f}s · app {esc(scenario.app)} · "
        f"ignored columns: {esc(ignored)}"
    )
    parts = [
        f'<header><p class="mark">deadheat {__version__} · {esc(scenario.path.name)}</p>',
        verdict,
        f'<p class="meta">{meta}</p></header>',
    ]
    if result.warnings:
        items = "".join(f"<li>{esc(w)}</li>" for w in result.warnings)
        parts.append(f'<section class="warn"><h2>Warnings</h2><ul>{items}</ul></section>')
    parts += [
        race_section(p, result.ignored, mirrored=(p.b.name, p.a.name) in keys and p.a is not p.b, first=i == 0)
        for i, p in enumerate(shown)
    ]
    if calm:
        items = "".join(
            f"<li><span>{esc(p.a.name)}</span> vs <span>{esc(names(p)[1])}</span>"
            f"<em>{len(p.schedules)} orders, all serializable</em></li>"
            for p in calm
        )
        parts.append(f'<section class="calm"><h2>No race</h2><ul>{items}</ul></section>')
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts.append(f"<footer>Generated {stamp}</footer>")
    return PAGE.replace("{body}", "\n".join(parts))


def names(pair):
    a, b = pair.a.name, pair.b.name
    return a, (f"{b} again" if pair.a is pair.b else b)


def race_section(pair, ignored, mirrored, first):
    race = pair.races[0]
    a, b = names(pair)
    sql, _ = race.hold
    mirror = " It also races with the roles swapped." if mirrored else ""
    out = [
        "<section class=\"card\">",
        f'<h2><span class="tag">race</span>{esc(a)} vs {esc(b)}</h2>',
        f'<p class="lede">Held <b>{esc(a)}</b> right before <code>{esc(short(sql))}</code> '
        f"and let <b>{esc(b)}</b> run. {len(pair.races)} of {len(pair.schedules)} orders "
        f"gave an outcome no serial order can give.{mirror}</p>",
        f'<ul class="reasons">{"".join(f"<li>{esc(r)}</li>" for r in race.reasons)}</ul>',
        "<h3>Responses</h3>",
        responses(pair, race, a, b),
    ]
    data = data_diff(pair, race, ignored, a, b)
    if data:
        out += ["<h3>Final data</h3>", data]
    steps = timeline(race.outcome.events, race.hold[0], a, b)
    out.append(f'<details{" open" if first else ""}><summary>What happened</summary>{steps}</details>')
    out += ["<h3>How to fix it</h3>", fix(pair, race, a, b), "</section>"]
    return "\n".join(out)


def responses(pair, schedule, a, b):
    rows = [
        (f"{a}, then {b}", pair.serials[0].statuses, ""),
        (f"{b}, then {a}", pair.serials[1].statuses, ""),
        ("deadheat's order", schedule.outcome.statuses, ' class="bad"'),
    ]
    body = "".join(
        f"<tr{cls}><th>{esc(label)}</th><td>{status(s['A'])}</td><td>{status(s['B'])}</td></tr>"
        for label, s, cls in rows
    )
    return f'<table class="grid"><tr><th></th><th>{esc(a)}</th><th>{esc(b)}</th></tr>{body}</table>'


def data_diff(pair, schedule, ignored, a, b):
    forced = normalize(schedule.outcome.snapshot, ignored)
    serial = [normalize(s.snapshot, ignored) for s in pair.serials]
    out = []
    for table, rows in forced.items():
        if all(rows == s[table] for s in serial):
            continue
        cols = [c for c in schedule.outcome.snapshot[table][0] if (table, c) not in ignored]
        states = [
            (f"{a}, then {b}", serial[0][table], ""),
            (f"{b}, then {a}", serial[1][table], ""),
            ("deadheat's order", rows, " bad"),
        ]
        cells = "".join(f'<div class="state{cls}"><h4>{esc(label)}</h4>{rows_table(cols, r)}</div>' for label, r, cls in states)
        out.append(f'<p class="table-name">{esc(table)}</p><div class="states">{cells}</div>')
    return "".join(out)


def rows_table(cols, rows, limit=8):
    items = sorted(rows.elements())
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in row) + "</tr>" for row in items[:limit])
    more = f'<p class="more">and {len(items) - limit} more</p>' if len(items) > limit else ""
    if not items:
        body = f'<tr><td colspan="{len(cols)}" class="empty">no rows</td></tr>'
    return f'<table class="rows"><tr>{head}</tr>{body}</table>{more}'


def timeline(events, held, a, b):
    rows, open_rows, waiting = [], {}, False
    for e in events:
        if e.kind in ("ok", "error") and e.conn in open_rows:
            row = open_rows[e.conn]
            # deadheat polls for lock waits every 20 ms, so anything quicker
            # is plain concurrency and stays on its statement's row
            if row is rows[-1] or e.t - row["t"] < 0.015:
                row["results"].append(e)
                continue
        row = {"kind": e.kind, "who": e.who, "conn": e.conn, "text": e.text, "t": e.t, "results": []}
        if e.kind in ("ok", "error"):
            row["results"].append(e)
        if e.kind != "hold":
            open_rows[e.conn] = row
        row["released"] = waiting and e.kind == "sql" and e.who == "A" and e.text == held
        waiting = (waiting or e.kind == "hold") and not row["released"]
        rows.append(row)
    body = []
    t0 = rows[0]["t"] if rows else 0
    for r in rows:
        cell = cell_html(r)
        lanes = (cell, "") if r["who"] == "A" else ("", cell)
        cls = ' class="held"' if r["kind"] == "hold" else ""
        ms = (r["t"] - t0) * 1000
        body.append(f'<tr{cls}><td class="t">{ms:.1f} ms</td><td>{lanes[0]}</td><td>{lanes[1]}</td></tr>')
    return (
        f'<table class="lanes"><tr><th></th><th>A · {esc(a)}</th><th>B · {esc(b)}</th></tr>'
        + "".join(body)
        + "</table>"
    )


def cell_html(row):
    if row["kind"] == "hold":
        return f'<span class="pause">held before</span><code>{esc(short(row["text"]))}</code>'
    label = '<span class="pause">released</span>' if row["released"] else ""
    code = "" if row["kind"] != "sql" else f'<code title="{esc(row["text"])}">{esc(short(row["text"]))}</code>'
    results = " · ".join(
        f'<b class="err">{esc(e.text)}</b>' if e.kind == "error" else esc(e.text) for e in row["results"]
    )
    late = " late" if row["kind"] != "sql" else ""
    return label + code + (f'<span class="res{late}">{results}</span>' if results else "")


def fix(pair, schedule, a, b):
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
        lead = (
            f"<p><b>{esc(a)}</b> read with <code>{esc(short(reads[-1]))}</code> and then wrote "
            f"based on what it read. <b>{esc(b)}</b> changed that data in between, so the write "
            "used a stale value.</p>"
        )
    else:
        lead = f"<p>The writes of <b>{esc(a)}</b> and <b>{esc(b)}</b> interleaved.</p>"
    return lead + (
        "<ol class=\"fixes\">"
        "<li>Lock what you read: add <code>FOR UPDATE</code> to that <code>SELECT</code>.</li>"
        "<li>Or check and write in one statement, like <code>UPDATE ... WHERE stock > 0 RETURNING ...</code>, "
        "and treat zero rows as a conflict.</li>"
        "<li>Or run the transaction at <code>SERIALIZABLE</code> and retry on error 40001.</li>"
        "</ol><p class=\"after\">Run deadheat again after the fix. It passes when every order is serializable.</p>"
    )


def status(code):
    return "no response" if code == 0 else esc(code)


def short(sql, limit=110):
    flat = " ".join(sql.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def esc(value):
    return html.escape(str(value))


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>deadheat report</title>
<style>
:root {
  --bg: #f7f6f3; --card: #ffffff; --ink: #1d1c1a; --muted: #6e6a64; --line: #e4e1dc;
  --red: #a8322d; --red-soft: #f6e7e5; --code: #f1efeb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111110; --card: #191917; --ink: #ebe9e5; --muted: #9b968f; --line: #2b2a27;
    --red: #e0675f; --red-soft: #2c1a18; --code: #22211e;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 980px; margin: 0 auto; padding: 56px 24px 40px; }
code, .t, .rows td, .res { font-family: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, monospace; font-size: 12.5px; }
code { background: var(--code); padding: 1px 6px; border-radius: 4px; }
header { margin-bottom: 40px; }
.mark { color: var(--muted); font-size: 13px; letter-spacing: .06em; text-transform: uppercase; margin: 0 0 10px; }
h1 { font-size: 40px; line-height: 1.1; margin: 0 0 12px; font-weight: 650; letter-spacing: -.02em; }
h1.race { color: var(--red); }
.meta { color: var(--muted); margin: 0; font-size: 13.5px; }
h2 { font-size: 20px; margin: 0 0 8px; font-weight: 620; display: flex; align-items: center; gap: 10px; }
h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 28px 0 10px; font-weight: 600; }
h4 { font-size: 12px; margin: 0 0 6px; font-weight: 600; color: var(--muted); }
.tag { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--red); border: 1px solid var(--red); border-radius: 999px; padding: 1px 8px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 28px; margin-bottom: 24px; }
.lede { margin: 6px 0 10px; }
.reasons { margin: 0; padding-left: 18px; color: var(--red); }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-weight: 600; font-size: 13px; }
.grid td { font-family: ui-monospace, Consolas, monospace; }
.grid tr.bad th, .grid tr.bad td { color: var(--red); background: var(--red-soft); }
.table-name { font-family: ui-monospace, Consolas, monospace; font-size: 13px; margin: 0 0 8px; }
.states { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.state { border: 1px solid var(--line); border-radius: 8px; padding: 10px; overflow-x: auto; }
.state.bad { border-color: var(--red); background: var(--red-soft); }
.state.bad h4 { color: var(--red); }
.rows th, .rows td { padding: 4px 6px; font-size: 12.5px; }
.empty, .more { color: var(--muted); font-size: 12px; }
.lanes { table-layout: fixed; }
.lanes th:first-child, .lanes td.t { width: 78px; }
.lanes td { padding: 6px 10px; }
.lanes td.t { color: var(--muted); font-size: 11.5px; white-space: nowrap; }
.lanes code { display: inline-block; max-width: 100%; overflow-wrap: anywhere; background: none; padding: 0; }
.lanes tr.held td { background: var(--red-soft); }
.pause { display: block; color: var(--red); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 2px; }
.res { display: block; color: var(--muted); font-size: 11.5px; }
.res.late::before { content: "returns after waiting: "; }
.res .err { color: var(--red); font-weight: 500; }
details { margin-top: 28px; }
summary { cursor: pointer; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); font-weight: 600; margin-bottom: 10px; }
.fixes { padding-left: 20px; margin: 8px 0; }
.fixes li { margin: 4px 0; }
.after { color: var(--muted); font-size: 13.5px; margin: 8px 0 0; }
.warn { border-left: 3px solid var(--red); padding: 4px 0 4px 16px; margin-bottom: 24px; }
.calm ul { list-style: none; padding: 0; margin: 0; }
.calm li { padding: 8px 0; border-bottom: 1px solid var(--line); display: flex; gap: 8px; align-items: baseline; }
.calm em { margin-left: auto; color: var(--muted); font-style: normal; font-size: 13px; }
footer { color: var(--muted); font-size: 12px; margin-top: 32px; }
@media (max-width: 720px) {
  main { padding: 32px 16px; }
  h1 { font-size: 30px; }
  .card { padding: 18px; }
  .states { grid-template-columns: 1fr; }
  .lanes th:first-child, .lanes td.t { width: 56px; }
}
</style>
</head>
<body><main>
{body}
</main></body>
</html>
"""
