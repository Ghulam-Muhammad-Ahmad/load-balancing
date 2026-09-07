import asyncio
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

# Requests travel the same path a browser takes: nginx on port 80, then FastAPI.
TARGET_BASE_URL = os.getenv("TARGET_BASE_URL", "http://frontend").rstrip("/")
EXPECTED_STATUS = 307
REQUEST_TIMEOUT = 10.0
# How long after the window closes we keep waiting for replies before calling them lost.
# Bounds the run: nginx proxies this endpoint and must not time out first.
DRAIN_GRACE = 10.0
# A browser sends these; the backend reads referer and user-agent into every click row.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_WORKERS = max(1, min(6, (os.cpu_count() or 2) - 2))

app = FastAPI(title="Short Circuit Load Generator", version="2.0.0")
test_lock = asyncio.Lock()


class LoadTestRequest(BaseModel):
    code: str
    duration_seconds: int = 5
    target_rps: int = 100

    @field_validator("code")
    @classmethod
    def valid_code(cls, value: str):
        if not 3 <= len(value) <= 24 or not all(char.isalnum() or char in "-_" for char in value):
            raise ValueError("Invalid short-link code")
        return value

    @field_validator("duration_seconds")
    @classmethod
    def valid_duration(cls, value: int):
        if not 1 <= value <= 15:
            raise ValueError("Duration must be between 1 and 15 seconds")
        return value

    @field_validator("target_rps")
    @classmethod
    def valid_target_rps(cls, value: int):
        if not 1 <= value <= 2000:
            raise ValueError("Target rate must be between 1 and 2000 requests/second")
        return value


class Connections:
    """Keep-alive HTTP/1.1 connections over raw asyncio streams, one request in flight
    each. An HTTP client library costs about ten times more CPU per request, which made
    the generator the bottleneck instead of the application."""

    def __init__(self, host: str, port: int, request: bytes):
        self.host, self.port, self.request = host, port, request
        self.free: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

    async def get(self) -> int:
        """Returns the HTTP status, or 0 if the exchange failed."""
        stream = self.free.pop() if self.free else None
        try:
            if stream is None:
                stream = await asyncio.open_connection(self.host, self.port)
            reader, writer = stream
            writer.write(self.request)
            await writer.drain()

            status_line = await reader.readuntil(b"\r\n")
            status = int(status_line.split(b" ")[1])
            length, close = 0, False
            while True:
                line = await reader.readuntil(b"\r\n")
                if line == b"\r\n":
                    break
                name, _, value = line.decode("latin-1").partition(":")
                name = name.strip().lower()
                if name == "content-length":
                    length = int(value.strip())
                elif name == "connection" and "close" in value.strip().lower():
                    close = True
                elif name == "transfer-encoding":
                    # Never expected from this endpoint; reusing the socket would desync it.
                    close = True
            if length:
                await reader.readexactly(length)
            if close:
                writer.close()
            else:
                self.free.append(stream)
            return status
        except asyncio.CancelledError:
            # A timed-out request leaves the socket mid-response; reusing it would desync.
            self._discard(stream)
            raise
        except Exception:
            self._discard(stream)
            return 0

    @staticmethod
    def _discard(stream):
        if stream is not None:
            try:
                stream[1].close()
            except Exception:
                pass

    def close(self):
        for _, writer in self.free:
            try:
                writer.close()
            except Exception:
                pass
        self.free.clear()


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 2)


def _worker(target: str, start_wall: float, rps: float, window: float, count: int, offset: int, stride: int) -> dict:
    """One generator process. Fires on a fixed arrival schedule regardless of how the
    server is coping, so a slow server shows up as latency instead of as fewer requests.
    All returned times are seconds from the start of the measurement window."""

    url = urlsplit(target)
    request = (
        f"GET {url.path or '/'} HTTP/1.1\r\nHost: {url.netloc}\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in BROWSER_HEADERS.items())
        + "\r\n"
    ).encode("latin-1")

    async def run():
        samples: list[tuple[float, float, float, int]] = []
        connections = Connections(url.hostname, url.port or 80, request)

        async def one(due: float):
            sent = time.perf_counter() - base
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    status = await connections.get()
            except TimeoutError:
                status = 0
            samples.append((due, sent, time.perf_counter() - base, status))

        gap = start_wall - time.time()
        if gap > 0:
            await asyncio.sleep(gap)
        base = time.perf_counter()
        cpu_started = time.process_time()
        tasks = []
        for index in range(count):
            due = (index * stride + offset) / rps
            sleep_for = due - (time.perf_counter() - base)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            tasks.append(asyncio.create_task(one(due)))
        # CPU cost of offering the load, measured over the offering window only. Including
        # the idle drain below would average the number down and hide a saturated generator.
        offered_for = max(time.perf_counter() - base, 1e-9)
        cpu_fraction = (time.process_time() - cpu_started) / offered_for
        # Hard stop so an overloaded server cannot stretch the run without bound.
        # Whatever is still in flight after the grace period is an unanswered request.
        _, pending = await asyncio.wait(tasks, timeout=max(window + DRAIN_GRACE - offered_for, 0.1))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        connections.close()
        return samples, len(pending), cpu_fraction

    samples, abandoned, cpu_fraction = asyncio.run(run())
    statuses: dict[int, int] = {}
    for _, _, _, status in samples:
        statuses[status] = statuses.get(status, 0) + 1
    return {
        # Latency measured from the scheduled arrival, not from send. This is what a user
        # waiting in the queue actually experiences, and what closed-loop tests hide.
        "arrival_latency": [(finished - due) * 1000 for due, _, finished, _ in samples],
        "service_latency": [(finished - sent) * 1000 for _, sent, finished, _ in samples],
        # How late this process was in firing. If it grows, the generator is the bottleneck.
        "schedule_lag": [(sent - due) * 1000 for due, sent, _, _ in samples],
        # Replies that landed after the window closed are backlog, not throughput.
        "in_window": sum(1 for _, _, finished, status in samples if status == EXPECTED_STATUS and finished <= window),
        "late": sum(1 for _, _, finished, status in samples if status == EXPECTED_STATUS and finished > window),
        "abandoned": abandoned,
        # Near 1.0 means this process ran out of CPU, so its numbers describe the
        # generator rather than the application.
        "cpu_fraction": cpu_fraction,
        "statuses": statuses,
    }


@app.get("/health")
def health():
    return {"status": "ok", "role": "load-generator", "target": TARGET_BASE_URL, "max_workers": MAX_WORKERS}


@app.post("/api/load-test")
async def run_load_test(payload: LoadTestRequest):
    if test_lock.locked():
        raise HTTPException(409, "A load test is already running")

    async with test_lock:
        target = f"{TARGET_BASE_URL}/{payload.code}"
        planned = payload.target_rps * payload.duration_seconds
        workers = max(1, min(MAX_WORKERS, planned))
        start_wall = time.time() + 0.5  # let every process spin up before any of them fires

        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        with ProcessPoolExecutor(max_workers=workers) as pool:
            parts = await asyncio.gather(*(
                loop.run_in_executor(
                    pool, _worker, target, start_wall, float(payload.target_rps),
                    float(payload.duration_seconds), len(range(offset, planned, workers)),
                    offset, workers,
                )
                for offset in range(workers)
            ))
        wall = time.perf_counter() - started

        arrival = [value for part in parts for value in part["arrival_latency"]]
        service = [value for part in parts for value in part["service_latency"]]
        lag = [value for part in parts for value in part["schedule_lag"]]
        statuses: dict[int, int] = {}
        for part in parts:
            for status, hits in part["statuses"].items():
                statuses[status] = statuses.get(status, 0) + hits

        answered = len(arrival)
        abandoned = sum(part["abandoned"] for part in parts)
        in_window = sum(part["in_window"] for part in parts)
        late = sum(part["late"] for part in parts)
        failures = planned - in_window
        # Offered load is fixed by the target rate, so throughput is only what was
        # actually delivered inside the window. Replies that drained afterwards are
        # backlog and do not count, or an overloaded server would look like it kept up.
        achieved = round(in_window / payload.duration_seconds, 2)
        lag_p95 = percentile(lag, .95)
        cpu_peak = round(max(part["cpu_fraction"] for part in parts), 2)
        bad_status = any(status != EXPECTED_STATUS for status in statuses)

        return {
            "rps": achieved,
            "target_rps": payload.target_rps,
            "requests_planned": planned,
            "latency_ms": {"p50": percentile(arrival, .50), "p95": percentile(arrival, .95), "p99": percentile(arrival, .99)},
            "service_latency_ms": {"p50": percentile(service, .50), "p95": percentile(service, .95), "p99": percentile(service, .99)},
            "error_rate": round(failures / planned * 100, 2) if planned else 0,
            "requests": planned,
            "errors": failures,
            "answered": answered,
            # Served correctly, but after the window closed. Counted against the app.
            "late": late,
            # Still unanswered when the drain grace expired.
            "abandoned": abandoned,
            "duration_seconds": payload.duration_seconds,
            # Wall clock for the whole run. Longer than the window means late replies drained after it.
            "wall_seconds": round(wall, 2),
            "expected_status": EXPECTED_STATUS,
            "status_counts": statuses,
            "generator": {
                "processes": workers,
                "schedule_lag_ms_p95": lag_p95,
                "cpu_fraction": cpu_peak,
                # Schedule lag alone is ambiguous: an overloaded server also congests the
                # generator's event loop. Only a CPU-bound process proves the generator
                # itself is the limit, and then the numbers describe it, not the app.
                "cpu_bound": cpu_peak > 0.9,
                "kept_schedule": lag_p95 < 50,
            },
            "path": f"nginx -> backend, via {target}",
            # Stated rather than silently assumed: a browser would follow the 307 to the
            # destination host, which would measure the public internet, not this app.
            "redirects_followed": False,
            # True only if 99% of the offered rate was delivered inside the window and
            # every answered request returned the expected redirect.
            "sustained_target": achieved >= payload.target_rps * 0.99 and not bad_status,
        }
