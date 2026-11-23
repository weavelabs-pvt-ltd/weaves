"""Bounded list pages: ``limit``, opaque keyset ``cursor`` and the response shape.

Compatibility contract for every collection endpoint: a request with neither
``cursor`` nor ``paginate=true`` keeps its historical bare-array response (a
``limit`` alone only truncates it). A request with either returns
``{"items": [...], "next_cursor": str | null}`` where ``next_cursor`` is
``null`` on the last page. Cursors are opaque, bind only a record identifier
and are always applied inside the caller's tenant scope, so a cursor taken from
another organization can at worst skip rows of the caller's own listing.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any

from shared.limits_config import LimitsConfig


@lru_cache(maxsize=1)
def page_limits() -> tuple[int, int]:
    """Configured ``(default, maximum)`` page sizes (``WEAVES_PAGE_*``)."""
    config = LimitsConfig.from_env(os.environ)
    return config.page_default, config.page_max


@lru_cache(maxsize=1)
def recovery_batch_size() -> int:
    """Rows a recovery or dispatch tick fetches per query."""
    return LimitsConfig.from_env(os.environ).recovery_batch


def encode_key_cursor(key: str) -> str:
    """Encode a record identifier into an opaque cursor."""
    raw = json.dumps({"k": key}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_key_cursor(cursor: str) -> str:
    """Decode a cursor produced by ``encode_key_cursor`` or raise ``ValueError``."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        key = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))["k"]
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    if not isinstance(key, str) or not key:
        raise ValueError("cursor is invalid")
    return key


def encode_time_cursor(timestamp: datetime, record_id: str) -> str:
    """Encode a ``(created_at, record_id)`` position for newest-first pages."""
    payload = json.dumps(
        {"created_at": timestamp.isoformat(), "record_id": record_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_time_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a time cursor from an HTTP query value or raise ``ValueError``."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        timestamp = datetime.fromisoformat(payload["created_at"])
        record_id = payload["record_id"]
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    if timestamp.tzinfo is None or not isinstance(record_id, str) or not record_id:
        raise ValueError("cursor is invalid")
    return timestamp, record_id


@dataclass(frozen=True)
class RecentPageRequest:
    """Paging parameters for a newest-first collection (time cursor)."""

    limit: int | None
    cursor_created_at: datetime | None
    cursor_id: str | None
    envelope: bool

    @property
    def effective_limit(self) -> int:
        """Rows to return; legacy bare arrays keep their historical bound."""
        if self.limit is not None:
            return self.limit
        return page_limits()[0]


def recent_page_request(
    limit: int | None,
    cursor: str | None,
    paginate: bool = False,
    *,
    legacy_default: int | None = None,
) -> RecentPageRequest:
    """Validate raw query values for a newest-first collection.

    ``legacy_default`` is the bound the bare-array response always had (for
    endpoints that returned a fixed newest page); other bare-array endpoints
    were unbounded and keep that only for callers that send no paging params.
    """
    maximum = page_limits()[1]
    if limit is not None and not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    created_at, record_id = decode_time_cursor(cursor) if cursor else (None, None)
    envelope = bool(cursor) or paginate
    if limit is None and not envelope and legacy_default is not None:
        limit = legacy_default
    return RecentPageRequest(limit, created_at, record_id, envelope)


@dataclass(frozen=True)
class PageRequest:
    """Validated paging parameters of one list request."""

    limit: int | None
    after: str | None
    envelope: bool

    @property
    def effective_limit(self) -> int | None:
        """Row bound for this request: ``None`` only for the legacy bare array."""
        if self.limit is not None:
            return self.limit
        return page_limits()[0] if self.envelope else None


def page_request(
    limit: int | None, cursor: str | None, paginate: bool = False
) -> PageRequest:
    """Validate raw query values; ``ValueError`` means a bad cursor or limit."""
    maximum = page_limits()[1]
    if limit is not None and not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    after = decode_key_cursor(cursor) if cursor else None
    return PageRequest(limit, after, envelope=bool(cursor) or paginate)


def slice_page[T](
    items: Sequence[T], key: Callable[[T], str], request: PageRequest
) -> tuple[list[T], str | None]:
    """One keyset page of an in-memory collection ordered by ``key``.

    For small, per-organization catalogs whose size is bounded by plan limits;
    growth collections use repository keyset queries instead.
    """
    ordered = sorted(items, key=key)
    if request.after is not None:
        ordered = [item for item in ordered if key(item) > request.after]
    bound = request.effective_limit
    if bound is None or len(ordered) <= bound:
        return ordered, None
    page = ordered[:bound]
    return page, encode_key_cursor(key(page[-1]))


def envelope(items: list[Any], next_cursor: str | None) -> dict[str, Any]:
    """The paginated response body."""
    return {"items": items, "next_cursor": next_cursor}


def respond[T](
    items: Sequence[T],
    key: Callable[[T], str],
    request: PageRequest,
    render: Callable[[T], Any],
) -> Any:
    """Render an in-memory collection as the legacy array or one page."""
    if not request.envelope:
        rows = list(items) if request.limit is None else list(items)[: request.limit]
        return [render(item) for item in rows]
    page, next_cursor = slice_page(items, key, request)
    return envelope([render(item) for item in page], next_cursor)


def batches[T](
    fetch: Callable[[tuple[Any, ...] | None], Sequence[T]],
    position: Callable[[T], tuple[Any, ...]],
    *,
    batch_size: int,
    max_batches: int = 10,
) -> Iterator[T]:
    """Walk ``fetch(after)`` pages with a keyset position so every pass progresses.

    ``fetch`` returns at most ``batch_size`` rows strictly after ``after``. The
    walk stops on a short page or after ``max_batches`` so one tick's work is
    bounded; the remainder is picked up by the next tick.
    """
    after: tuple[Any, ...] | None = None
    for _ in range(max_batches):
        rows = fetch(after)
        yield from rows
        if len(rows) < batch_size:
            return
        after = position(rows[-1])


def drain[T](
    fetch: Callable[[], Sequence[T]],
    handle: Callable[[T], bool],
    *,
    batch_size: int,
    max_batches: int = 10,
) -> None:
    """Handle rows that leave the query's predicate once handled.

    Each pass re-queries the head of the (indexed, status-filtered) set. The
    loop ends on a short page, on a pass that handled nothing (so rows that
    cannot be handled never spin the tick), or after ``max_batches``.
    """
    for _ in range(max_batches):
        rows = fetch()
        handled = sum(1 for row in rows if handle(row))
        if len(rows) < batch_size or handled == 0:
            return


def recent_response[T](
    rows: Sequence[T],
    request: RecentPageRequest,
    record_id: Callable[[T], str],
    render: Callable[[T], Any],
    *,
    created_at: Callable[[T], datetime] = lambda row: getattr(row, "created_at"),  # noqa: B009
) -> Any:
    """Render newest-first rows fetched with ``limit + 1`` as an array or page.

    ``rows`` holds up to ``bound + 1`` records; the extra one only signals that
    another page exists.
    """
    bound = request.limit or page_limits()[0]
    page = list(rows[:bound])
    items = [render(row) for row in page]
    if not request.envelope:
        return items
    more = len(rows) > bound
    return envelope(
        items,
        encode_time_cursor(created_at(page[-1]), record_id(page[-1]))
        if more and page
        else None,
    )
