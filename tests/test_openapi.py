import tomllib

import pytest

from racetrap.cli import main
from racetrap.openapi import InitError, scenario
from racetrap.scenario import load

SPEC = {
    "openapi": "3.1.0",
    "paths": {
        "/shows/{show_id}/book": {
            "post": {
                "summary": "Book",
                "parameters": [{"name": "show_id", "in": "path", "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Booking"}}}},
            }
        },
        "/shows": {"get": {"summary": "List shows"}},
        "/coupons/{code}": {
            "delete": {
                "operationId": "drop_coupon",
                "parameters": [{"name": "code", "in": "path", "schema": {"type": "string", "example": "SUMMER"}}],
            }
        },
    },
    "components": {
        "schemas": {
            "Booking": {
                "type": "object",
                "properties": {
                    "user": {"type": "string"},
                    "seats": {"type": "integer", "default": 2},
                    "email": {"type": "string", "format": "email"},
                    "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "tags": {"type": "array", "items": {"type": "string", "enum": ["vip"]}},
                    "quote": {"type": "string", "example": 'say "hi"\nthen go'},
                },
            }
        }
    },
}


def test_writes_one_request_per_write_operation():
    text, count = scenario(SPEC, "openapi.json", "http://127.0.0.1:8000", "racetrap.toml")
    data = tomllib.loads(text)
    assert count == 2
    book, drop = data["request"]
    assert book == {
        "name": "book",
        "method": "POST",
        "path": "/shows/1/book",
        "json": {"user": "text", "seats": 2, "email": "user@example.com", "note": "text", "tags": ["vip"], "quote": 'say "hi"\nthen go'},
    }
    assert drop == {"name": "drop_coupon", "method": "DELETE", "path": "/coupons/SUMMER"}
    assert data["app"] == "http://127.0.0.1:8000"
    assert "setup" not in data, "racetrap restores the data itself"


def test_names_stay_unique():
    spec = {"paths": {"/a": {"post": {"summary": "Book"}}, "/b": {"post": {"summary": "Book"}}}}
    data = tomllib.loads(scenario(spec, "s.json", "http://x", "r.toml")[0])
    assert [r["name"] for r in data["request"]] == ["book", "book 2"]


def test_a_read_only_api_is_an_error():
    with pytest.raises(InitError, match="nothing to race"):
        scenario({"paths": {"/a": {"get": {}}}}, "s.json", "http://x", "r.toml")


def test_init_writes_a_file_racetrap_can_load(tmp_path, monkeypatch):
    import json

    spec_file = tmp_path / "openapi.json"
    spec_file.write_text(json.dumps(SPEC), encoding="utf-8")
    out = tmp_path / "racetrap.toml"
    assert main(["init", "--openapi", str(spec_file), "-o", str(out)]) == 0
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/shop")
    assert [r.path for r in load(out).requests] == ["/shows/1/book", "/coupons/SUMMER"]
    assert main(["init", "--openapi", str(spec_file), "-o", str(out)]) == 2, "it does not overwrite by default"


YAML_SPEC = """
openapi: 3.0.3
paths:
  /orders/{order_id}/pay:
    post:
      summary: Pay
      parameters:
        - name: order_id
          in: path
          schema: {type: integer}
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                amount: {type: number}
                due: {type: string, format: date, example: 2026-01-31}
"""


def test_init_reads_yaml(tmp_path, monkeypatch):
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(YAML_SPEC, encoding="utf-8")
    out = tmp_path / "racetrap.toml"
    assert main(["init", "--openapi", str(spec_file), "-o", str(out)]) == 0
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/shop")
    (request,) = load(out).requests
    assert (request.method, request.path) == ("POST", "/orders/1/pay")
    # YAML turns a bare date into a date object, which must come out as text
    assert request.json == {"amount": 1.5, "due": "2026-01-31"}


@pytest.mark.parametrize("text, error", [("a: [unclosed", "JSON or YAML"), ("just: text", "no 'paths'")])
def test_init_rejects_what_is_not_openapi(tmp_path, capsys, text, error):
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(text, encoding="utf-8")
    assert main(["init", "--openapi", str(spec_file), "-o", str(tmp_path / "out.toml")]) == 2
    assert error in capsys.readouterr().err
