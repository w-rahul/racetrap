"""Scenario files: which requests to race, against which app and database."""

import json
import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

# A value a login captured, written as ${login.name}.
REF = re.compile(r"\$\{(\w+)\.(\w+)\}")

# A table and one of its columns.
Column = tuple[str, str]


class ScenarioError(Exception):
    pass


@dataclass
class Request:
    name: str
    method: str
    path: str
    json: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] | None = None


@dataclass
class Login:
    """A request sent once before the run, and what to keep from its response."""

    name: str
    request: Request
    capture: dict[str, str]


@dataclass
class Scenario:
    path: Path
    app: str
    database: str
    requests: list[Request]
    setup: list[str] = field(default_factory=list)
    init: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    ignore: set[Column] = field(default_factory=set)
    refs: dict[Column, Column] = field(default_factory=dict)
    proxy: tuple[str, int] = ("127.0.0.1", 6499)
    command: str | None = None
    database_env: str = "DATABASE_URL"
    schema: str = "public"
    stall: float = 1.0
    logins: list[Login] = field(default_factory=list)
    bodies: bool = True

    @property
    def upstream(self) -> tuple[str, int]:
        u = urlsplit(self.database)
        return u.hostname or "127.0.0.1", u.port or 5432

    @property
    def proxied_database(self) -> str:
        """The database URL with the proxy in place of the real server."""
        u = urlsplit(with_address(self.database, *self.proxy))
        if "sslmode=" not in u.query and "channel_binding=" not in u.query:
            return urlunsplit(u)
        # The proxy speaks plain TCP to the app and opens TLS to the server itself.
        query = [(k, "disable" if k == "sslmode" else v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "channel_binding"]
        return urlunsplit(u._replace(query=urlencode(query, quote_via=quote)))


def with_address(url: str, host: str, port: int) -> str:
    u = urlsplit(url)
    auth = u.netloc.rpartition("@")[0]
    netloc = f"{host}:{port}" if not auth else f"{auth}@{host}:{port}"
    return urlunsplit(u._replace(netloc=netloc))


def load(path: str | Path) -> Scenario:
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

    logins = []
    for i, raw in enumerate(get("login", list, [])):
        request = _request(raw, i, f"{path.name} login")
        capture = raw.get("capture")
        if not isinstance(capture, dict) or not capture or not all(isinstance(v, str) for v in capture.values()):
            raise ScenarioError(f'{path.name}: login {request.name} needs capture, like {{ token = "access_token" }}')
        logins.append(Login(request.name, request, capture))
    known = {f"{login.name}.{var}" for login in logins for var in login.capture}
    for r in requests:
        for name, var in REF.findall(json.dumps([r.path, r.headers, r.json, r.form])):
            if f"{name}.{var}" not in known:
                raise ScenarioError(f"{path.name}: {r.name} uses ${{{name}.{var}}}, but no login captures it")

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
        _ = urlsplit(database).port  # raises when the port is not a valid number
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
        setup=_sql_list(get("setup", list, []), "setup", path.name),
        init=_sql_list(get("init", list, []), "init", path.name),
        invariants=_sql_list(get("invariants", list, []), "invariants", path.name),
        ignore=ignore,
        refs=refs,
        proxy=(host, int(port)),
        command=_expand(command) if command else None,
        database_env=get("database_env", str, "DATABASE_URL"),
        schema=get("schema", str, "public"),
        stall=stall / 1000,
        logins=logins,
        bodies=get("bodies", bool, True),
    )


def _column_parser(where: str) -> Callable[[object, str], Column]:
    def column(item: object, key: str) -> Column:
        table, dot, name = str(item).partition(".")
        if not dot or not table or not name:
            raise ScenarioError(f"{where}: {key} entries look like table.column, got {item!r}")
        return table, name

    return column


_REQUIRED = object()


def _getter(data: dict[str, Any], where: str) -> Callable[..., Any]:
    def get(key: str, kind: type, default: Any = _REQUIRED) -> Any:
        if key not in data:
            if default is _REQUIRED:
                raise ScenarioError(f"{where}: missing {key!r}")
            return default
        if not isinstance(data[key], kind):
            raise ScenarioError(f"{where}: {key!r} must be a {kind.__name__}")
        return data[key]

    return get


def _request(raw: Any, i: int, where: str) -> Request:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: request {i + 1} must be a table")
    get = _getter(raw, f"{where} request {i + 1}")
    path = get("path", str)
    if not path.startswith("/") or any(c.isspace() for c in path):
        raise ScenarioError(f"{where}: request path {path!r} must start with / and have no spaces")
    form = get("form", dict, None)
    if form is not None and "json" in raw:
        raise ScenarioError(f"{where}: request {i + 1} has both json and form. Keep one")
    return Request(
        name=get("name", str),
        method=get("method", str, "POST" if "json" in raw or form is not None else "GET").upper(),
        path=path,
        json=raw.get("json"),
        headers={str(k): str(v) for k, v in get("headers", dict, {}).items()},
        form=None if form is None else {str(k): str(v) for k, v in form.items()},
    )


def _sql_list(items: list[Any], key: str, where: str) -> list[str]:
    if not all(isinstance(s, str) for s in items):
        raise ScenarioError(f"{where}: {key} must be a list of SQL strings")
    return items


def _expand(value: str) -> str:
    def sub(m: re.Match[str]) -> str:
        if m[1] not in os.environ:
            raise ScenarioError(f"environment variable {m[1]} is not set")
        return os.environ[m[1]]

    return re.sub(r"\$\{(\w+)\}", sub, value)
