"""Opaque keyset cursors for task and transcript pages."""

from __future__ import annotations

import base64
import json
from datetime import datetime


def encode_cursor(timestamp: datetime, record_id: str) -> str:
    """Encode a timestamp and stable record ID for the next page request."""
    payload = json.dumps(
        {"created_at": timestamp.isoformat(), "record_id": record_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode and validate one task page cursor from an HTTP query value."""
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
