"""Runs requests in forced orders and checks each outcome against serial ones.

For a pair of requests A and B, racetrap first runs A then B, and B then A,
one at a time. Then for every statement A sends, it holds A right before
that statement, lets B run, and releases A. If the responses or the final
data match neither serial run, no serial order could have produced them, so
the app has a race.
"""

import asyncio
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

from psycopg import sql as pgsql

from .proxy import Hooks

COLUMNS = """
SELECT c.table_name, c.column_name, coalesce(c.column_default, ''), c.is_identity
FROM information_schema.columns c
JOIN information_schema.tables t USING (table_schema, table_name)
WHERE c.table_schema = %s AND t.table_type = 'BASE TABLE'
ORDER BY c.table_name, c.ordinal_position
"""

FOREIGN_KEYS = """
SELECT t.relname, a.attname, rt.relname, ra.attname
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_class rt ON rt.oid = c.confrelid
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
JOIN pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = c.confkey[1]
WHERE c.contype = 'f' AND cardinality(c.conkey) = 1 AND c.connamespace = to_regnamespace(%s)
"""

# Values the database fills in differ between runs and between orders.
GENERATED = re.compile(r"nextval\(|now\(\)|current_(time|date)|localtime|_timestamp\(|random|uuid", re.I)

# Drivers that inline parameters send different text each run. Holds match on
# the statement with its literals blanked.
LITERAL = re.compile(r"'(?:[^']|'')*'|(?<!\$)\b\d+(?:\.\d+)?\b")

BLOCKED = """
SELECT EXISTS (
    SELECT FROM unnest(%s::int[]) AS b(pid) WHERE %s = ANY (pg_blocking_pids(b.pid))
)
"""


class RunError(Exception):
    pass


@dataclass
class Event:
    t: float
    who: str
    conn: int
    kind: str
    text: str


@dataclass
class Outcome:
    statuses: dict
    snapshot: dict
    events: list
    failed: list
    how: str = "serial"


@dataclass
class Schedule:
    hold: tuple
    outcome: Outcome
    reasons: list


@dataclass
class Pair:
    a: object
    b: object
    trace: list
    serials: list
    ignored: set
    schedules: list = field(default_factory=list)

    @property
    def races(self):
        return [s for s in self.schedules if s.reasons]


@dataclass
class Result:
    pairs: list
    ignored: set
    fks: dict
    runs: int
    seconds: float
    warnings: list


class Recorder(Hooks):
    """Records each request's statements and can hold request A at one of them."""

    def __init__(self):
        self.conns = {}
        self.background = set()
        self.begin()

    def begin(self, hold=None):
        self.t0 = self.active = time.monotonic()
        self.last = {}
        self.events = []
        self.who = "A"
        self.owner = {}
        self.hold = hold
        self.seen = Counter()
        self.held = None
        self.reached = asyncio.Event()
        self.gate = asyncio.Event()

    def opened(self, conn):
        self.conns[conn.id] = conn

    def closed(self, conn):
        self.conns.pop(conn.id, None)

    async def before(self, conn, sqls):
        if all(shape(s) in self.background for s in sqls):
            # the app sends this on its own, like a poller, so no request owns it
            self.owner[conn.id] = "bg"
            for sql in sqls:
                self._log(conn, "sql", sql)
            return
        # shortcut: after A is released, a connection B never used counts as A's.
        # Only the report timeline depends on it, never the verdict.
        owner = self.owner.get(conn.id)
        who = self.who or (owner if owner in ("A", "B") else "A")
        self.owner[conn.id] = who
        if who == "A" and self.hold and not self.reached.is_set():
            self.seen.update(shape(s) for s in sqls)
            sql, n = self.hold
            if self.seen[sql] >= n:
                self.held = conn
                self._log(conn, "hold", sql, who)
                self.reached.set()
                await self.gate.wait()
        for sql in sqls:
            self._log(conn, "sql", sql, who)

    def done(self, conn, tag=None, error=None):
        if error:
            self._log(conn, "error", f"{error.get('C', '')} {error.get('M', '')}".strip())
        else:
            self._log(conn, "ok", tag or "")

    def release(self):
        """Let A go, and disarm the hold so nothing that arrives later is parked."""
        self.who = None
        self.hold = None
        self.gate.set()

    async def quiet(self, gap=0.05, limit=3.0):
        """Wait until the app has sent nothing for `gap` seconds."""
        end = time.monotonic() + limit
        while time.monotonic() < end and time.monotonic() - self.active < gap:
            await asyncio.sleep(gap / 4)

    def _log(self, conn, kind, text, who=None):
        self.active = time.monotonic()
        who = who or self.owner.get(conn.id, "?")
        self.last[who] = self.active
        self.events.append(Event(self.active - self.t0, who, conn.id, kind, text))


class Explorer:
    def __init__(self, scenario, db, recorder, progress=None, alive=None):
        self.s = scenario
        self.db = db
        self.rec = recorder
        self.progress = progress or (lambda pair: None)
        self.alive = alive or (lambda: True)
        self.runs = 0
        self.warnings = []
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    async def run(self):
        start = time.monotonic()
        self.tables, self.ignored, self.fks = await self.introspect()
        self.ignored |= self.s.ignore
        self.fks.update(self.s.refs)
        await self.learn_background()
        traces = {}
        for r in self.s.requests:
            first = await self.serial([("A", r)])
            again = await self.serial([("A", r)])
            self.ignored |= unstable(first.snapshot, again.snapshot)
            if first.statuses != again.statuses:
                self.warn(f"{r.name} answered {first.statuses['A']} then {again.statuses['A']} from the same state")
            for q in first.failed:
                self.warn(f"invariant fails after {r.name} alone: {q}")
            traces[r.name] = [shape(e.text) for e in first.events if e.kind == "sql" and e.who != "bg"]
            if not traces[r.name]:
                self.warn(f"{r.name} sent no SQL, so nothing can race with it")
        if not any(traces.values()):
            # an app talking to the real database would otherwise pass with no races
            host, port = self.s.proxy
            raise RunError(f"no request sent SQL through the proxy. Point the app's database at {host}:{port}")
        pairs = []
        for a in self.s.requests:
            for b in self.s.requests:
                pairs.append(await self.pair(a, b, traces[a.name]))
                self.progress(pairs[-1])
        ignored = set().union(*(p.ignored for p in pairs))
        return Result(pairs, ignored, self.fks, self.runs, time.monotonic() - start, self.warnings)

    async def learn_background(self, seconds=1.0):
        """Watch the app with no request in flight. What it sends then is its own."""
        self.rec.begin()
        self.rec.who = "bg"
        await asyncio.sleep(seconds)
        self.rec.background = {shape(e.text) for e in self.rec.events if e.kind == "sql"}
        if self.rec.background:
            listed = " | ".join(" ".join(s.split()) for s in sorted(self.rec.background))[:200]
            self.warn(f"the app sends SQL on its own, which is left out of every request: {listed}")

    async def pair(self, a, b, trace):
        ab = await self.serial([("A", a), ("B", b)])
        again = await self.serial([("A", a), ("B", b)])
        # rows only a pair writes, like a waitlist entry, can carry their own timestamps
        ignored = self.ignored | unstable(ab.snapshot, again.snapshot)
        if ab.statuses != again.statuses:
            self.warn(f"{a.name} then {b.name} answered differently on two identical runs")
        if a is b:
            ba = Outcome({"A": ab.statuses["B"], "B": ab.statuses["A"]}, ab.snapshot, [], ab.failed)
        else:
            ba = await self.serial([("B", b), ("A", a)])
        for q in set(ab.failed) | set(ba.failed):
            self.warn(f"invariant fails even when {a.name} and {b.name} run one at a time: {q}")
        pair = Pair(a, b, trace, [ab, ba], ignored)
        points = holds(trace)
        for hold in points:
            outcome = await self.forced(a, b, hold)
            if outcome:
                pair.schedules.append(Schedule(hold, outcome, judge(outcome, pair.serials, ignored, self.fks)))
        if len(pair.schedules) < len(points):
            missed = len(points) - len(pair.schedules)
            self.warn(f"{a.name} vs {b.name}: {missed} of {len(points)} statements were sent after the response or changed between runs, so they were not tested")
        return pair

    async def forced(self, a, b, hold):
        await self.reset()
        rec = self.rec
        rec.begin(hold)
        task_a = asyncio.create_task(self.send(a))
        reached = asyncio.create_task(rec.reached.wait())
        try:
            await asyncio.wait({task_a, reached}, return_when=asyncio.FIRST_COMPLETED)
            if not rec.reached.is_set():
                # A answered without sending that statement, so this order does not exist
                rec.release()
                await task_a
                await rec.quiet()
                return None
            rec.who = "B"
            started = time.monotonic()
            task_b = asyncio.create_task(self.send(b))
            how = await self.watch(task_b, rec.held.pid, started)
            rec.release()
            statuses = dict(zip("AB", await asyncio.gather(task_a, task_b)))
        finally:
            rec.release()
            reached.cancel()
        await rec.quiet()
        return Outcome(statuses, await self.snapshot(), rec.events, await self.check(), how)

    async def watch(self, task, held_pid, started):
        """Let B run until it finishes, waits on a lock A holds, or goes quiet."""
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.02)
            if done:
                return "finished"
            pids = [c.pid for c in self.rec.conns.values() if c.pid and self.rec.owner.get(c.id) == "B"]
            if pids and await self.fetch(BLOCKED, (pids, held_pid)):
                return "blocked"
            # B's own silence counts, so SQL the app sends by itself cannot keep A held
            if time.monotonic() - max(started, self.rec.last.get("B", started)) > self.s.stall:
                return "stalled"

    async def serial(self, order):
        if not self.alive():
            raise RunError("the app exited during the run")
        await self.reset()
        self.rec.begin()
        statuses = {}
        for role, req in order:
            self.rec.who = role
            statuses[role] = await self.send(req)
            if not statuses[role]:
                raise RunError(f"{req.name} got no response from {self.s.app}. Did the app stop?")
            await self.rec.quiet()
        return Outcome(statuses, await self.snapshot(), self.rec.events, await self.check())

    async def reset(self):
        self.runs += 1
        for q in self.s.setup:
            await self.db.execute(q)

    async def snapshot(self):
        out = {}
        for table in self.tables:
            query = pgsql.SQL("SELECT * FROM {}").format(pgsql.Identifier(self.s.schema, table))
            cur = await self.db.execute(query)
            out[table] = ([d.name for d in cur.description], await cur.fetchall())
        return out

    async def check(self):
        return [q for q in self.s.invariants if not await self.fetch(q)]

    async def fetch(self, query, params=None):
        row = await (await self.db.execute(query, params)).fetchone()
        return row[0] if row else None

    async def introspect(self):
        tables, ignored = [], set()
        rows = await (await self.db.execute(COLUMNS, (self.s.schema,))).fetchall()
        for table, column, default, identity in rows:
            if table not in tables:
                tables.append(table)
            if identity == "YES" or GENERATED.search(default):
                ignored.add((table, column))
        keys = await (await self.db.execute(FOREIGN_KEYS, (self.s.schema,))).fetchall()
        return tables, ignored, {(t, c): (rt, rc) for t, c, rt, rc in keys}

    async def send(self, req):
        return await asyncio.to_thread(self._http, req)

    def _http(self, req):
        data = None if req.json is None else json.dumps(req.json).encode()
        headers = {"content-type": "application/json", **req.headers} if data else req.headers
        r = urllib.request.Request(self.s.app + req.path, data=data, method=req.method, headers=headers)
        try:
            with self.http.open(r, timeout=30) as resp:
                resp.read()
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except (OSError, http.client.HTTPException):
            return 0


def judge(outcome, serials, ignored, fks=None):
    """Why this outcome is impossible in any serial order. Empty if it is possible."""
    state = normalize(outcome.snapshot, ignored, fks)
    states = [normalize(s.snapshot, ignored, fks) for s in serials]
    reasons = []
    if not any(outcome.statuses == s.statuses and state == st for s, st in zip(serials, states)):
        if not any(outcome.statuses == s.statuses for s in serials):
            reasons.append("responses match no serial order")
        if not any(state == st for st in states):
            reasons.append("final data matches no serial order")
        if not reasons:
            reasons.append("responses and data each match a different serial order")
    # an invariant that fails in every serial order is broken by the scenario, not by a race
    reasons += [f"invariant failed: {q}" for q in outcome.failed if any(q not in s.failed for s in serials)]
    if reasons:
        codes = {e.text[:5] for e in outcome.events if e.kind == "error"}
        if "40P01" in codes:
            reasons.append("a deadlock aborted one request")
        if "40001" in codes:
            reasons.append("a serialization failure was not retried")
    return reasons


def shape(sql):
    """The statement with its literals blanked."""
    return LITERAL.sub("?", sql)


def holds(trace):
    """Every statement A sends after its first is a point to hold A at."""
    seen = Counter()
    points = []
    for i, sql in enumerate(trace):
        seen[sql] += 1
        if i:
            points.append((sql, seen[sql]))
    return points


def normalize(snapshot, ignored, fks=None):
    """Each table as a multiset of rows, without the ignored columns.

    A reference to a generated id becomes the row it points to, so two runs
    that hand out ids in a different order still compare equal.
    """
    refs = {
        col: target
        for col, target in (fks or {}).items()
        if target in ignored and target[0] in snapshot and target[1] in snapshot[target[0]][0]
    }
    index = {}

    def pointed(target, value):
        if target not in index:
            cols, rows = snapshot[target[0]]
            i = cols.index(target[1])
            index[target] = {r[i]: r for r in rows}
        return index[target].get(value)

    def label(table, row, depth=0):
        out = []
        for col, value in zip(snapshot[table][0], row):
            if (table, col) in ignored:
                continue
            target = refs.get((table, col))
            if target and value is not None and depth < 3:
                ref = pointed(target, value)
                value = "→ " + ", ".join(label(target[0], ref, depth + 1)) if ref else f"→ missing {value}"
            out.append(str(value))
        return tuple(out)

    return {table: Counter(label(table, row) for row in rows) for table, (_, rows) in snapshot.items()}


def unstable(first, again):
    """Columns whose values differ between two identical runs."""
    out = set()
    for table, (cols, rows) in first.items():
        other = again[table][1]
        for i, col in enumerate(cols):
            if Counter(str(r[i]) for r in rows) != Counter(str(r[i]) for r in other):
                out.add((table, col))
    return out
