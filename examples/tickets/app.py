"""A tiny ticket shop with the race most booking code has, and two fixes."""

import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


class Booking(BaseModel):
    user: str


@asynccontextmanager
async def lifespan(app):
    app.state.pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=2)
    yield
    await app.state.pool.close()


app = FastAPI(lifespan=lifespan)


async def check_then_book(show_id, user, lock):
    read = "SELECT seats_left FROM shows WHERE id = $1" + (" FOR UPDATE" if lock else "")
    async with app.state.pool.acquire() as conn, conn.transaction():
        seats = await conn.fetchval(read, show_id)
        if not seats:
            raise HTTPException(409, "sold out")
        await conn.execute("UPDATE shows SET seats_left = $1 WHERE id = $2", seats - 1, show_id)
        await conn.execute("INSERT INTO bookings (show_id, user_name) VALUES ($1, $2)", show_id, user)
    return {"booked": user}


@app.post("/shows/{show_id}/book")
async def book(show_id: int, booking: Booking):
    """Reads the seats, checks, writes. Inside a transaction. Every test passes."""
    return await check_then_book(show_id, booking.user, lock=False)


@app.post("/shows/{show_id}/book-locked")
async def book_locked(show_id: int, booking: Booking):
    """FOR UPDATE makes a second buyer wait until the first one commits."""
    return await check_then_book(show_id, booking.user, lock=True)


@app.post("/shows/{show_id}/book-atomic")
async def book_atomic(show_id: int, booking: Booking):
    """Checks and writes in one statement, so there is no gap to race in."""
    async with app.state.pool.acquire() as conn, conn.transaction():
        left = await conn.fetchval(
            "UPDATE shows SET seats_left = seats_left - 1 WHERE id = $1 AND seats_left > 0 RETURNING seats_left",
            show_id,
        )
        if left is None:
            raise HTTPException(409, "sold out")
        await conn.execute("INSERT INTO bookings (show_id, user_name) VALUES ($1, $2)", show_id, booking.user)
    return {"booked": booking.user}
