# Short Circuit

A personal URL shortener built with FastAPI, React, PostgreSQL, and Docker. It includes custom aliases, persistent link history, redirect tracking, a seven-day click overview, and an isolated load generator.

## Run with Docker

```bash
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080). PostgreSQL data is stored in the `postgres_data` Docker volume. Every redirect performs a SELECT and INSERT in a committed transaction, including load tests.

Existing SQLite history is imported once at startup into empty PostgreSQL tables. The old `shortener_data` volume remains mounted read-only as a backup; subsequent starts skip the completed import. PostgreSQL is available within the Docker network only. The default password is for local development; set `POSTGRES_PASSWORD` before initializing a new database to override it.

The redirect path caches each code's link id and target URL in the handling worker's memory. A link's code and target never change after creation, so the entry never needs invalidating; a code created by another worker simply misses once. Unknown codes are not cached, so the cache is bounded by the number of real links. The click insert is not cached and still commits before every redirect.

Inspect persisted clicks with `docker compose exec postgres psql -U shortener -d shortener -c "SELECT * FROM clicks ORDER BY id DESC LIMIT 20;"`.

## Development

The backend runs eight Uvicorn worker processes so requests can execute across CPU cores. The operating system distributes accepted connections among workers behind the single `backend:8000` service endpoint.

Request handlers are `async def` on psycopg's `AsyncConnectionPool`. The synchronous equivalent ran every handler on a worker thread, and that dispatch plus GIL contention cost more CPU than the queries did: converting raised the sustained ceiling from roughly 550 to roughly 890 requests/second on the same machine.

Each worker reuses its own psycopg connection pool (2 minimum, 20 maximum), giving the backend an aggregate minimum of 16 and maximum of 160 pooled PostgreSQL connections. PostgreSQL is started with `max_connections=200` and `shared_buffers=512MB` to accommodate them.
`DB_POOL_TIMEOUT=3` limits checkout waiting to three seconds; up to 100 callers may queue.
Pool exhaustion returns HTTP 503. Transactions still commit before redirect responses,
and failures roll back before returning the connection. `/api/health` includes pool statistics.
Startup migration uses a separate short-lived connection. Pool connections close on shutdown.

Pressure tests run in the separate `loadgen` container on port 9000 inside the Docker network. Nginx routes only `POST /api/load-test` to it. Only one test can run at a time.

The generator is built so its numbers cannot flatter the application:

- **Same path as a real visitor.** It requests `http://frontend/{code}`, so traffic passes through Nginx and then FastAPI, carrying browser `User-Agent` and `Accept` headers. It does not shortcut to `backend:8000`.
- **Fixed arrival rate, not fixed concurrency.** You set `target_rps`; requests fire on a schedule that does not slow down when the server does. A closed-loop test that waits for each reply cannot distinguish "fast" from "not asked for much".
- **Latency measured from scheduled arrival.** Queue wait is included, so a backlog raises the reported latency instead of disappearing from it.
- **Backlog is not throughput.** `rps` counts only replies delivered inside the measurement window. Replies that drain afterwards are reported as `late`, and requests still unanswered after a ten-second grace period as `abandoned`. Both count against `error_rate`.
- **The generator reports on itself.** Six worker processes spread the schedule across cores, and `generator.cpu_fraction` plus `generator.cpu_bound` say when the generator ran out of CPU, so a generator limit is never read as an application limit.
- **Redirects are not followed**, and `redirects_followed: false` states it in every result. Following the 307 would measure the destination host rather than this app.

`sustained_target` is true only when at least 99% of the offered rate was delivered inside the window and every answered request returned HTTP 307.

Measured on a 12-core development machine with the load generator running beside the
application and an empty `clicks` table:

| Offered | Sustained | p50 | p95 | p99 |
| --- | --- | --- | --- | --- |
| 500 rps | 499 | 15 ms | 50 ms | 73 ms |
| 800 rps | 792 | 94 ms | 333 ms | 473 ms |
| 1000 rps | 894 | 489 ms | 1551 ms | 2001 ms |

The knee is near 800 rps. Above it the backend is CPU-bound, not database-bound: at 1000
rps the backend container uses about 6.7 cores while PostgreSQL uses 1.8. The generator
needs another 0.15, so generating and serving on one machine is itself a constraint.
Every redirect still performs a database read and a committed insert, load-test traffic
included.

Backend:

```bash
cd backend
pip install -r requirements.txt
# PowerShell, with a PostgreSQL server reachable locally:
$env:DATABASE_URL="postgresql://shortener:shortener_local@localhost:5432/shortener"
uvicorn app.main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

The production Nginx configuration proxies API requests and short-link redirects to FastAPI, keeping the app on one public origin.
