import pytest

from deadheat.scenario import ScenarioError, load

GOOD = """
app = "http://127.0.0.1:8000/"
database = "${TEST_DB}"
setup = ["TRUNCATE orders"]
ignore = ["orders.updated_at"]

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


@pytest.mark.parametrize(
    "old, new, error",
    [
        ('database = "${TEST_DB}"', "", "missing 'database'"),
        ('"/buy"', '"buy"', "must start with /"),
        ('"/buy"', '"/buy now"', "no spaces"),
        ('["orders.updated_at"]', '["updated_at"]', "table.column"),
        ("[[request]]", "[[other]]", "at least one"),
        ('setup = ["TRUNCATE orders"]', "setup = [1]", "list of SQL strings"),
    ],
)
def test_rejects_bad_scenarios(tmp_path, monkeypatch, old, new, error):
    monkeypatch.setenv("TEST_DB", "postgresql://localhost/shop")
    with pytest.raises(ScenarioError, match=error):
        load(write(tmp_path, GOOD.replace(old, new)))


def test_unset_environment_variable_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DB", raising=False)
    with pytest.raises(ScenarioError, match="TEST_DB is not set"):
        load(write(tmp_path, GOOD))
