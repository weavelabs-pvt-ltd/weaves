from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from infrastructure.auth.throttle import (
    InMemoryLoginRateLimiter,
    PostgresLoginRateLimiter,
    login_bucket_key,
)


class RecordingConnection:
    def __init__(self, row):
        self.row = row
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append((" ".join(query.split()), params))
        return self

    def fetchone(self):
        return self.row


class RecordingPool:
    def __init__(self, connection):
        self.connection_value = connection

    @contextmanager
    def connection(self):
        yield self.connection_value


def test_in_memory_login_limiter_expires_and_clears_buckets():
    limiter = InMemoryLoginRateLimiter(limit=2, window_seconds=60)
    now = datetime(2026, 10, 6, tzinfo=UTC)

    assert limiter.consume("account", now).allowed
    assert limiter.consume("account", now).allowed
    denied = limiter.consume("account", now)

    assert not denied.allowed
    assert denied.retry_after_seconds == 60
    for _ in range(100):
        limiter.consume("account", now)
    assert limiter._buckets["account"][1] == 3
    assert limiter.consume("account", now + timedelta(seconds=61)).allowed
    limiter.clear("account")
    assert limiter.consume("account", now + timedelta(seconds=61)).allowed


def test_login_bucket_keys_do_not_store_the_submitted_identifier():
    email = "Owner@Example.com"

    bucket_key = login_bucket_key("account", email)

    assert len(bucket_key) == 64
    assert email.casefold() not in bucket_key
    assert bucket_key == login_bucket_key("account", "owner@example.com")
    assert bucket_key != login_bucket_key("source", email)


def test_postgres_login_limiter_uses_atomic_upsert_and_returns_retry_window():
    now = datetime(2026, 10, 6, tzinfo=UTC)
    connection = RecordingConnection((6, now))
    limiter = PostgresLoginRateLimiter(
        RecordingPool(connection), limit=5, window_seconds=900
    )

    decision = limiter.consume("a" * 64, now)

    statement, params = connection.statements[0]
    assert "INSERT INTO product_login_throttle" in statement
    assert "ON CONFLICT (bucket_key) DO UPDATE SET" in statement
    assert "RETURNING attempts, window_started_at" in statement
    assert params == (
        "a" * 64,
        now,
        now,
        now - timedelta(seconds=900),
        now - timedelta(seconds=900),
        6,
    )
    assert not decision.allowed
    assert decision.retry_after_seconds == 900


def test_postgres_login_limiter_clears_successful_login_bucket():
    connection = RecordingConnection(None)
    limiter = PostgresLoginRateLimiter(RecordingPool(connection))

    limiter.clear("b" * 64)

    statement, params = connection.statements[0]
    assert statement == "DELETE FROM product_login_throttle WHERE bucket_key = %s"
    assert params == ("b" * 64,)
