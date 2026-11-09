import asyncio

import asyncpg
import psycopg

from conftest import DSN, needs_db
from racetrap.cli import run_async
from racetrap.proxy import Hooks, Proxy
from racetrap.scenario import Scenario, with_address


class Tape(Hooks):
    def __init__(self, hold=None):
        self.sqls = []
        self.pids = set()
        self.hold = hold
        self.reached = asyncio.Event()
        self.gate = asyncio.Event()

    async def before(self, conn, sqls):
        self.sqls += sqls
        self.pids.add(conn.pid)
        if self.hold and any(self.hold in s for s in sqls):
            self.reached.set()
            await self.gate.wait()


async def started(hooks):
    upstream = Scenario(path=None, app="", database=DSN, requests=[], setup=[]).upstream
    proxy = Proxy(("127.0.0.1", 0), upstream, hooks)
    await proxy.start()
    port = proxy.server.sockets[0].getsockname()[1]
    return proxy, with_address(DSN, "127.0.0.1", port)


@needs_db
def test_records_sql_from_asyncpg_and_psycopg():
    async def go():
        tape = Tape()
        proxy, url = await started(tape)
        conn = await asyncpg.connect(url)
        for n in (1, 2):  # the second run reuses asyncpg's prepared statement
            assert await conn.fetchval("SELECT $1::int + 1", n) == n + 1
        await conn.close()
        async with await psycopg.AsyncConnection.connect(url) as pg:
            await pg.execute("SELECT %s::text", ("hi",))
        await proxy.close()
        return tape

    tape = run_async(go())
    assert tape.sqls.count("SELECT $1::int + 1") == 2
    assert "SELECT $1::text" in tape.sqls
    assert None not in tape.pids


@needs_db
def test_held_statement_waits_for_release():
    async def go():
        tape = Tape(hold="pg_sleep")
        proxy, url = await started(tape)
        conn = await asyncpg.connect(url)
        query = asyncio.create_task(conn.fetchval("SELECT 7 FROM pg_sleep(0)"))
        await tape.reached.wait()
        await asyncio.sleep(0.2)
        assert not query.done()
        tape.gate.set()
        result = await query
        await conn.close()
        await proxy.close()
        return result

    assert run_async(go()) == 7
