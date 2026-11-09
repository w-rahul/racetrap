import os

import pytest

DSN = os.environ.get("DEADHEAT_TEST_DATABASE")

needs_db = pytest.mark.skipif(not DSN, reason="set DEADHEAT_TEST_DATABASE to a Postgres URL")
