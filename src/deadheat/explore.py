"""Runs requests in forced orders and checks each outcome against serial ones.

For a pair of requests A and B, deadheat first runs A then B, and B then A,
one at a time. Then for every statement A sends, it holds A right before
that statement, lets B run, and releases A. If the responses or the final
data match neither serial run, no serial order could have produced them, so
the app has a race.
"""

import asyncio
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

# Values the database fills in differ between runs and between orders.
GENERATED = re.compile(r"nextval\(|now\(\)|current_timestamp|random|uuid", re.I)

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
    schedules: list = field(default_factory=list)

    @property
    def races(self):
        return [s for s in self.schedules if s.reasons]


@dataclass
class Result:
    pairs: list
    ignored: set
    runs: int
    seconds: float
    warnings: list


class Recorder(Hooks):
    """Records each request's statements and can hold request A at one of them."""

    def __init__(self):
        self.conns = {}
        self.begin()

    def begin(self, hold=None):
        self.t0 = self.active = time.monotonic()
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
        # shortcut: after A is released, a connection B never used counts as A's.
        # Only the report timeline depends on it, never the verdict.
        who = self.who or self.owner.get(conn.id, "A")
        self.owner[conn.id] = who
        if who == "A" and self.hold and not self.reached.is_set():
            self.seen.update(sqls)
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
        self.who = None
        self.gate.set()

    async def quiet(self, gap=0.05, limit=3.0):
        """Wait until the app has sent nothing for `gap` seconds."""
        end = time.monotonic() + limit
        while time.monotonic() < end and time.monotonic() - self.active < gap:
            await asyncio.sleep(gap / 4)

    def _log(self, conn, kind, text, who=None):
        self.active = time.monotonic()
        who = who or self.owner.get(conn.id, "?")
        self.events.append(Event(self.active - self.t0, who, conn.id, kind, text))


class Explorer:
    def __init__(self, scenario, db, recorder, progress=None):
        self.s = scenario
        self.db = db
        self.rec = recorder
        self.progress = progress or (lambda pair: None)
        self.runs = 0
        self.warnings = []
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    async def run(self):
        start = time.monotonic()
        self.tables, self.ignored = await self.introspect()
        self.ignored |= self.s.ignore
        traces = {}
        for r in self.s.requests:
            first = await self.serial([("A", r)])
            again = await self.serial([("A", r)])
            self.ignored |= unstable(first.snapshot, again.snapshot)
            if first.statuses != again.statuses:
                self.warnings.append(f"{r.name} answered {first.statuses['A']} then {again.statuses['A']} from the same state")
            for q in first.failed:
                self.warnings.append(f"invariant fails after {r.name} alone: {q}")
            traces[r.name] = [e.text for e in first.events if e.kind == "sql"]
            if not traces[r.name]:
                self.warnings.append(f"{r.name} sent no SQL, so nothing can race with it")
        if not any(traces.values()):
            # an app talking to the real database would otherwise pass with no races
            host, port = self.s.proxy
            raise RunError(f"no request sent SQL through the proxy. Point the app's database at {host}:{port}")
        pairs = []
        for a in self.s.requests:
            for b in self.s.requests:
                pairs.append(await self.pair(a, b, traces[a.name]))
                self.progress(pairs[-1])
        return Result(pairs, self.ignored, self.runs, time.monotonic() - start, self.warnings)

    async def pair(self, a, b, trace):
        ab = await self.serial([("A", a), ("B", b)])
        if a is b:
            ba = Outcome({"A": ab.statuses["B"], "B": ab.statuses["A"]}, ab.snapshot, [], ab.failed)
        else:
            ba = await self.serial([("B", b), ("A", a)])
        pair = Pair(a, b, trace, [ab, ba])
        for hold in holds(trace):
            outcome = await self.forced(a, b, hold)
            if outcome:
                pair.schedules.append(Schedule(hold, outcome, judge(outcome, pair.serials, self.ignored)))
        return pair

    async def forced(self, a, b, hold):
        await self.reset()
        rec = self.rec
        rec.begin(hold)
        task_a = asyncio.create_task(self.send(a))
        reached = asyncio.create_task(rec.reached.wait())
        await asyncio.wait({task_a, reached}, return_when=asyncio.FIRST_COMPLETED)
        if not rec.reached.is_set():
            # A took another path this time and never sent that statement
            reached.cancel()
            await task_a
            await rec.quiet()
            return None
        rec.who = "B"
        task_b = asyncio.create_task(self.send(b))
        how = await self.watch(task_b, rec.held.pid)
        rec.release()
        statuses = dict(zip("AB", await asyncio.gather(task_a, task_b)))
        await rec.quiet()
        return Outcome(statuses, await self.snapshot(), rec.events, await self.check(), how)

    async def watch(self, task, held_pid):
        """Let B run until it finishes, waits on a lock A holds, or goes quiet."""
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.02)
            if done:
                return "finished"
            pids = [c.pid for c in self.rec.conns.values() if c.pid and self.rec.owner.get(c.id) == "B"]
            if pids and await self.fetch(BLOCKED, (pids, held_pid)):
                return "blocked"
            if time.monotonic() - self.rec.active > self.s.stall:
                return "stalled"

    async def serial(self, order):
        await self.reset()
        self.rec.begin()
        statuses = {}
        for role, req in order:
            self.rec.who = role
            statuses[role] = await self.send(req)
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
        return tables, ignored

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
        except OSError:
            return 0


def judge(outcome, serials, ignored):
    """Why this outcome is impossible in any serial order. Empty if it is possible."""
    state = normalize(outcome.snapshot, ignored)
    states = [normalize(s.snapshot, ignored) for s in serials]
    reasons = []
    if not any(outcome.statuses == s.statuses and state == st for s, st in zip(serials, states)):
        if not any(outcome.statuses == s.statuses for s in serials):
            reasons.append("responses match no serial order")
        if not any(state == st for st in states):
            reasons.append("final data matches no serial order")
        if not reasons:
            reasons.append("responses and data each match a different serial order")
    reasons += [f"invariant failed: {q}" for q in outcome.failed]
    if reasons:
        codes = {e.text[:5] for e in outcome.events if e.kind == "error"}
        if "40P01" in codes:
            reasons.append("a deadlock aborted one request")
        if "40001" in codes:
            reasons.append("a serialization failure was not retried")
    return reasons


def holds(trace):
    """Every statement A sends after its first is a point to hold A at."""
    seen = Counter()
    points = []
    for i, sql in enumerate(trace):
        seen[sql] += 1
        if i:
            points.append((sql, seen[sql]))
    return points


def normalize(snapshot, ignored):
    """Each table as a multiset of rows, without the ignored columns."""
    out = {}
    for table, (cols, rows) in snapshot.items():
        keep = [i for i, c in enumerate(cols) if (table, c) not in ignored]
        out[table] = Counter(tuple(str(row[i]) for i in keep) for row in rows)
    return out


def unstable(first, again):
    """Columns whose values differ between two identical runs."""
    out = set()
    for table, (cols, rows) in first.items():
        other = again[table][1]
        for i, col in enumerate(cols):
            if Counter(str(r[i]) for r in rows) != Counter(str(r[i]) for r in other):
                out.add((table, col))
    return out


def extra_rows(snapshot, other, ignored):
    """Rows present in snapshot but not in other, per table."""
    a, b = normalize(snapshot, ignored), normalize(other, ignored)
    return {t: list((a[t] - b[t]).elements()) for t in a if a[t] - b[t]}
