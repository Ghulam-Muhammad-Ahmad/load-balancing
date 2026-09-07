# Short Circuit

A personal URL shortener built with FastAPI, React, PostgreSQL, and Docker. It includes custom aliases, persistent link history, redirect tracking, a seven-day click overview, and an isolated load generator.

It sustains **2,200 redirects per second** on a 12-core development machine, each one recording a click, with a p99 of 353 ms and no failed requests. That is up from 103 rps when the work started. [How it got there](#performance-what-actually-moved-the-needle).

## Run with Docker

```bash
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080). PostgreSQL data is stored in the `postgres_data` Docker volume. Every redirect records a click, including load-test traffic. Clicks are written in batches rather than one transaction per redirect -- see [Click batching](#click-batching) for the durability trade and how to turn it off.

Existing SQLite history is imported once at startup into empty PostgreSQL tables. The old `shortener_data` volume remains mounted read-only as a backup; subsequent starts skip the completed import. PostgreSQL is available within the Docker network only. The default password is for local development; set `POSTGRES_PASSWORD` before initializing a new database to override it.

The redirect path caches each code's link id and target URL in the handling worker's memory. A link's code and target
never change after creation, so the entry never needs invalidating; a code created by another worker simply misses once.
Unknown codes are not cached, so the cache is bounded by the number of real links.

### Click batching

Clicks are appended to an in-memory buffer and written by a background task, either every `CLICK_FLUSH_SECONDS`
(default 0.2) or as soon as `CLICK_FLUSH_MAX_ROWS` (default 500) have accumulated, whichever comes first. Combined with
the link cache, **a redirect that hits the cache touches the database zero times.** Committing one transaction per
redirect was the single largest remaining cost in the request path; removing it took the ceiling from about 800 rps to
about 2,200.

The trade, stated plainly: **a worker killed between flushes loses up to 0.2 s of clicks.** Nothing else is buffered --
link creation still commits synchronously, so no link is ever lost. Flushes are transactional and drain on graceful
shutdown, and `verify_postgres` asserts that 1,000 redirects produce exactly 1,000 rows.

- `CLICK_FLUSH_SECONDS=0` restores the old behaviour: one committed transaction per redirect, no buffering.
- Raise `CLICK_FLUSH_SECONDS` for fewer, larger writes and a proportionally larger loss window.
- `CLICK_BUFFER_MAX_ROWS` (default 100,000) bounds memory if PostgreSQL is unreachable. Beyond it the oldest clicks are
  dropped and counted rather than growing the buffer without limit.

`/api/health` reports `clicks.pending`, `written`, `batches`, `dropped`, and `failures`, so buffer behaviour is
observable rather than assumed. Analytics read from PostgreSQL, so counts can trail the buffer by one flush interval.

Inspect persisted clicks with `docker compose exec postgres psql -U shortener -d shortener -c "SELECT * FROM clicks ORDER BY id DESC LIMIT 20;"`.

## Development

The backend runs eight Uvicorn worker processes so requests can execute across CPU cores. The operating system distributes accepted connections among workers behind the single `backend:8000` service endpoint.

Request handlers are `async def` on psycopg's `AsyncConnectionPool`. The synchronous equivalent ran every handler on a worker thread, and that dispatch plus GIL contention cost more CPU than the queries did. One consequence to remember: a genuinely blocking call inside a handler now stalls its whole worker rather than one thread, so database and network work must stay awaited.

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

## Performance: what actually moved the needle

Measured on one 12-core development machine, generator and application sharing it, `clicks` table small.
Each row is a single change, measured on its own.

| # | Technique | Why it mattered | Effect |
| --- | --- | --- | --- |
| 0 | **Rebuilt the load generator** | The old one used an HTTP client library and a closed loop. It burned 8.7 cores to offer 1,000 rps, starving the app it measured, and reported comfortable numbers while doing it. Raw keep-alive sockets and a fixed arrival schedule replaced it. | 8.7 cores -> **0.15 cores** for the same rate. Nothing below was visible until this was fixed. |
| 1 | **Batched click inserts** | One commit per redirect was the largest cost left in the request path. Buffer in memory, flush every 200 ms with `executemany`. With the link cache, a cached redirect does no database work at all. | ~800 -> **~2,200 rps**. Pool checkouts per worker fell from thousands to 77. |
| 2 | **`async def` handlers on an async pool** | Synchronous `def` made FastAPI dispatch every request to a worker thread. Two handoffs per request, all contending on the GIL, costing more CPU than the queries. | ~550 -> **~890 rps** |
| 3 | **Disabled the Nginx access log on the redirect route** | One log line per redirect through Docker's json-file driver. Nginx was spending about **8.8 cores** writing logs -- more than serving traffic. Clicks are already in PostgreSQL. | ~550 -> ~650 rps |
| 4 | **Raised each pool from 8 to 20 connections** | 2,511 of 2,834 checkouts were queueing, average 251 ms, while PostgreSQL sat at 1.8 cores with spare capacity. A queue in front of an idle counter. | ~650 -> ~790 rps |
| 5 | **Per-worker link cache** | A code's target never changes, so the lookup is cacheable and never needs invalidating. Removed the `SELECT` from the redirect path. | p50 at 500 rps: 364 ms -> **69 ms** |
| 6 | **Nginx keep-alive upstream, 8 workers** | Stopped opening a fresh TCP connection to the backend per request; more processes to spread across cores. | Incremental |
| 7 | **Aggregate before joining in `/api/links`** | The page polls it every second, and it was heap-scanning every click row. Grouping clicks first keeps it on an index-only scan. | 9.6 ms -> **4.5 ms** at 23k clicks |

One thing that did **not** work: adding a keep-alive upstream pool *before* fixing the access log measured 20% slower,
because Nginx was CPU-saturated and the comparison was noise. It only helped once the real bottleneck was gone.

### Best results

| Offered | Sustained | Delivered | Failed | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- | --- |
| 100 rps | 100 | 100% | 0 | 5 ms | 22 ms | 37 ms |
| 800 rps | 799 | 100% | 0 | 6 ms | 42 ms | 72 ms |
| 1,000 rps | 999 | 100% | 0 | 8 ms | 63 ms | 109 ms |
| 1,500 rps | 1,493 | 100% | 0 | 17 ms | 72 ms | 118 ms |
| 2,000 rps | 1,997 | 100% | 0 | 44 ms | 244 ms | 406 ms |
| 2,200 rps | 2,189 | 99% | 0 | 51 ms | 215 ms | 353 ms |
| 2,500 rps | 2,478 | 99% | 0 | 136 ms | 1,355 ms | 1,849 ms |
| 3,000 rps | 2,617 | 87% | 0 | 368 ms | 6,970 ms | 8,219 ms |

**2,200 rps is the usable ceiling** -- the highest rate the application keeps up with while the generator still holds
its own schedule. Saturation is around 2,600 rps. Beyond that, offering more work produces no more completed work,
only deeper queues.

No request ever returned anything but HTTP 307 at any rate tested. The percentages below 100% are replies that arrived
*after* the measurement window closed, not failures.

The binding resource is CPU. At 2,000 rps the machine is oversubscribed: backend about 9.0 cores, generator 2.3,
Nginx 2.0, PostgreSQL 1.8 -- roughly 15 cores of demand on 12 physical cores. The generator alone costs over two,
so measuring on the same machine meaningfully lowers the ceiling; a client on separate hardware would record more.

For context, the first measurement of this system was **103 rps** with a p50 of 9.7 s and an 89% error rate. Most of
that gap was the measuring instrument, not the application.

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
