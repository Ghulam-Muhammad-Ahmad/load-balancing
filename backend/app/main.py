import os
import secrets
import string
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from psycopg_pool import AsyncConnectionPool, PoolTimeout, TooManyRequests
from psycopg.rows import dict_row

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl, field_validator

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shortener:shortener_local@localhost:5432/shortener")
ALPHABET = string.ascii_letters + string.digits
# code -> (link id, target url) for the redirect hot path. See follow_link.
LINK_CACHE: dict[str, tuple[int, str]] = {}
# Async pool with async handlers: the sync equivalent ran every request on a worker
# thread, and that dispatch plus GIL contention cost more CPU than the query itself.
pool = AsyncConnectionPool(
    DATABASE_URL,
    min_size=int(os.getenv("DB_POOL_MIN_SIZE", "5")),
    max_size=int(os.getenv("DB_POOL_MAX_SIZE", "20")),
    timeout=float(os.getenv("DB_POOL_TIMEOUT", "3")),
    max_waiting=100,
    open=False,
    kwargs={"row_factory": dict_row, "connect_timeout": 10, "options": "-c timezone=UTC", "application_name": "short-circuit-pool"},
)

app = FastAPI(title="Short Circuit API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:8080").split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


class LinkCreate(BaseModel):
    url: HttpUrl
    alias: str | None = None

    @field_validator("alias")
    @classmethod
    def valid_alias(cls, value: str | None):
        if value is None or value == "":
            return None
        if not 3 <= len(value) <= 24 or not all(c in ALPHABET + "-_" for c in value):
            raise ValueError("Alias must be 3–24 letters, numbers, hyphens, or underscores")
        return value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def database():
    # Commits on success, rolls back on failure, then returns the connection.
    async with pool.connection() as connection:
        yield connection


async def init_db():
    async with database() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS links (
                id BIGSERIAL PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                target_url TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clicks (
                id BIGSERIAL PRIMARY KEY,
                link_id BIGINT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
                clicked_at TIMESTAMPTZ NOT NULL,
                referrer TEXT,
                user_agent TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_clicks_link_id ON clicks(link_id);
        """)


@app.on_event("startup")
async def startup():
    await pool.open()
    try:
        await pool.wait(timeout=30)
        await init_db()
        # Startup only, on its own short-lived connection, so it stays synchronous.
        from .migrate_sqlite import migrate
        migrate(DATABASE_URL)
    except Exception:
        await pool.close()
        raise


@app.on_event("shutdown")
async def shutdown():
    await pool.close()


@app.exception_handler(PoolTimeout)
@app.exception_handler(TooManyRequests)
async def pool_busy(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Database busy; retry shortly"}, headers={"Retry-After": "1"})


def serialize_link(row: dict, request: Request) -> dict:
    return {
        "id": row["id"],
        "code": row["code"],
        "target_url": row["target_url"],
        "created_at": row["created_at"],
        "clicks": row["clicks"],
        "short_url": str(request.base_url).rstrip("/") + "/" + row["code"],
    }


@app.get("/api/health")
async def health():
    async with database() as db:
        await db.execute("SELECT 1")
    return {"status": "ok", "database": "postgresql", "worker_pid": os.getpid(), "pool": pool.get_stats()}


@app.post("/api/links", status_code=201)
async def create_link(payload: LinkCreate, request: Request):
    code = payload.alias
    async with database() as db:
        if code:
            if await (await db.execute("SELECT 1 FROM links WHERE code = %s", (code,))).fetchone():
                raise HTTPException(409, "That custom alias is already in use")
        else:
            for _ in range(8):
                candidate = "".join(secrets.choice(ALPHABET) for _ in range(7))
                if not await (await db.execute("SELECT 1 FROM links WHERE code = %s", (candidate,))).fetchone():
                    code = candidate
                    break
            if not code:
                raise HTTPException(503, "Could not allocate a short code")

        row = await (await db.execute(
            "INSERT INTO links (code, target_url, created_at) VALUES (%s, %s, %s) RETURNING *, 0 AS clicks",
            (code, str(payload.url), now_iso()),
        )).fetchone()
    return serialize_link(row, request)


@app.get("/api/links")
async def list_links(request: Request):
    async with database() as db:
        # Aggregating clicks first keeps this on an index-only scan of idx_clicks_link_id.
        # Joining before grouping made it a heap scan of every click row, and the page
        # polls this once a second. A counter column on links would be O(1) to read but
        # would lock the same link row on every redirect, so the scan is the better trade.
        rows = await (await db.execute("""
            SELECT links.*, coalesce(counted.clicks, 0) AS clicks
            FROM links
            LEFT JOIN (SELECT link_id, COUNT(*) AS clicks FROM clicks GROUP BY link_id) counted
              ON counted.link_id = links.id
            ORDER BY links.created_at DESC
        """)).fetchall()
    return [serialize_link(row, request) for row in rows]


@app.get("/api/stats")
async def stats():
    async with database() as db:
        totals = await (await db.execute("""
            SELECT (SELECT COUNT(*) FROM links) AS links,
                   (SELECT COUNT(*) FROM clicks) AS clicks
        """)).fetchone()
        daily = await (await db.execute("""
            WITH dates AS (
              SELECT CURRENT_DATE - n AS day FROM generate_series(0, 6) AS n
            )
            SELECT dates.day, COUNT(clicks.id) AS clicks
            FROM dates LEFT JOIN clicks ON date(clicks.clicked_at) = dates.day
            GROUP BY dates.day ORDER BY dates.day
        """)).fetchall()
    return {"links": totals["links"], "clicks": totals["clicks"], "daily": [dict(row) for row in daily]}


@app.get("/{code}")
async def follow_link(code: str, request: Request):
    # A link's code and target never change once created, so the lookup is cacheable and
    # never needs invalidating. Each worker fills its own copy on first use; a code created
    # by a different worker simply misses once. Misses are not cached, so an unknown code
    # cannot grow this: it is bounded by the number of real links.
    # ponytail: plain dict, per worker. Reach for a shared cache only if link count stops
    # fitting comfortably in each worker's memory.
    cached = LINK_CACHE.get(code)
    async with database() as db:
        if cached is None:
            link = await (await db.execute("SELECT id, target_url FROM links WHERE code = %s", (code,))).fetchone()
            if not link:
                raise HTTPException(404, "Short link not found")
            cached = LINK_CACHE[code] = (link["id"], link["target_url"])
        # Commit one click row before returning every successful redirect. The database
        # supplies the timestamp, which saves formatting and parsing one string per click.
        await db.execute(
            "INSERT INTO clicks (link_id, clicked_at, referrer, user_agent) VALUES (%s, now(), %s, %s)",
            (cached[0], request.headers.get("referer"), request.headers.get("user-agent")),
        )
    return RedirectResponse(cached[1], status_code=307)
