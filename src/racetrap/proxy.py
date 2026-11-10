"""A Postgres proxy that can hold any statement before the server sees it."""

import asyncio
import itertools
import logging
import ssl
from collections.abc import Mapping
from typing import Any

from . import wire

log = logging.getLogger("racetrap")

# Extended protocol messages wait for Sync or Flush, so a statement is always
# held or forwarded as one piece together with its Parse and Bind.
BUFFERED = {b"P", b"B", b"D", b"E", b"C"}


class Conn:
    """One client connection through the proxy."""

    def __init__(self, cid: int) -> None:
        self.id = cid
        self.pid: int | None = None
        self.session = wire.Session()


class Hooks:
    """What the proxy reports. The default does nothing and never holds."""

    def opened(self, conn: Conn) -> None:
        pass

    def closed(self, conn: Conn) -> None:
        pass

    async def before(self, conn: Conn, sqls: list[str]) -> None:
        pass

    def done(self, conn: Conn, tag: str | None = None, error: dict[str, str] | None = None) -> None:
        pass


class Proxy:
    def __init__(self, listen: tuple[str, int], upstream: tuple[str, int], hooks: Hooks, tls: tuple[ssl.SSLContext, bool] | None = None) -> None:
        self.listen = listen
        self.upstream = upstream
        self.hooks = hooks
        self.tls = tls
        self.ids = itertools.count(1)
        self.tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._accept, *self.listen)

    async def close(self) -> None:
        self.server.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.server.wait_closed()

    async def _accept(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None  # asyncio runs this callback as a task
        self.tasks.add(task)
        try:
            await self._serve(creader, cwriter)
        except (asyncio.IncompleteReadError, ConnectionError, wire.ProtocolError) as e:
            log.debug("connection ended: %r", e)
        except ssl.SSLError as e:
            log.warning("TLS to the database failed: %s", e)
        finally:
            self.tasks.discard(task)
            cwriter.close()

    async def _serve(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        code, raw = await wire.read_startup(creader)
        while code in (wire.SSL_REQUEST, wire.GSSENC_REQUEST):
            # The app side is plain TCP. Clients on sslmode=prefer retry without TLS.
            cwriter.write(b"N")
            code, raw = await wire.read_startup(creader)
        # localhost often resolves to ::1 first. Racing IPv4 after 250 ms keeps a
        # refused IPv6 attempt from costing seconds on every new connection.
        ureader, uwriter = await asyncio.open_connection(*self.upstream, happy_eyeballs_delay=0.25)
        try:
            if self.tls:
                await self._encrypt(ureader, uwriter, *self.tls)
            uwriter.write(raw)
            if code == wire.CANCEL_REQUEST:
                await uwriter.drain()
                return
            conn = Conn(next(self.ids))
            self.hooks.opened(conn)
            pumps = {
                asyncio.create_task(self._upstream(conn, creader, uwriter)),
                asyncio.create_task(self._downstream(conn, ureader, cwriter)),
            }
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
                self.hooks.closed(conn)
        finally:
            uwriter.close()

    async def _encrypt(self, ureader: asyncio.StreamReader, uwriter: asyncio.StreamWriter, context: ssl.SSLContext, required: bool) -> None:
        uwriter.write(wire.SSL_REQUEST_PACKET)
        if await ureader.readexactly(1) == b"S":
            await uwriter.start_tls(context, server_hostname=self.upstream[0])
        elif required:
            raise ssl.SSLError("the server does not offer TLS, but sslmode asks for it")

    async def _upstream(self, conn: Conn, creader: asyncio.StreamReader, uwriter: asyncio.StreamWriter) -> None:
        held: list[bytes] = []
        sqls: list[str] = []
        while True:
            kind, body, raw = await wire.read_message(creader)
            sql = conn.session.feed(kind, body)
            if sql is not None:
                sqls.append(sql)
            held.append(raw)
            if kind in BUFFERED:
                continue
            if sqls:
                await self.hooks.before(conn, sqls)
                sqls = []
            uwriter.write(b"".join(held))
            held = []
            await uwriter.drain()
            if kind == b"X":
                return

    async def _downstream(self, conn: Conn, ureader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        rows = 0
        while True:
            kind, body, raw = await wire.read_message(ureader)
            if kind == b"D":
                rows += 1
            elif kind == b"K":
                conn.pid = wire.backend_pid(body)
            elif kind == b"C":
                self.hooks.done(conn, tag=wire.cstring(body)[0])
                rows = 0
            elif kind == b"s":
                # asyncpg's fetchval asks for one row, which ends the portal
                # with PortalSuspended instead of CommandComplete
                self.hooks.done(conn, tag=f"{rows} row" + ("" if rows == 1 else "s"))
                rows = 0
            elif kind == b"E":
                self.hooks.done(conn, error=wire.error_fields(body))
                rows = 0
            cwriter.write(raw)
            await cwriter.drain()


def tls_context(params: Mapping[str, Any]) -> tuple[ssl.SSLContext, bool] | None:
    """Read sslmode the way libpq does. Returns (context, required), or None for plain TCP."""
    mode = params.get("sslmode", "prefer")
    if mode == "disable":
        return None
    if mode in ("verify-ca", "verify-full"):
        # shortcut: without sslrootcert this trusts the system store, where libpq
        # reads ~/.postgresql/root.crt. Client certificates are not sent.
        root = params.get("sslrootcert")
        context = ssl.create_default_context(cafile=None if root in (None, "system") else root)
        context.check_hostname = mode == "verify-full"
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context, mode not in ("allow", "prefer")
