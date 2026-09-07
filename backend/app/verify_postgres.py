"""Run with docker compose exec backend python -m app.verify_postgres."""
import asyncio
import sqlite3
import tempfile
import shutil
import http.client
import json
from pathlib import Path
from .main import database
from .migrate_sqlite import migrate
from .main import DATABASE_URL


async def main():
    from .main import pool
    await pool.open(wait=True)
    async with database() as db:
        before = (await (await db.execute("SELECT COUNT(*) AS n FROM clicks")).fetchone())["n"]
        scratch = tempfile.TemporaryDirectory()
        for name in ("links.db", "links.db-wal", "links.db-shm"):
            if Path("/legacy", name).exists():
                shutil.copy2(Path("/legacy", name), Path(scratch.name, name))
        with sqlite3.connect(str(Path(scratch.name, "links.db"))) as old:
            for table in ("links", "clicks"):
                legacy_count = old.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                count = (await (await db.execute(f"SELECT COUNT(*) AS n FROM {table}")).fetchone())["n"]
                assert count >= legacy_count, (table, legacy_count, count)
                print(f"{table}: legacy={legacy_count}, postgres={count}")
    migrate(DATABASE_URL)
    async with database() as db:
        assert (await (await db.execute("SELECT COUNT(*) AS n FROM clicks")).fetchone())["n"] == before
    client = http.client.HTTPConnection("localhost", 8000, timeout=20)
    client.request("POST", "/api/links", json.dumps({"url": "https://example.com/postgres-check"}), {"Content-Type": "application/json"})
    created = client.getresponse()
    assert created.status == 201, created.read()
    code = json.loads(created.read())["code"]
    for _ in range(5):
        client.request("GET", f"/{code}")
        redirect = client.getresponse()
        assert redirect.status == 307
        assert redirect.getheader("location") == "https://example.com/postgres-check"
        redirect.read()
    async with database() as db:
        count = (await (await db.execute("SELECT COUNT(*) AS n FROM clicks JOIN links ON links.id = clicks.link_id WHERE code = %s", (code,))).fetchone())["n"]
    assert count == 5, count
    for path in ("/api/stats", "/api/links"):
        client.request("GET", path)
        response = client.getresponse()
        assert response.status == 200
        response.read()
    client.close()
    print("Verified: 5 redirects persisted; migration rerun is idempotent.")


if __name__ == "__main__":
    asyncio.run(main())
