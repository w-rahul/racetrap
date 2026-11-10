import os

import pytest

DSN = os.environ.get("RACETRAP_TEST_DATABASE")

needs_db = pytest.mark.skipif(not DSN, reason="set RACETRAP_TEST_DATABASE to a Postgres URL")

pytest_plugins = ["pytester"]
