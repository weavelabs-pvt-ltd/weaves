"""Typed pool settings read once for an API or worker process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PoolSettings:
    """Connection limits for one process-scoped HTTP client and database pool."""

    database_url: str
    db_pool_min_size: int
    db_pool_max_size: int
    db_application_name: str
    http_timeout_seconds: float
    http_max_connections: int
    http_max_keepalive_connections: int


def load_pool_settings(*, process: Literal["api", "worker", "cli"]) -> PoolSettings:
    """Load pool settings for one API, worker, or CLI process.

    Defaults match the previous implicit clients: a 5 second HTTP timeout,
    100 connections with 20 kept alive, and a database pool of 1 to 10.
    """
    if process not in {"api", "worker", "cli"}:
        raise ValueError("process must be 'api', 'worker', or 'cli'")
    min_size = _positive_int("WEAVES_DB_POOL_MIN_SIZE", 1)
    max_size = _positive_int("WEAVES_DB_POOL_MAX_SIZE", 10)
    if min_size > max_size:
        raise ValueError(
            "WEAVES_DB_POOL_MIN_SIZE cannot exceed WEAVES_DB_POOL_MAX_SIZE"
        )
    max_connections = _positive_int("WEAVES_HTTP_MAX_CONNECTIONS", 100)
    keepalive = _positive_int("WEAVES_HTTP_MAX_KEEPALIVE_CONNECTIONS", 20)
    if keepalive > max_connections:
        raise ValueError(
            "WEAVES_HTTP_MAX_KEEPALIVE_CONNECTIONS cannot exceed "
            "WEAVES_HTTP_MAX_CONNECTIONS"
        )
    default_name = {
        "api": "weaves-product-api",
        "worker": "weaves-product-worker",
        "cli": "weaves-product-cli",
    }[process]
    return PoolSettings(
        database_url=os.environ.get("DATABASE_URL", "").strip(),
        db_pool_min_size=min_size,
        db_pool_max_size=max_size,
        db_application_name=os.environ.get("WEAVES_DB_APPLICATION_NAME", "").strip()
        or default_name,
        http_timeout_seconds=_positive_float("WEAVES_HTTP_TIMEOUT_SECONDS", 5.0),
        http_max_connections=max_connections,
        http_max_keepalive_connections=keepalive,
    )


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value
