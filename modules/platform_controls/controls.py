"""Platform-wide runtime switches, read through a short cache.

The stored row (``platform_controls``) holds optional overrides: NULL means the
environment default applies, so a database nobody has touched behaves exactly as
before. Every process (API, worker) reads the row through ``PlatformControls``,
which caches the answer for ``ttl_seconds`` (default 5), so a flipped switch
takes effect everywhere within that time and without a restart.

Failure policy: a failed read never blocks traffic by itself. The reader keeps
serving the last value it read successfully (so an explicit ``read_only`` stays
in force through a database blip) and falls back to the environment defaults only
when it has never read the row. Failures are logged once per cache interval.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

logger = logging.getLogger("product.controls")

DEFAULT_TTL_SECONDS = 5.0
SWITCHES: tuple[str, ...] = (
    "admissions_paused",
    "worker_paused",
    "read_only",
    "signup_enabled",
)


@dataclass(frozen=True)
class StoredControls:
    """The row as stored: ``None`` means "use the environment default"."""

    version: int = 0
    admissions_paused: bool | None = None
    worker_paused: bool | None = None
    read_only: bool | None = None
    signup_enabled: bool | None = None
    message: str | None = None
    updated_by: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ControlsSnapshot:
    """The effective switches after applying defaults."""

    version: int = 0
    admissions_paused: bool = False
    # True only when an operator set the pause explicitly (it then applies in
    # every limits mode, including ``off`` and ``observe``).
    admissions_forced: bool = False
    worker_paused: bool = False
    read_only: bool = False
    signup_enabled: bool = True
    message: str | None = None


def resolve(stored: StoredControls, *, admissions_default: bool) -> ControlsSnapshot:
    """Combine the stored overrides with the environment defaults."""
    paused = (
        admissions_default
        if stored.admissions_paused is None
        else stored.admissions_paused
    )
    return ControlsSnapshot(
        version=stored.version,
        admissions_paused=paused,
        admissions_forced=stored.admissions_paused is True,
        worker_paused=bool(stored.worker_paused),
        read_only=bool(stored.read_only),
        signup_enabled=True if stored.signup_enabled is None else stored.signup_enabled,
        message=stored.message,
    )


class PlatformControls:
    """Cached, never-raising reader of the platform switches."""

    def __init__(
        self,
        load: Callable[[], StoredControls],
        *,
        admissions_default: bool = False,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the row reader, the environment default and the cache lifetime."""
        self._load = load
        self._default = admissions_default
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._value = resolve(StoredControls(), admissions_default=admissions_default)
        self._read_at: float | None = None

    def cached(self) -> ControlsSnapshot | None:
        """A fresh cached value without touching storage, else None."""
        with self._lock:
            if self._read_at is not None and self._clock() - self._read_at < self._ttl:
                return self._value
        return None

    def snapshot(self) -> ControlsSnapshot:
        """The effective switches, refreshing the cache when it is stale."""
        hit = self.cached()
        if hit is not None:
            return hit
        try:
            fresh = resolve(self._load(), admissions_default=self._default)
        except Exception:
            logger.exception("platform controls read failed; keeping last value")
            with self._lock:
                self._read_at = self._clock()
                return self._value
        with self._lock:
            self._value = fresh
            self._read_at = self._clock()
        return fresh

    def forget(self) -> None:
        """Drop the cache (used after an in-process write)."""
        with self._lock:
            self._read_at = None

    def worker_paused(self) -> bool:
        """Whether workers must not claim new work."""
        return self.snapshot().worker_paused

    def signups_open(self) -> bool:
        """Whether new accounts may be created."""
        return self.snapshot().signup_enabled
