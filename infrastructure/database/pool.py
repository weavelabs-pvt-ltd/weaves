"""Process-scoped PostgreSQL connection pool."""

from __future__ import annotations

from typing import Any

from shared.metrics import DB_POOL_IN_USE


def open_postgres_pool(
    database_url: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    application_name: str = "weaves-product-api",
) -> Any:
    """Open one pool. Repositories borrow connections; they do not create pools."""
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"application_name": application_name},
    )
    pool.open(wait=True)
    DB_POOL_IN_USE.set_callback(lambda: _in_use(pool))
    return pool


def _in_use(pool: Any) -> float:
    """Connections checked out right now (open connections minus idle ones)."""
    stats = pool.get_stats()
    return float(stats["pool_size"] - stats["pool_available"])
