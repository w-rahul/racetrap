"""Scenario files: which requests to race, against which app and database."""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ScenarioError(Exception):
    pass


@dataclass
class Request:
    name: str
    method: str
    path: str
    json: object = None
    headers: dict = field(default_factory=dict)


@dataclass
class Scenario:
    path: Path
    app: str
    database: str
    requests: list
    setup: list
    init: list = field(default_factory=list)
    invariants: list = field(default_factory=list)
    ignore: set = field(default_factory=set)
    refs: dict = field(default_factory=dict)
    proxy: tuple = ("127.0.0.1", 6499)
    command: str | None = None
    database_env: str = "DATABASE_URL"
    schema: str = "public"
    stall: float = 1.0

    @property
    def upstream(self):
        u = urlsplit(self.database)
        return u.hostname or "127.0.0.1", u.port or 5432

    @property
    def proxied_database(self):
        """The database URL with the proxy in place of the real server."""
        return with_address(self.database, *self.proxy)


def with_address(url, host, port):
    u = urlsplit(url)
    auth = u.netloc.rpartition("@")[0]
    netloc = f"{host}:{port}" if not auth else f"{auth}@{host}:{port}"
    return urlunsplit(u._replace(netloc=netloc))


def load(path):
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ScenarioError(f"{path}: {e}") from None
    get = _getter(data, path.name)

    requests = [_request(r, i, path.name) for i, r in enumerate(get("request", list, []))]
    if not requests:
        raise ScenarioError(f"{path.name}: add at least one [[request]]")
    names = [r.name for r in requests]
    if len(set(names)) != len(names):
        raise ScenarioError(f"{path.name}: request names must be unique")

    host, _, port = get("proxy", str, "127.0.0.1:6499").rpartition(":")
    if not host or not port.isdigit():
        raise ScenarioError(f"{path.name}: proxy must look like 127.0.0.1:6499")

    column = _column_parser(path.name)
    ignore = {column(item, "ignore") for item in get("ignore", list, [])}
    refs = {column(k, "refs"): column(v, "refs") for k, v in get("refs", dict, {}).items()}

    app = _expand(get("app", str)).rstrip("/")
    if not app.startswith(("http://", "https://")):
        raise ScenarioError(f"{path.name}: app must start with http:// or https://")
    database = _expand(get("database", str))
    try:
        urlsplit(database).port
    except ValueError:
        raise ScenarioError(f"{path.name}: database URL has an invalid port") from None
    stall = get("stall_ms", int, 1000)
    if stall <= 0:
        raise ScenarioError(f"{path.name}: stall_ms must be above 0")

    command = get("command", str, None)
    return Scenario(
        path=path,
        app=app,
        database=database,
        requests=requests,
        setup=_sql_list(get("setup", list), "setup", path.name),
        init=_sql_list(get("init", list, []), "init", path.name),
        invariants=_sql_list(get("invariants", list, []), "invariants", path.name),
        ignore=ignore,
        refs=refs,
        proxy=(host, int(port)),
        command=_expand(command) if command else None,
        database_env=get("database_env", str, "DATABASE_URL"),
        schema=get("schema", str, "public"),
        stall=stall / 1000,
    )


def _column_parser(where):
    def column(item, key):
        table, dot, name = str(item).partition(".")
        if not dot or not table or not name:
            raise ScenarioError(f"{where}: {key} entries look like table.column, got {item!r}")
        return table, name

    return column


_REQUIRED = object()


def _getter(data, where):
    def get(key, kind, default=_REQUIRED):
        if key not in data:
            if default is _REQUIRED:
                raise ScenarioError(f"{where}: missing {key!r}")
            return default
        if not isinstance(data[key], kind):
            raise ScenarioError(f"{where}: {key!r} must be a {kind.__name__}")
        return data[key]

    return get


def _request(raw, i, where):
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: request {i + 1} must be a table")
    get = _getter(raw, f"{where} request {i + 1}")
    path = get("path", str)
    if not path.startswith("/") or any(c.isspace() for c in path):
        raise ScenarioError(f"{where}: request path {path!r} must start with / and have no spaces")
    return Request(
        name=get("name", str),
        method=get("method", str, "POST" if "json" in raw else "GET").upper(),
        path=path,
        json=raw.get("json"),
        headers={str(k): str(v) for k, v in get("headers", dict, {}).items()},
    )


def _sql_list(items, key, where):
    if not all(isinstance(s, str) for s in items):
        raise ScenarioError(f"{where}: {key} must be a list of SQL strings")
    return items


def _expand(value):
    def sub(m):
        if m[1] not in os.environ:
            raise ScenarioError(f"environment variable {m[1]} is not set")
        return os.environ[m[1]]

    return re.sub(r"\$\{(\w+)\}", sub, value)
