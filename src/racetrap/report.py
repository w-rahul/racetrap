"""One self contained HTML report: for each race, the order, the outcome and the fix."""

import html
import re
from datetime import datetime

from . import __version__
from .explore import normalize, shape

LOCKING_READ = re.compile(r"\bFOR\s+(NO\s+KEY\s+UPDATE|UPDATE|SHARE|KEY\s+SHARE)\b", re.I)
VERB = re.compile(r"\s*([A-Za-z]+)")
# what connection pools send when a connection goes back, shown as one quiet step
POOL_RESET = re.compile(r"pg_advisory_unlock_all|DISCARD ALL|RESET ALL", re.I)
COUNTED = re.compile(r"^(?:SELECT|UPDATE|DELETE|INSERT \d+|MERGE|COPY|FETCH|MOVE) (\d+)$")
ROWS = re.compile(r"^\d+ rows?$")


def render(scenario, result):
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
            f"<em>{len(p.schedules)} orders, all serializable</em></div>"
            for p in calm
        )
        parts.append(f'<section class="passed"><h2>Passed</h2><div class="pass-grid">{items}</div></section>')
    parts.append(
        '<footer>racetrap checks the transactions and locks you already wrote. '
        "It does not replace them, and it never changes your code.</footer>"
    )
    return PAGE.replace("{body}", "\n".join(parts))


def names(pair):
    a, b = pair.a.name, pair.b.name
    return a, (f"{b} again" if pair.a is pair.b else b)


def race_card(pair, fks, mirrored):
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
        f"<details><summary>Every statement, in order</summary>{timeline(race.outcome.events, sql, a, b)}</details>",
        "</section>",
    ])


def lanes(events, held, a, b):
    """A and B as two tracks, each statement a chip in the order Postgres saw it."""
    steps, by_conn, gate, released = [], {}, None, None
    for e in events:
        if e.who == "bg":
            continue
        if e.kind in ("ok", "error"):
            if e.conn in by_conn:
                by_conn[e.conn]["results"].append(e)
            continue
        step = {"who": e.who, "kind": e.kind, "text": e.text, "results": []}
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


def rows_label(results):
    """'1 row' from a CommandComplete tag. BEGIN and COMMIT say nothing new."""
    tag = results[0].text if results else ""
    counted = COUNTED.match(tag)
    if not counted:
        # the proxy already words a suspended portal as "1 row"
        return tag if ROWS.match(tag) else ""
    n = int(counted[1])
    return f"{n} row" + ("" if n == 1 else "s")


def outcome(pair, race, fks):
    ab, ba = pair.serials
    forced = normalize(race.outcome.snapshot, pair.ignored, fks)
    serial = [normalize(s.snapshot, pair.ignored, fks) for s in pair.serials]
    rows = [
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


def data_diff(pair, schedule, fks, a, b):
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


def cell_html(row):
    if row["kind"] == "hold":
        return f'<span class="flag">held before</span><code>{esc(short(row["text"]))}</code>'
    label = '<span class="flag">released</span>' if row["released"] else ""
    code = "" if row["kind"] != "sql" else f'<code title="{esc(row["text"])}">{esc(short(row["text"]))}</code>'
    results = ", ".join(
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


def status(code):
    return "none" if code == 0 else esc(code)


def short(sql, limit=110):
    flat = " ".join(sql.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def esc(value):
    return html.escape(str(value))


ICON = (
    '<svg viewBox="0 0 512 512" aria-hidden="true"><rect width="512" height="512" rx="112" fill="#17181B"/>'
    '<path d="M138 214 C 128 160 140 112 162 96 C 186 104 214 132 232 160 Z" fill="#9A968F"/>'
    '<path d="M374 214 C 384 160 372 112 350 96 C 326 104 298 132 280 160 Z" fill="#9A968F"/>'
    '<path d="M158 190 C 154 156 160 128 170 118 C 184 128 198 144 208 160 Z" fill="#2A2B2F"/>'
    '<path d="M354 190 C 358 156 352 128 342 118 C 328 128 314 144 304 160 Z" fill="#2A2B2F"/>'
    '<path d="M380 258 C 408 246 440 230 476 222 C 466 240 456 254 450 268 C 428 268 404 270 380 274 Z" fill="#E0483E"/>'
    '<path d="M380 276 C 408 280 436 294 466 320 C 448 320 430 316 416 310 C 404 300 392 290 380 284 Z" fill="#B93A31"/>'
    '<path d="M256 134 C 330 134 380 170 394 228 C 400 256 412 282 432 304 L 400 314 L 424 340 L 384 344 C 360 392 312 416 256 416 '
    'C 200 416 152 392 128 344 L 88 340 L 112 314 L 80 304 C 100 282 112 256 118 228 C 132 170 182 134 256 134 Z" fill="#D9D6D0"/>'
    '<path d="M256 140 C 268 170 270 200 262 236 L 250 236 C 242 200 244 170 256 140 Z" fill="#5A5853"/>'
    '<path d="M112 270 C 132 236 168 222 204 228 C 226 232 242 240 256 250 C 270 240 286 232 308 228 C 344 222 380 236 400 270 '
    'C 384 300 352 312 318 306 C 292 302 272 290 256 280 C 240 290 220 302 194 306 C 160 312 128 300 112 270 Z" fill="#E0483E"/>'
    '<circle cx="200" cy="266" r="21" fill="#F3F1EC"/><circle cx="312" cy="266" r="21" fill="#F3F1EC"/>'
    '<circle cx="209" cy="268" r="10" fill="#17181B"/><circle cx="321" cy="268" r="10" fill="#17181B"/>'
    '<ellipse cx="256" cy="352" rx="74" ry="50" fill="#F3F1EC"/>'
    '<path d="M232 318 C 232 306 280 306 280 318 C 280 330 264 342 256 342 C 248 342 232 330 232 318 Z" fill="#17181B"/>'
    '<path d="M256 342 L 256 356 C 262 366 276 366 286 358" fill="none" stroke="#17181B" stroke-width="7" stroke-linecap="round"/>'
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
