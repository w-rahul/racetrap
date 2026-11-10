"""The parts of the Postgres wire protocol that racetrap needs.

Splits a byte stream into messages and maps every Execute back to its SQL,
for both the simple and the extended query protocol.
"""

import struct
from asyncio import StreamReader

SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104
CANCEL_REQUEST = 80877102
SSL_REQUEST_PACKET = struct.pack("!II", 8, SSL_REQUEST)

# Postgres itself refuses messages over 1 GB, so a bigger length is garbage.
MAX_LENGTH = 1 << 30

# Postgres caps startup packets at 10000 bytes. A client that opens with a TLS
# ClientHello sends bytes that read as a huge length and would wait forever.
MAX_STARTUP = 10000


class ProtocolError(Exception):
    pass


def _length(raw: bytes, limit: int = MAX_LENGTH) -> int:
    (n,) = struct.unpack("!I", raw)
    if not 4 <= n <= limit:
        raise ProtocolError(f"bad message length {n}")
    return n


async def read_startup(reader: StreamReader) -> tuple[int, bytes]:
    """Read one untyped startup packet. Returns its request code and raw bytes."""
    head = await reader.readexactly(4)
    body = await reader.readexactly(_length(head, MAX_STARTUP) - 4)
    if len(body) < 4:
        raise ProtocolError("startup packet too short")
    return struct.unpack("!I", body[:4])[0], head + body


async def read_message(reader: StreamReader) -> tuple[bytes, bytes, bytes]:
    """Read one typed message. Returns its type byte, body and raw bytes."""
    head = await reader.readexactly(5)
    body = await reader.readexactly(_length(head[1:]) - 4)
    return head[:1], body, head + body


def cstring(buf: bytes, pos: int = 0) -> tuple[str, int]:
    """Decode the NUL terminated string at pos. Returns it and the next position."""
    end = buf.find(b"\0", pos)
    if end < 0:
        raise ProtocolError("unterminated string")
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def backend_pid(body: bytes) -> int:
    """Process id from a BackendKeyData message."""
    return struct.unpack("!I", body[:4])[0]


def error_fields(body: bytes) -> dict[str, str]:
    """Fields of an ErrorResponse, keyed by their one letter code."""
    fields: dict[str, str] = {}
    pos = 0
    while pos < len(body) and body[pos]:
        fields[chr(body[pos])], pos = cstring(body, pos + 1)
    return fields


class Session:
    """Prepared statements and portals of one client connection.

    asyncpg, psycopg, node-postgres and most ORMs send the SQL once in Parse
    and then run it by name, so an Execute means nothing without this state.
    """

    def __init__(self) -> None:
        self.statements: dict[str, str] = {}
        self.portals: dict[str, str] = {}

    def feed(self, kind: bytes, body: bytes) -> str | None:
        """Apply one frontend message. Returns the SQL it runs, if it runs one."""
        if kind == b"Q":
            return cstring(body)[0]
        if kind == b"P":
            name, pos = cstring(body)
            self.statements[name] = cstring(body, pos)[0]
        elif kind == b"B":
            portal, pos = cstring(body)
            self.portals[portal] = self.statements.get(cstring(body, pos)[0], "")
        elif kind == b"E":
            return self.portals.get(cstring(body)[0], "")
        elif kind == b"C":
            names = self.statements if body[:1] == b"S" else self.portals
            names.pop(cstring(body, 1)[0], None)
        return None
