"""Verify reuse, rollback, and bounded checkout against the configured PostgreSQL."""
from psycopg_pool import ConnectionPool, PoolTimeout
from .main import DATABASE_URL


def main():
    with ConnectionPool(DATABASE_URL, min_size=1, max_size=1, timeout=.1) as pool:
        pool.wait()
        with pool.connection() as conn:
            pid = conn.info.backend_pid
            conn.execute("CREATE TEMP TABLE pool_check (value INTEGER)")
            conn.execute("INSERT INTO pool_check VALUES (1)")
            try:
                with pool.connection():
                    raise AssertionError("Pool limit was not enforced")
            except PoolTimeout:
                pass
        try:
            with pool.connection() as conn:
                assert conn.info.backend_pid == pid
                conn.execute("INSERT INTO pool_check VALUES (2)")
                raise ValueError("force rollback")
        except ValueError:
            pass
        with pool.connection() as conn:
            assert conn.info.backend_pid == pid
            assert conn.execute("SELECT COUNT(*) FROM pool_check").fetchone()[0] == 1
        print("PASS: connection reuse, transaction commit/rollback, pool bound, checkout timeout, recovery")


if __name__ == "__main__":
    main()
