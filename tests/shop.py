"""Race free endpoints that once fooled deadheat. Every order must pass."""

import asyncio
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class User(BaseModel):
    user: str


async def poll(pool):
    """SQL the app sends on its own, with no request in flight."""
    while True:
        await pool.fetchval("SELECT count(*) FROM visits")
        await asyncio.sleep(0.3)


@asynccontextmanager
async def lifespan(app):
    app.state.pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=2)
    poller = asyncio.create_task(poll(app.state.pool))
    yield
    poller.cancel()
    await app.state.pool.close()


app = FastAPI(lifespan=lifespan)
lock = asyncio.Lock()


@app.post("/signup")
async def signup(body: User):
    """Ids come from two sequences, so which user gets which id depends on timing."""
    async with app.state.pool.acquire() as conn, conn.transaction():
        uid = await conn.fetchval("INSERT INTO users (name) VALUES ($1) RETURNING id", body.user)
        aid = await conn.fetchval("INSERT INTO accounts (name) VALUES ($1) RETURNING id", body.user)
        await conn.execute("INSERT INTO memberships (user_id, account_id) VALUES ($1, $2)", uid, aid)
    return {}


@app.post("/book")
async def book(body: User):
    """A locked booking. The loser joins a waitlist stamped by clock_timestamp()."""
    async with app.state.pool.acquire() as conn, conn.transaction():
        seats = await conn.fetchval("SELECT seats_left FROM shows WHERE id = 1 FOR UPDATE")
        if not seats:
            await conn.execute("INSERT INTO waitlist (user_name) VALUES ($1)", body.user)
            return JSONResponse({}, status_code=409)
        await conn.execute("UPDATE shows SET seats_left = seats_left - 1 WHERE id = 1")
    return {}


@app.post("/visit")
async def visit(body: User, tasks: BackgroundTasks):
    """Writes from a background task, after the response is sent."""
    async with app.state.pool.acquire() as conn:
        await conn.execute("SELECT 1 FROM counters WHERE id = 1")
    tasks.add_task(log_visit, body.user)
    return {}


async def log_visit(user):
    await asyncio.sleep(0.02)
    async with app.state.pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE counters SET n = n + 1 WHERE id = 1")
        await conn.execute("INSERT INTO visits (user_name) VALUES ($1)", user)


@app.post("/take")
async def take(body: User):
    """Serialized by a lock inside the app, which Postgres cannot see."""
    async with lock, app.state.pool.acquire() as conn:
        n = await conn.fetchval("SELECT n FROM counters WHERE id = 1")
        await conn.execute("UPDATE counters SET n = $1 WHERE id = 1", n + 1)
    return {}
