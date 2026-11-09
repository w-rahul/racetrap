"""Write a starting scenario file from an OpenAPI document."""

import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

WRITES = ("post", "put", "patch", "delete")
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
PARAM = re.compile(r"\{([^}]+)\}")
FORMATS = {
    "email": "user@example.com",
    "date": "2026-01-01",
    "date-time": "2026-01-01T00:00:00Z",
    "uuid": "00000000-0000-0000-0000-000000000001",
    "uri": "https://example.com",
}

HEADER = """# Written by racetrap init from {source}
#
# 1. Fill setup with SQL that puts your test data back in a known state.
# 2. Keep only the requests that touch shared data. racetrap tries every
#    pair, so 5 requests make 25 pairs.
# 3. Check the example bodies and ids, then run: racetrap {out}
{auth}
app = {app}
database = "${{DATABASE_URL}}"
# command = "uvicorn main:app --port 8000"

setup = [
    # "TRUNCATE orders RESTART IDENTITY",
]
"""


class InitError(Exception):
    pass


def load_spec(source):
    if source.startswith(("http://", "https://")):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(source, timeout=10) as resp:
            text = resp.read().decode("utf-8")
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        # shortcut: JSON only. Add PyYAML if people ask for YAML specs.
        raise InitError("racetrap init reads OpenAPI as JSON. FastAPI serves it at /openapi.json") from None
    if not isinstance(spec, dict) or "paths" not in spec:
        raise InitError("that file has no 'paths', so it is not an OpenAPI document")
    return spec


def default_app(spec, source):
    if source.startswith(("http://", "https://")):
        u = urlsplit(source)
        return f"{u.scheme}://{u.netloc}"
    for server in spec.get("servers") or []:
        if str(server.get("url", "")).startswith(("http://", "https://")):
            return server["url"].rstrip("/")
    return "http://127.0.0.1:8000"


def scenario(spec, source, app, out):
    """The scenario file as text, and how many requests it has."""
    blocks, names = [], set()
    for path, item in (spec.get("paths") or {}).items():
        item = resolve(item, spec)
        for method in WRITES:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = (item.get("parameters") or []) + (op.get("parameters") or [])
            lines = [
                "[[request]]",
                f"name = {toml_value(unique(label(op, method, path), names))}",
                f'method = "{method.upper()}"',
                f"path = {toml_value(fill(path, params, spec))}",
            ]
            body = example(body_schema(op, spec), spec)
            if body is not None:
                lines.append(f"json = {toml_value(body)}")
            blocks.append("\n".join(lines))
    if not blocks:
        raise InitError("the spec has no POST, PUT, PATCH or DELETE operations, so there is nothing to race")
    secured = spec.get("security") or (spec.get("components") or {}).get("securitySchemes")
    auth = '# Add auth to each request: headers = { authorization = "Bearer ..." }\n' if secured else ""
    head = HEADER.format(source=source, out=out, app=toml_value(app), auth=auth)
    return head + "\n" + "\n\n".join(blocks) + "\n", len(blocks)


def label(op, method, path):
    if op.get("summary"):
        return str(op["summary"]).lower()
    return str(op.get("operationId") or f"{method} {path}")


def unique(name, names):
    candidate, n = name, 2
    while candidate in names:
        candidate, n = f"{name} {n}", n + 1
    names.add(candidate)
    return candidate


def fill(path, params, spec):
    by_name = {}
    for p in params:
        p = resolve(p, spec)
        if isinstance(p, dict) and p.get("in") == "path":
            by_name[p.get("name")] = p

    def value(m):
        p = by_name.get(m[1], {})
        v = p.get("example")
        if v is None:
            v = example(p.get("schema"), spec)
        return "1" if v is None or isinstance(v, (dict, list)) or v == "text" else str(v)

    return PARAM.sub(value, path)


def body_schema(op, spec):
    body = resolve(op.get("requestBody"), spec)
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    media = content.get("application/json") or next((v for k, v in content.items() if k.endswith("json")), None)
    return (media or {}).get("schema")


def resolve(node, spec, depth=0):
    while isinstance(node, dict) and "$ref" in node and depth < 20:
        target = spec
        for part in str(node["$ref"]).lstrip("#/").split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
        node, depth = target, depth + 1
    return node


def example(schema, spec, depth=0):
    """A plausible value for a JSON schema, preferring the spec's own examples."""
    schema = resolve(schema, spec)
    if not isinstance(schema, dict) or depth > 5:
        return None
    for key in ("example", "default"):
        if key in schema and schema[key] is not None:
            return schema[key]
    if schema.get("enum"):
        return schema["enum"][0]
    if schema.get("allOf"):
        merged = {}
        for part in schema["allOf"]:
            value = example(part, spec, depth + 1)
            if isinstance(value, dict):
                merged.update(value)
        return merged
    for combo in ("oneOf", "anyOf"):
        options = [s for s in schema.get(combo) or [] if resolve(s, spec).get("type") != "null"]
        if options:
            return example(options[0], spec, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind == "object" or "properties" in schema:
        out = {}
        for key, sub in (schema.get("properties") or {}).items():
            value = example(sub, spec, depth + 1)
            if value is not None:
                out[key] = value
        return out
    if kind == "array":
        item = example(schema.get("items"), spec, depth + 1)
        return [] if item is None else [item]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.5
    if kind == "boolean":
        return True
    if kind == "string":
        return FORMATS.get(schema.get("format"), "text")
    return None


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value if v is not None) + "]"
    if isinstance(value, dict):
        items = [f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items() if v is not None]
        return "{ " + ", ".join(items) + " }" if items else "{}"
    # a JSON string is also a valid TOML basic string
    return json.dumps(str(value), ensure_ascii=False)


def toml_key(key):
    return key if BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)
