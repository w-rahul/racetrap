"""Runs requests in forced orders and checks each outcome against serial ones.

For a pair of requests A and B, racetrap first runs A then B, and B then A,
one at a time. Then for every statement A sends, it holds A right before
that statement, lets B run, and releases A. If the responses or the final
data match neither serial run, no serial order could have produced them, so
the app has a race.

Two shortcuts keep that fast without changing the answer. A hold is skipped
when it would only repeat a serial order, and a pair is skipped when the two
requests share no table that either one writes.
"""

import asyncio
import http.client
import itertools
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql as pgsql

from .proxy import Conn, Hooks
from .scenario import REF, Column, Request, Scenario

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

SNAPSHOT = "racetrap_snapshot"

# Tables to save, with the columns that can be written back. Partitions come
# back through their parent, and generated columns fill themselves in.
RESTORABLE = """
SELECT c.relname, array_agg(a.attname::text ORDER BY a.attnum)
FROM pg_class c
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = ''
WHERE c.relnamespace = to_regnamespace(%s) AND c.relkind IN ('r', 'p') AND NOT c.relispartition
GROUP BY c.relname
ORDER BY c.relname
"""

SEQUENCES = "SELECT c.relname FROM pg_class c WHERE c.relkind = 'S' AND c.relnamespace = to_regnamespace(%s)"

# Names that tell what a statement can touch. A view, a function of your own,
# or a table with triggers or rules can reach tables the SQL never names.
NAMES = """
WITH own AS (SELECT oid FROM pg_namespace WHERE nspname !~ '^(pg_|information_schema$|racetrap_snapshot$)')
SELECT 'table', c.relname FROM pg_class c WHERE c.relkind IN ('r', 'p') AND c.relnamespace IN (SELECT oid FROM own)
UNION ALL
SELECT 'opaque', c.relname FROM pg_class c WHERE c.relkind IN ('v', 'm', 'f') AND c.relnamespace IN (SELECT oid FROM own)
UNION ALL
SELECT 'opaque', p.proname FROM pg_proc p
WHERE p.pronamespace IN (SELECT oid FROM own)
  AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
UNION ALL
SELECT 'opaque', c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal
UNION ALL
SELECT 'opaque', r.tablename FROM pg_rules r WHERE r.schemaname !~ '^pg_'
"""

# Every foreign key as (child table, parent table).
LINKS = """
SELECT t.relname, rt.relname FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_class rt ON rt.oid = c.confrelid
WHERE c.contype = 'f'
"""

# A statement that reads nothing, writes nothing and ends no transaction.
CONTROL = re.compile(r"^\s*(?:BEGIN|START\s+TRANSACTION|SET|SHOW|RESET)\b[^;]*;?\s*$", re.I)
# What connection pools send when a connection goes back.
POOL_RESET = re.compile(r"pg_advisory_unlock_all|DISCARD ALL|RESET ALL", re.I)
# Words that mean a statement touches some relation, named or not.
DATA = re.compile(r"\b(?:FROM|INTO|UPDATE|DELETE|CALL|DO|COPY|EXECUTE|TRUNCATE|MERGE|LOCK|TABLE|JOIN|USING)\b", re.I)
WRITE = re.compile(r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE|COPY)\b", re.I)
# Writes that foreign keys can carry on into child tables.
CASCADES = re.compile(r"\b(?:UPDATE|DELETE|MERGE|TRUNCATE)\b", re.I)

# Response fields that carry values made up on the spot: ids, tokens and timestamps.
GENERATED_KEY = re.compile(r"(?:^|_)(?:id|uuid|token|at|timestamp)$|(?:Id|At|Token)$")
MISSING = object()

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


# A table as its column names and its rows, and every table by name.
Table = tuple[list[str], list[tuple[Any, ...]]]
Snapshot = dict[str, Table]
# Where a value sits in a JSON body: keys and list positions.
BodyPath = tuple[str | int, ...]
# A statement shape, and which time the request sends it.
Hold = tuple[str, int]
# The tables a request touched and the ones it wrote. None when its SQL cannot tell.
Footprint = tuple[set[str], set[str]] | None


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
    statuses: dict[str, int]
    snapshot: Snapshot
    events: list[Event]
    failed: list[str]
    how: str = "serial"
    bodies: dict[str, str | None] = field(default_factory=dict)


@dataclass
class Schedule:
    hold: Hold
    outcome: Outcome
    reasons: list[str]


@dataclass
class Pair:
    a: Request
    b: Request
    trace: list[str]
    serials: list[Outcome]
    ignored: set[Column]
    schedules: list[Schedule] = field(default_factory=list)
    skipped: bool = False
    loose: dict[str, set[BodyPath]] = field(default_factory=dict)

    @property
    def races(self) -> list[Schedule]:
        return [s for s in self.schedules if s.reasons]


@dataclass
class Result:
    pairs: list[Pair]
    ignored: set[Column]
    fks: dict[Column, Column]
    runs: int
    seconds: float
    warnings: list[str]


class Recorder(Hooks):
    """Records each request's statements and can hold request A at one of them."""

    def __init__(self) -> None:
        self.conns: dict[int, Conn] = {}
        self.background: set[str] = set()
        self.begin()

    def begin(self, hold: Hold | None = None) -> None:
        self.t0 = self.active = time.perf_counter()
        self.last: dict[str, float] = {}
        self.events: list[Event] = []
        self.who: str | None = "A"
        self.owner: dict[int, str] = {}
        self.hold = hold
        self.seen: Counter[str] = Counter()
        self.held: Conn | None = None
        self.reached = asyncio.Event()
        self.gate = asyncio.Event()

    def opened(self, conn: Conn) -> None:
        self.conns[conn.id] = conn

    def closed(self, conn: Conn) -> None:
        self.conns.pop(conn.id, None)

    async def before(self, conn: Conn, sqls: list[str]) -> None:
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

    def done(self, conn: Conn, tag: str | None = None, error: dict[str, str] | None = None) -> None:
        if error:
            self._log(conn, "error", f"{error.get('C', '')} {error.get('M', '')}".strip())
        else:
            self._log(conn, "ok", tag or "")

    def release(self) -> None:
        """Let A go, and disarm the hold so nothing that arrives later is parked."""
        self.who = None
        self.hold = None
        self.gate.set()

    async def quiet(self, gap: float = 0.05, limit: float = 3.0) -> None:
        """Wait until the app has sent nothing for `gap` seconds."""
        end = time.perf_counter() + limit
        while time.perf_counter() < end and time.perf_counter() - self.active < gap:
            await asyncio.sleep(gap / 4)

    def _log(self, conn: Conn, kind: str, text: str, who: str | None = None) -> None:
        self.active = time.perf_counter()
        who = who or self.owner.get(conn.id, "?")
        self.last[who] = self.active
        self.events.append(Event(self.active - self.t0, who, conn.id, kind, text))


class Explorer:
    def __init__(
        self,
        scenario: Scenario,
        db: psycopg.AsyncConnection[Any],
        recorder: Recorder,
        progress: Callable[[Pair], None] | None = None,
        alive: Callable[[], bool] | None = None,
        all_pairs: bool = False,
        share: tuple[int, int] = (0, 1),
    ) -> None:
        self.s = scenario
        self.db = db
        self.rec = recorder
        self.progress: Callable[[Pair], None] = progress or (lambda pair: None)
        self.alive: Callable[[], bool] = alive or (lambda: True)
        self.all_pairs = all_pairs
        self.share = share
        self.restore: pgsql.Composed | None = None
        self.loose: dict[str, set[BodyPath]] = {}
        self.runs = 0
        self.warnings: list[str] = []
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    async def run(self) -> Result:
        start = time.perf_counter()
        self.tables, self.ignored, self.fks = await self.introspect()
        self.ignored |= self.s.ignore
        self.fks.update(self.s.refs)
        # logins come first, so the sessions they create are part of the saved data
        await self.login()
        if not self.s.setup:
            self.restore = await self.save()
        try:
            return await self.explore(start)
        finally:
            if self.restore:
                # leave the database the way it was found
                await self.revert()
                await self.db.execute(pgsql.SQL("DROP SCHEMA {} CASCADE").format(pgsql.Identifier(SNAPSHOT)))

    async def login(self) -> None:
        """Run each login once and put what it captured into the requests."""
        values: dict[str, str] = {}
        self.rec.begin()
        self.rec.who = "bg"
        for login in self.s.logins:
            status, body, headers = await self.send(login.request)
            if not 200 <= status < 300:
                raise RunError(f"login {login.name} answered {status or 'nothing'}")
            for var, source in login.capture.items():
                values[f"{login.name}.{var}"] = captured(source, body, headers, login.name)
        for r in self.s.requests:
            r.path, r.headers, r.json = fill(r.path, values), fill(r.headers, values), fill(r.json, values)
        if self.s.logins:
            await self.rec.quiet()

    async def explore(self, start: float) -> Result:
        await self.learn_background()
        names, opaque, parents, children = await self.catalog()
        await self.revert()
        base = await self.snapshot()
        traces: dict[str, list[str]] = {}
        feet: dict[str, Footprint] = {}
        for r in self.s.requests:
            first = await self.serial([("A", r)])
            again = await self.serial([("A", r)])
            self.ignored |= unstable(first.snapshot, again.snapshot)
            self.loose[r.name] = differing(first.bodies["A"], again.bodies["A"])
            if first.statuses != again.statuses:
                self.warn(f"{r.name} answered {first.statuses['A']} then {again.statuses['A']} from the same state")
            for q in first.failed:
                self.warn(f"invariant fails after {r.name} alone: {q}")
            traces[r.name] = [shape(e.text) for e in first.events if e.kind == "sql" and e.who != "bg"]
            if not traces[r.name]:
                self.warn(f"{r.name} sent no SQL, so nothing can race with it")
            changed = {t.lower() for t in self.tables if rows(base[t]) != rows(first.snapshot[t])}
            feet[r.name] = footprint(traces[r.name], names, opaque, parents, children, changed)
        if not any(traces.values()):
            # an app talking to the real database would otherwise pass with no races
            host, port = self.s.proxy
            raise RunError(f"no request sent SQL through the proxy. Point the app's database at {host}:{port}")
        pairs = []
        worker, workers = self.share
        for k, (a, b) in enumerate(itertools.product(self.s.requests, repeat=2)):
            if k % workers != worker:
                continue
            if not self.all_pairs and independent(feet[a.name], feet[b.name]):
                pairs.append(Pair(a, b, traces[a.name], [], set(), skipped=True))
            else:
                pairs.append(await self.pair(a, b, traces[a.name]))
            self.progress(pairs[-1])
        ignored = self.ignored.union(*(p.ignored for p in pairs))
        return Result(pairs, ignored, self.fks, self.runs, time.perf_counter() - start, self.warnings)

    async def learn_background(self, seconds: float = 1.0) -> None:
        """Watch the app with no request in flight. What it sends then is its own."""
        self.rec.begin()
        self.rec.who = "bg"
        await asyncio.sleep(seconds)
        self.rec.background = {shape(e.text) for e in self.rec.events if e.kind == "sql"}
        if self.rec.background:
            listed = " | ".join(" ".join(s.split()) for s in sorted(self.rec.background))[:200]
            self.warn(f"the app sends SQL on its own, which is left out of every request: {listed}")

    async def pair(self, a: Request, b: Request, trace: list[str]) -> Pair:
        ab = await self.serial([("A", a), ("B", b)])
        again = await self.serial([("A", a), ("B", b)])
        # rows only a pair writes, like a waitlist entry, can carry their own timestamps
        ignored = self.ignored | unstable(ab.snapshot, again.snapshot)
        # the same goes for response fields, like a position in a queue stamped with the time
        loose = {role: self.loose[r.name] | differing(ab.bodies[role], again.bodies[role]) for role, r in (("A", a), ("B", b))}
        if ab.statuses != again.statuses:
            self.warn(f"{a.name} then {b.name} answered differently on two identical runs")
        if a is b:
            ba = Outcome({"A": ab.statuses["B"], "B": ab.statuses["A"]}, ab.snapshot, [], ab.failed, bodies={"A": ab.bodies["B"], "B": ab.bodies["A"]})
        else:
            ba = await self.serial([("B", b), ("A", a)])
        for q in set(ab.failed) | set(ba.failed):
            self.warn(f"invariant fails even when {a.name} and {b.name} run one at a time: {q}")
        pair = Pair(a, b, trace, [ab, ba], ignored, loose=loose)
        points = holds(trace)
        for hold in points:
            outcome = await self.forced(a, b, hold)
            if not outcome:
                continue
            reasons = judge(outcome, pair.serials, ignored, self.fks, loose)
            if reasons:
                # a forced order repeats exactly, so a race that does not is noise
                repeat = await self.forced(a, b, hold)
                if not repeat or not judge(repeat, pair.serials, ignored, self.fks, loose):
                    self.warn(f"{a.name} vs {b.name}: an order raced once and passed when repeated, so it is not reported")
                    reasons = []
            pair.schedules.append(Schedule(hold, outcome, reasons))
        if len(pair.schedules) < len(points):
            missed = len(points) - len(pair.schedules)
            self.warn(f"{a.name} vs {b.name}: {missed} of {len(points)} statements were sent after the response or changed between runs, so they were not tested")
        return pair

    async def forced(self, a: Request, b: Request, hold: Hold) -> Outcome | None:
        await self.reset()
        rec = self.rec
        rec.begin(hold)
        task_a = asyncio.create_task(self.ask(a))
        reached = asyncio.create_task(rec.reached.wait())
        try:
            await asyncio.wait({task_a, reached}, return_when=asyncio.FIRST_COMPLETED)
            held = rec.held
            if not rec.reached.is_set() or held is None:
                # A answered without sending that statement, so this order does not exist
                rec.release()
                await task_a
                await rec.quiet()
                return None
            rec.who = "B"
            started = time.perf_counter()
            task_b = asyncio.create_task(self.ask(b))
            how = await self.watch(task_b, held.pid, started)
            rec.release()
            answers = await asyncio.gather(task_a, task_b)
        finally:
            rec.release()
            reached.cancel()
        await rec.quiet()
        statuses = {role: answer[0] for role, answer in zip("AB", answers, strict=True)}
        bodies = {role: answer[1] for role, answer in zip("AB", answers, strict=True)}
        return Outcome(statuses, await self.snapshot(), rec.events, await self.check(), how, bodies)

    async def watch(self, task: asyncio.Task[Any], held_pid: int | None, started: float) -> str:
        """Let B run until it finishes, waits on a lock A holds, or goes quiet."""
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.02)
            if done:
                return "finished"
            pids = [c.pid for c in self.rec.conns.values() if c.pid and self.rec.owner.get(c.id) == "B"]
            if pids and await self.fetch(BLOCKED, (pids, held_pid)):
                return "blocked"
            # B's own silence counts, so SQL the app sends by itself cannot keep A held
            if time.perf_counter() - max(started, self.rec.last.get("B", started)) > self.s.stall:
                return "stalled"

    async def serial(self, order: list[tuple[str, Request]]) -> Outcome:
        if not self.alive():
            raise RunError("the app exited during the run")
        await self.reset()
        self.rec.begin()
        statuses: dict[str, int] = {}
        bodies: dict[str, str | None] = {}
        for role, req in order:
            self.rec.who = role
            statuses[role], bodies[role] = await self.ask(req)
            if not statuses[role]:
                raise RunError(f"{req.name} got no response from {self.s.app}. Did the app stop?")
            await self.rec.quiet()
        return Outcome(statuses, await self.snapshot(), self.rec.events, await self.check(), bodies=bodies)

    async def reset(self) -> None:
        self.runs += 1
        await self.revert()

    async def revert(self) -> None:
        """Put the data back: the saved tables, or the scenario's own setup."""
        if self.restore:
            async with self.db.transaction():
                await self.db.execute(self.restore, prepare=False)
        for q in self.s.setup:
            # never prepared, so a setup string can hold several statements
            await self.db.execute(q, prepare=False)

    async def save(self) -> pgsql.Composed:
        """Copy every table aside once. Returns the SQL that puts the copy back."""
        ident = pgsql.Identifier
        try:
            # without foreign key checks and triggers, tables go back in any order
            await self.db.execute("SET session_replication_role = replica")
            await self.db.execute("RESET session_replication_role")
        except psycopg.errors.InsufficientPrivilege:
            raise RunError("restoring the data needs a superuser on the test database. Add setup SQL to the scenario instead") from None
        if await self.fetch("SELECT to_regnamespace(%s) IS NOT NULL", (SNAPSHOT,)):
            self.warn("an earlier run did not finish, so the test data may not be in its starting state")
        tables = await (await self.db.execute(RESTORABLE, (self.s.schema,))).fetchall()
        sequences = await (await self.db.execute(SEQUENCES, (self.s.schema,))).fetchall()
        await self.db.execute(pgsql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(ident(SNAPSHOT)))
        await self.db.execute(pgsql.SQL("CREATE SCHEMA {}").format(ident(SNAPSHOT)))
        steps: list[pgsql.Composable] = [pgsql.SQL("SET LOCAL session_replication_role = replica")]
        if tables:
            steps.append(pgsql.SQL("TRUNCATE {}").format(pgsql.SQL(", ").join(ident(self.s.schema, t) for t, _ in tables)))
        for table, columns in tables:
            cols = pgsql.SQL(", ").join(map(ident, columns))
            real, copy = ident(self.s.schema, table), ident(SNAPSHOT, table)
            await self.db.execute(pgsql.SQL("CREATE TABLE {} AS SELECT {} FROM {}").format(copy, cols, real))
            steps.append(pgsql.SQL("INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE SELECT {} FROM {}").format(real, cols, cols, copy))
        for (name,) in sequences:
            seq = ident(self.s.schema, name)
            [(last, called)] = await (await self.db.execute(pgsql.SQL("SELECT last_value, is_called FROM {}").format(seq))).fetchall()
            steps.append(pgsql.SQL("SELECT setval({}, {}, {})").format(pgsql.Literal(seq.as_string(self.db)), pgsql.Literal(last), pgsql.Literal(called)))
        return pgsql.SQL("; ").join(steps)

    async def catalog(self) -> tuple[set[str], set[str], dict[str, set[str]], dict[str, set[str]]]:
        """Table names, names that hide what they touch, and foreign keys both ways."""
        found = await (await self.db.execute(NAMES)).fetchall()
        opaque = {name.lower() for kind, name in found if kind == "opaque"}
        names = {name.lower() for kind, name in found if kind == "table"}
        parents: dict[str, set[str]] = {}
        children: dict[str, set[str]] = {}
        for child, parent in await (await self.db.execute(LINKS)).fetchall():
            parents.setdefault(child.lower(), set()).add(parent.lower())
            children.setdefault(parent.lower(), set()).add(child.lower())
        return names, opaque, parents, children

    async def snapshot(self) -> Snapshot:
        out: Snapshot = {}
        for table in self.tables:
            query = pgsql.SQL("SELECT * FROM {}").format(pgsql.Identifier(self.s.schema, table))
            cur = await self.db.execute(query)
            out[table] = ([d.name for d in cur.description or []], await cur.fetchall())
        return out

    async def check(self) -> list[str]:
        return [q for q in self.s.invariants if not await self.fetch(q)]

    async def fetch(self, query: str, params: Sequence[Any] | None = None) -> Any:
        row = await (await self.db.execute(query, params)).fetchone()
        return row[0] if row else None

    async def introspect(self) -> tuple[list[str], set[Column], dict[Column, Column]]:
        tables: list[str] = []
        ignored: set[Column] = set()
        rows = await (await self.db.execute(COLUMNS, (self.s.schema,))).fetchall()
        for table, column, default, identity in rows:
            if table not in tables:
                tables.append(table)
            if identity == "YES" or GENERATED.search(default):
                ignored.add((table, column))
        keys = await (await self.db.execute(FOREIGN_KEYS, (self.s.schema,))).fetchall()
        return tables, ignored, {(t, c): (rt, rc) for t, c, rt, rc in keys}

    async def send(self, req: Request) -> tuple[int, bytes, http.client.HTTPMessage | None]:
        return await asyncio.to_thread(self._http, req)

    async def ask(self, req: Request) -> tuple[int, str | None]:
        """Send a request. Returns its status, and its body when bodies are compared."""
        status, body, _ = await self.send(req)
        return status, body.decode("utf-8", "replace") if self.s.bodies else None

    def _http(self, req: Request) -> tuple[int, bytes, http.client.HTTPMessage | None]:
        data = None if req.json is None else json.dumps(req.json).encode()
        headers = {"content-type": "application/json", **req.headers} if data else req.headers
        r = urllib.request.Request(self.s.app + req.path, data=data, method=req.method, headers=headers)
        try:
            try:
                resp = self.http.open(r, timeout=30)
            except urllib.error.HTTPError as e:
                resp = e  # an error status still has a body and headers worth reading
            with resp:
                return resp.code, resp.read(), resp.headers
        except (OSError, http.client.HTTPException):
            return 0, b"", None


def judge(
    outcome: Outcome,
    serials: list[Outcome],
    ignored: set[Column],
    fks: dict[Column, Column] | None = None,
    loose: dict[str, set[BodyPath]] | None = None,
) -> list[str]:
    """Why this outcome is impossible in any serial order. Empty if it is possible."""

    def answers(o: Outcome) -> dict[str, tuple[int, frozenset[tuple[BodyPath, str]] | None]]:
        return {role: (status, body_key(o.bodies.get(role), (loose or {}).get(role, ()))) for role, status in o.statuses.items()}

    mine, theirs = answers(outcome), [answers(s) for s in serials]
    state = normalize(outcome.snapshot, ignored, fks)
    states = [normalize(s.snapshot, ignored, fks) for s in serials]
    reasons = []
    if not any(mine == t and state == st for t, st in zip(theirs, states, strict=True)):
        if not any(outcome.statuses == s.statuses for s in serials):
            reasons.append("responses match no serial order")
        elif mine not in theirs:
            reasons.append("response bodies match no serial order")
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


def flat(body: str) -> dict[BodyPath, Any]:
    """A response body as {path: value}. A body that is not JSON is one value."""
    try:
        root = json.loads(body)
    except ValueError:
        return {(): body}
    out: dict[BodyPath, Any] = {}

    def walk(node: Any, path: BodyPath) -> None:
        if isinstance(node, (dict, list)) and node:
            for key, value in node.items() if isinstance(node, dict) else enumerate(node):
                walk(value, path + (key,))
        else:
            out[path] = node

    walk(root, ())
    return out


def differing(first: str | None, again: str | None) -> set[BodyPath]:
    """Paths whose values differ between the bodies of two identical runs."""
    if first is None or again is None:
        return set()
    a, b = flat(first), flat(again)
    return {path for path in a.keys() | b.keys() if a.get(path, MISSING) != b.get(path, MISSING)}


def body_key(body: str | None, loose: Collection[BodyPath]) -> frozenset[tuple[BodyPath, str]] | None:
    """What is left of a body to compare: no ids, tokens or timestamps, and no field that changes on its own."""
    if body is None:
        return None
    kept = set()
    for path, value in flat(body).items():
        names = [key for key in path if isinstance(key, str)]
        if path not in loose and not (names and GENERATED_KEY.search(names[-1])):
            kept.add((path, repr(value)))
    return frozenset(kept)


def captured(source: str, body: bytes, headers: http.client.HTTPMessage | None, who: str) -> str:
    """What a login hands on: a field of its JSON body, or a response header named after 'header:'."""
    if source.lower().startswith("header:"):
        name = source[7:].strip()
        values = (headers.get_all(name) if headers else None) or []
        if not values:
            raise RunError(f"login {who}: the response has no {name} header")
        # a Set-Cookie value carries attributes after its first part, which a Cookie header must not repeat
        return "; ".join(value.split(";")[0] for value in values)
    try:
        node = json.loads(body)
        for part in source.split("."):
            node = node[int(part)] if isinstance(node, list) else node[part]
    except (ValueError, LookupError, TypeError):
        raise RunError(f"login {who}: the response has no {source}") from None
    return str(node)


def fill(node: Any, values: dict[str, str]) -> Any:
    """Put captured login values into every ${login.name} of a path, headers or a JSON body."""
    if isinstance(node, str):
        return REF.sub(lambda m: values[f"{m[1]}.{m[2]}"], node)
    if isinstance(node, dict):
        return {key: fill(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [fill(value, values) for value in node]
    return node


def shape(sql: str) -> str:
    """The statement with its literals blanked."""
    return LITERAL.sub("?", sql)


def holds(trace: list[str]) -> list[Hold]:
    """The statements to hold A before, leaving out holds that repeat a serial order.

    While A has only opened a transaction, letting B run is B then A. Once A
    has nothing left but handing its connection back, it is A then B.
    """
    seen: Counter[str] = Counter()
    points: list[Hold] = []
    for i, sql in enumerate(trace):
        seen[sql] += 1
        started = any(not CONTROL.match(s) for s in trace[:i])
        unfinished = any(not (CONTROL.match(s) or POOL_RESET.search(s)) for s in trace[i:])
        if started and unfinished:
            points.append((sql, seen[sql]))
    return points


def footprint(trace: list[str], names: set[str], opaque: set[str], parents: dict[str, set[str]], children: dict[str, set[str]], changed: set[str]) -> Footprint:
    """The tables a request touched and the ones it wrote, or None when its SQL cannot tell.

    Every doubt widens the answer, so a pair is only ever skipped when it is safe.
    """
    touched, written = set(changed), set(changed)
    roots: set[str] = set()
    for sql in trace:
        words = set(re.findall(r"\w+", sql.lower()))
        if words & opaque:
            return None
        named = words & names
        if not named:
            if DATA.search(sql):
                return None
            continue
        touched |= named
        if WRITE.search(sql):
            written |= named
        if CASCADES.search(sql):
            roots |= named
    # a delete or a key update can travel down foreign keys to any depth
    stack = list(roots)
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in written:
                written.add(child)
                stack.append(child)
    touched |= written
    # writing a child row makes Postgres read its parent row
    touched |= {parent for table in touched for parent in parents.get(table, ())}
    return touched, written


def independent(a: Footprint, b: Footprint) -> bool:
    """True when neither request writes a table the other touches, so no order can matter."""
    return a is not None and b is not None and not (a[1] & b[0]) and not (b[1] & a[0])


def rows(table: Table) -> Counter[str]:
    return Counter(map(str, table[1]))


def normalize(snapshot: Snapshot, ignored: set[Column], fks: dict[Column, Column] | None = None) -> dict[str, Counter[tuple[str, ...]]]:
    """Each table as a multiset of rows, without the ignored columns.

    A reference to a generated id becomes the row it points to, so two runs
    that hand out ids in a different order still compare equal.
    """
    refs = {
        col: target
        for col, target in (fks or {}).items()
        if target in ignored and target[0] in snapshot and target[1] in snapshot[target[0]][0]
    }
    index: dict[Column, dict[Any, tuple[Any, ...]]] = {}

    def pointed(target: Column, value: Any) -> tuple[Any, ...] | None:
        if target not in index:
            cols, rows = snapshot[target[0]]
            i = cols.index(target[1])
            index[target] = {r[i]: r for r in rows}
        return index[target].get(value)

    def label(table: str, row: tuple[Any, ...], depth: int = 0) -> tuple[str, ...]:
        out: list[str] = []
        for col, value in zip(snapshot[table][0], row, strict=True):
            if (table, col) in ignored:
                continue
            target = refs.get((table, col))
            if target and value is not None and depth < 3:
                ref = pointed(target, value)
                value = "→ " + ", ".join(label(target[0], ref, depth + 1)) if ref else f"→ missing {value}"
            out.append(str(value))
        return tuple(out)

    return {table: Counter(label(table, row) for row in rows) for table, (_, rows) in snapshot.items()}


def unstable(first: Snapshot, again: Snapshot) -> set[Column]:
    """Columns whose values differ between two identical runs."""
    out: set[Column] = set()
    for table, (cols, rows) in first.items():
        other = again[table][1]
        for i, col in enumerate(cols):
            if Counter(str(r[i]) for r in rows) != Counter(str(r[i]) for r in other):
                out.add((table, col))
    return out
