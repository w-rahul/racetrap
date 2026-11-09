"""How often does firing two requests at the same instant catch the double booking?

This is what people try without racetrap. It starts the ticket example app
straight on the database, fires alice and bob together behind a barrier, and
counts how often the last seat gets sold twice.

    DATABASE_URL=postgresql://... uv run python benchmarks/burst.py 1000
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

APP = Path(__file__).parent.parent / "examples" / "tickets"
URL = "http://127.0.0.1:8765"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def book(user, barrier):
    req = urllib.request.Request(
        f"{URL}/shows/1/book", data=json.dumps({"user": user}).encode(),
        headers={"content-type": "application/json"}, method="POST",
    )
    barrier.wait()
    try:
        opener.open(req, timeout=10).close()
    except urllib.error.HTTPError:
        pass  # the loser gets 409, which is the correct answer


def main(trials):
    db = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    db.execute("DROP TABLE IF EXISTS bookings, shows")
    db.execute("CREATE TABLE shows (id int PRIMARY KEY, title text NOT NULL, seats_left int NOT NULL)")
    db.execute("CREATE TABLE bookings (id serial PRIMARY KEY, show_id int NOT NULL REFERENCES shows, user_name text NOT NULL)")
    app = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", "8765", "--log-level", "warning"], cwd=APP,
    )
    try:
        for _ in range(100):
            try:
                opener.open(URL, timeout=1).close()
                break
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(0.1)
        caught = 0
        for _ in range(trials):
            db.execute("TRUNCATE bookings, shows RESTART IDENTITY")
            db.execute("INSERT INTO shows VALUES (1, 'Dune', 1)")
            barrier = threading.Barrier(2)
            threads = [threading.Thread(target=book, args=(u, barrier)) for u in ("alice", "bob")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            caught += db.execute("SELECT count(*) FROM bookings").fetchone()[0] > 1
        print(f"{caught} of {trials} bursts caught the double booking ({caught / trials:.1%})")
    finally:
        app.terminate()
        app.wait()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000)
