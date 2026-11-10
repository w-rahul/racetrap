import asyncio
import ssl

import asyncpg
import psycopg
import psycopg2
import pytest
import trustme

from conftest import DSN, needs_db
from racetrap.cli import run_async
from racetrap.proxy import Hooks, Proxy, tls_context
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


@needs_db
def test_records_and_holds_psycopg2():
    """psycopg2 sends plain text queries with the values already written in."""

    def query(url):
        conn = psycopg2.connect(url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT %s::int + 1", (41,))
                return cur.fetchone()[0]
        finally:
            conn.close()

    async def go():
        tape = Tape(hold="41::int")
        proxy, url = await started(tape)
        task = asyncio.create_task(asyncio.to_thread(query, url))
        await tape.reached.wait()
        await asyncio.sleep(0.2)
        assert not task.done()
        tape.gate.set()
        result = await task
        await proxy.close()
        return tape, result

    tape, result = run_async(go())
    assert result == 42
    assert "SELECT 41::int + 1" in tape.sqls


async def tls_front(context, target, seen):
    """What a hosted Postgres looks like: it answers the TLS request, then speaks TLS."""

    async def pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    async def serve(reader, writer):
        await reader.readexactly(8)
        writer.write(b"S")
        await writer.start_tls(context)
        seen.append(writer.get_extra_info("cipher")[0])
        db_reader, db_writer = await asyncio.open_connection(*target)
        await asyncio.gather(pipe(reader, db_writer), pipe(db_reader, writer), return_exceptions=True)

    return await asyncio.start_server(serve, "127.0.0.1", 0)


async def through_tls(tmp_path, params, trusted=True):
    """Run one query through the proxy, with a TLS server between it and the database."""
    ca = trustme.CA()
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("127.0.0.1").configure_cert(server_context)
    (trustme.CA() if not trusted else ca).cert_pem.write_to_path(tmp_path / "root.pem")
    seen = []
    scenario = Scenario(path=None, app="", database=DSN, requests=[])
    front = await tls_front(server_context, scenario.upstream, seen)
    port = front.sockets[0].getsockname()[1]
    params = {k: v.replace("ROOT", str(tmp_path / "root.pem")) for k, v in params.items()}
    proxy = Proxy(("127.0.0.1", 0), ("127.0.0.1", port), Hooks(), tls_context(params))
    await proxy.start()
    url = with_address(DSN, "127.0.0.1", proxy.server.sockets[0].getsockname()[1])
    try:
        async with await psycopg.AsyncConnection.connect(url, sslmode="disable", connect_timeout=5) as pg:
            return (await (await pg.execute("SELECT 7")).fetchone())[0], seen
    finally:
        await proxy.close()
        front.close()


@needs_db
@pytest.mark.parametrize("params", [{"sslmode": "require"}, {}, {"sslmode": "verify-full", "sslrootcert": "ROOT"}, {"sslmode": "verify-ca", "sslrootcert": "ROOT"}])
def test_speaks_tls_to_the_database(tmp_path, params):
    answer, seen = run_async(through_tls(tmp_path, params))
    assert answer == 7
    assert len(seen) == 1 and seen[0], "the query went over one TLS connection"


@needs_db
def test_refuses_a_server_certificate_it_cannot_verify(tmp_path, caplog):
    with pytest.raises(psycopg.OperationalError):
        run_async(through_tls(tmp_path, {"sslmode": "verify-full", "sslrootcert": "ROOT"}, trusted=False))
    assert "TLS to the database failed" in caplog.text


@needs_db
def test_falls_back_to_plain_tcp_unless_tls_is_required(caplog):
    """The test database has TLS off, which is fine for prefer and an error for require."""

    async def go(params):
        scenario = Scenario(path=None, app="", database=DSN, requests=[])
        proxy = Proxy(("127.0.0.1", 0), scenario.upstream, Hooks(), tls_context(params))
        await proxy.start()
        url = with_address(DSN, "127.0.0.1", proxy.server.sockets[0].getsockname()[1])
        try:
            async with await psycopg.AsyncConnection.connect(url, sslmode="disable", connect_timeout=5) as pg:
                return (await (await pg.execute("SELECT 7")).fetchone())[0]
        finally:
            await proxy.close()

    with psycopg.connect(DSN) as db:
        if db.execute("SHOW ssl").fetchone() == ("on",):
            pytest.skip("this database has TLS on")
    assert run_async(go({})) == 7
    with pytest.raises(psycopg.OperationalError):
        run_async(go({"sslmode": "require"}))
    assert "does not offer TLS" in caplog.text


def test_reads_sslmode_like_libpq():
    assert tls_context({"sslmode": "disable"}) is None
    context, required = tls_context({})
    assert not required and context.verify_mode == ssl.CERT_NONE
    context, required = tls_context({"sslmode": "require"})
    assert required and context.verify_mode == ssl.CERT_NONE
    context, required = tls_context({"sslmode": "verify-ca"})
    assert required and context.verify_mode == ssl.CERT_REQUIRED and not context.check_hostname
    assert tls_context({"sslmode": "verify-full"})[0].check_hostname


def test_the_app_gets_a_url_without_tls():
    def proxied(database):
        return Scenario(path=None, app="", database=database, requests=[]).proxied_database

    assert proxied("postgresql://u:p@db.example.com/shop") == "postgresql://u:p@127.0.0.1:6499/shop"
    assert proxied("postgresql://u:p@db.example.com:5433/shop?sslmode=require&channel_binding=require&options=-c%20search_path%3Dx") == (
        "postgresql://u:p@127.0.0.1:6499/shop?sslmode=disable&options=-c%20search_path%3Dx"
    )
