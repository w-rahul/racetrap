import pytest

from racetrap.scenario import ScenarioError, load

GOOD = """
app = "http://127.0.0.1:8000/"
database = "${TEST_DB}"
setup = ["TRUNCATE orders"]
ignore = ["orders.updated_at"]
refs = { "orders.user_id" = "users.id" }

[[request]]
name = "buy"
path = "/buy"
json = { item = 1 }
"""


def write(tmp_path, text):
    path = tmp_path / "scenario.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_scenario(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DB", "postgresql://u:p@db.local:5433/shop")
    s = load(write(tmp_path, GOOD))
    assert s.app == "http://127.0.0.1:8000"
    assert s.upstream == ("db.local", 5433)
    assert s.proxied_database == "postgresql://u:p@127.0.0.1:6499/shop"
    assert s.requests[0].method == "POST"
    assert s.ignore == {("orders", "updated_at")}
    assert s.refs == {("orders", "user_id"): ("users", "id")}


@pytest.mark.parametrize(
    "old, new, error",
    [
        ('database = "${TEST_DB}"', "", "missing 'database'"),
        ('"/buy"', '"buy"', "must start with /"),
        ('"/buy"', '"/buy now"', "no spaces"),
        ('["orders.updated_at"]', '["updated_at"]', "table.column"),
        ("[[request]]", "[[other]]", "at least one"),
        ('setup = ["TRUNCATE orders"]', "setup = [1]", "list of SQL strings"),
        ('app = "http://127.0.0.1:8000/"', 'app = "127.0.0.1:8000"', "http://"),
        ("setup = [", "stall_ms = 0\nsetup = [", "stall_ms"),
        ('"users.id"', '"users"', "table.column"),
    ],
)
def test_rejects_bad_scenarios(tmp_path, monkeypatch, old, new, error):
    monkeypatch.setenv("TEST_DB", "postgresql://localhost/shop")
    with pytest.raises(ScenarioError, match=error):
        load(write(tmp_path, GOOD.replace(old, new)))


def test_bad_database_port_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DB", "postgresql://localhost:99999/shop")
    with pytest.raises(ScenarioError, match="invalid port"):
        load(write(tmp_path, GOOD))


def test_unset_environment_variable_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DB", raising=False)
    with pytest.raises(ScenarioError, match="TEST_DB is not set"):
        load(write(tmp_path, GOOD))
