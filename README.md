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

## Running this on AWS: cost at 300-500 rps

An estimate, not a bill -- but built on measured numbers from this architecture rather than
rules of thumb. Prices are us-east-1 on-demand, ARM (Graviton), and were current at the time
of writing; check the [AWS Pricing Calculator](https://calculator.aws) before budgeting.

### The traffic math

| | 300 rps | 500 rps |
| --- | --- | --- |
| Requests/month | 778 million | **1.30 billion** |
| Click rows/month | 778 million | **1.30 billion** |
| Storage growth/month | 146 GiB | **243 GiB** |
| Accumulated after 12 months | 1.7 TiB | **2.8 TiB** |
| Egress/month, incl. TLS overhead | ~160 GiB | ~265 GiB |

Measured inputs: **200.9 bytes per click row** (heap plus index) and **169-byte** 307 responses.
Those rows have `referrer` NULL, as load-test traffic does; real browser traffic populates it, so
budget nearer **260 bytes/row** and add about 30% to the storage figures.

### Compute is nearly free

Measured CPU, all three services, at the rates in question:

| | 300 rps | 500 rps |
| --- | --- | --- |
| Backend, 8 workers | 0.55 cores | 0.66 cores |
| PostgreSQL | 0.44 cores | 0.57 cores |
| Nginx | 0.17 cores | 0.17 cores |
| **Total** | **1.16 cores** | **1.40 cores** |

Resident memory: backend 343 MB, PostgreSQL 158 MB, Nginx 11 MB. 500 rps fits comfortably on two
small instances. This is what the optimisation work bought -- before it, 500 rps was near the ceiling
of a 12-core machine.

### Monthly cost at 500 rps

Lean: 2x `t4g.small`, `db.t4g.medium` single-AZ.

| | Month 1 | Month 12 |
| --- | --- | --- |
| EC2, 2x t4g.small | $25 | $25 |
| ALB, base + ~2 LCU | $28 | $28 |
| RDS db.t4g.medium | $47 | $47 |
| **RDS storage** | **$28** | **$335** |
| Egress, 165 GiB billable | $15 | $15 |
| Cross-AZ transfer, CloudWatch | $18 | $18 |
| **Total** | **~$161** | **~$468** |

Production: 2x `c7g.large`, `db.m7g.large` Multi-AZ.

| | Month 1 | Month 12 |
| --- | --- | --- |
| EC2, 2x c7g.large | $106 | $106 |
| ALB | $28 | $28 |
| RDS Multi-AZ | $249 | $249 |
| **RDS storage, charged twice** | **$56** | **$670** |
| Egress and misc | $40 | $40 |
| **Total** | **~$479** | **~$1,093** |

At 300 rps, take roughly 40% off the storage lines: lean about $150 rising to $310, production about
$460 rising to $720.

### Storage of raw clicks is the entire cost curve

Compute is flat and cheap. Storage grows forever, and by month 12 it is 60-70% of the bill. Reserved
Instances or a Savings Plan cut compute by about 40%, which barely moves the total.

Two levers, both measured:

1. **`user_agent` is 112 of the 201 bytes.** Storing only `id`, `link_id` and `clicked_at` takes a row
   from 201 to **59.4 bytes** -- 3.4x less storage.
2. **Seven-day raw retention plus hourly rollups.** At 500 rps, seven days of raw clicks is a *steady*
   61 GiB instead of unbounded growth. Partition `clicks` by day, roll up to per-link-per-hour counts,
   and archive raw partitions to S3 Glacier Instant Retrieval at $0.004/GiB.

With retention the bill stops growing:

| | Month 1 | Month 12 |
| --- | --- | --- |
| Production + 7-day retention | ~$420 | **~$430** |
| ...and RDS downsized, since the table is now small | ~$270 | **~$280** |

About **$280/month flat against $1,093 and climbing**, for identical traffic.

### Three things to fix before this touches AWS

1. **`POST /api/load-test` is publicly routed.** `nginx.conf` proxies it straight to the load generator.
   Exposed to the internet, anyone can make your own infrastructure attack itself at up to 8000 rps.
   Do not deploy the `loadgen` container to production, or put it behind authentication on a private
   network. It exists to measure a local stack.
2. **`/api/links` will not survive.** The page polls it every second and it aggregates the whole
   `clicks` table. Fine at 320k rows (4.5 ms); at 1.3 billion it is an index-only scan of a billion
   entries every second, forever. Rollup tables fix this and the storage cost together.
3. **Buffered clicks versus instance termination.** At 500 rps, 0.2 s of buffer is about 100 clicks per
   worker. Graceful shutdown drains it, but a spot interruption or hard scale-in does not. Set the ALB
   deregistration delay above the flush interval, and do not run this on spot instances unless that
   loss is acceptable.

Not priced here: Fargate (broadly comparable, simpler operationally), Aurora Serverless v2 (cheaper at
low load, dearer at a sustained 500 rps), CloudFront in front of the ALB (would reduce egress cost and
offload TLS), Route 53, and NAT Gateway -- avoid the last one, as $32/month plus $0.045/GiB will quietly
exceed the compute bill.

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
