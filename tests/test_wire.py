import asyncio
import struct

import pytest

from racetrap import wire


def msg(kind, *parts):
    body = b"".join(p if isinstance(p, bytes) else p.encode() + b"\0" for p in parts)
    return kind + struct.pack("!I", len(body) + 4) + body


def feed(session, raw):
    return session.feed(raw[:1], raw[5:])


def test_simple_query():
    assert feed(wire.Session(), msg(b"Q", "SELECT 1")) == "SELECT 1"


def test_named_statement_runs_by_name():
    s = wire.Session()
    assert feed(s, msg(b"P", "s1", "UPDATE t SET n = $1", b"\0\0")) is None
    assert feed(s, msg(b"B", "", "s1", b"\0\0\0\0\0\0")) is None
    assert feed(s, msg(b"E", "", b"\0\0\0\0")) == "UPDATE t SET n = $1"
    # asyncpg and psycopg skip Parse once a statement is prepared
    feed(s, msg(b"B", "p", "s1", b"\0\0\0\0\0\0"))
    assert feed(s, msg(b"E", "p", b"\0\0\0\0")) == "UPDATE t SET n = $1"


def test_unnamed_statement_is_replaced():
    s = wire.Session()
    feed(s, msg(b"P", "", "SELECT a"))
    feed(s, msg(b"P", "", "SELECT b"))
    feed(s, msg(b"B", "", ""))
    assert feed(s, msg(b"E", "")) == "SELECT b"


def test_close_forgets_statement():
    s = wire.Session()
    feed(s, msg(b"P", "s1", "SELECT 1"))
    feed(s, msg(b"C", b"S", "s1"))
    feed(s, msg(b"B", "", "s1"))
    assert feed(s, msg(b"E", "")) == ""


def test_error_fields():
    body = b"SERROR\0C40P01\0Mdeadlock detected\0\0"
    assert wire.error_fields(body) == {"S": "ERROR", "C": "40P01", "M": "deadlock detected"}


def read(func, data):
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return await func(reader)

    return asyncio.run(go())


def test_read_message_keeps_raw_bytes():
    raw = msg(b"Q", "SELECT 1")
    assert read(wire.read_message, raw + b"extra") == (b"Q", raw[5:], raw)


def test_read_startup():
    raw = struct.pack("!II", 8, wire.SSL_REQUEST)
    assert read(wire.read_startup, raw) == (wire.SSL_REQUEST, raw)


@pytest.mark.parametrize("length", [3, wire.MAX_LENGTH + 1])
def test_rejects_bad_length(length):
    with pytest.raises(wire.ProtocolError):
        read(wire.read_message, b"Q" + struct.pack("!I", length))


def test_rejects_unterminated_string():
    with pytest.raises(wire.ProtocolError):
        feed(wire.Session(), msg(b"Q", b"SELECT 1"))


def test_rejects_oversized_startup_packet():
    # the first bytes of a TLS ClientHello read as a length of about 369 MB
    with pytest.raises(wire.ProtocolError):
        read(wire.read_startup, bytes.fromhex("16030101") + b"\0" * 8)
