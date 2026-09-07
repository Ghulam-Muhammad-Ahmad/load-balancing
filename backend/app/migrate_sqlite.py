"""One-time, transactional import of the preserved legacy SQLite volume."""
import os
import sqlite3
import shutil
import tempfile
from pathlib import Path

import psycopg


def migrate(database_url):
    source = Path(os.getenv("LEGACY_SQLITE_PATH", "/legacy/links.db"))
    if not source.is_file():
        return
    with psycopg.connect(database_url) as target:
        target.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)")
        target.execute("SELECT pg_advisory_xact_lock(782104)")
        if target.execute("SELECT 1 FROM migrations WHERE name = 'sqlite_import'").fetchone():
            return
        if target.execute("SELECT EXISTS(SELECT 1 FROM links) OR EXISTS(SELECT 1 FROM clicks)").fetchone()[0]:
            raise RuntimeError("Refusing to merge legacy SQLite into populated PostgreSQL tables")
        # A stopped SQLite WAL database needs writable scratch files for recovery.
        scratch = tempfile.TemporaryDirectory()
        snapshot = Path(scratch.name) / "links.db"
        shutil.copy2(source, snapshot)
        for suffix in ("-wal", "-shm"):
            companion = Path(str(source) + suffix)
            if companion.exists():
                shutil.copy2(companion, Path(str(snapshot) + suffix))
        legacy = sqlite3.connect(snapshot)
        try:
            with target.cursor() as cursor:
                for table, columns in (
                    ("links", "id, code, target_url, created_at"),
                    ("clicks", "id, link_id, clicked_at, referrer, user_agent"),
                ):
                    rows = legacy.execute(f"SELECT {columns} FROM {table}")
                    placeholders = ", ".join(["%s"] * len(columns.split(",")))
                    while batch := rows.fetchmany(1000):
                        cursor.executemany(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", batch)
                    cursor.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table}")
            target.execute("INSERT INTO migrations (name) VALUES ('sqlite_import')")
        finally:
            legacy.close()
            scratch.cleanup()
