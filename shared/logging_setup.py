"""JSON structured logging with correlation fields and defensive redaction.

Every record is one JSON object on one line. Fields come from an allow-list:
the fixed record fields, the active correlation context and a short list of
explicit ``extra`` keys. Free-text messages and exception messages are passed
through :func:`redact`, which removes secret-looking values (credentials,
cookies, bearer and account tokens, secret-keyed pairs, URL secrets, long opaque
blobs and model prompt or message bodies). Code must still avoid logging
payloads; redaction is the safety net, not the policy.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import IO, Any

from shared.correlation import CORRELATION_FIELDS, current_fields

MAX_MESSAGE_CHARS = 2000
MAX_STACK_FRAMES = 12
REDACTED = "[redacted]"

# ``extra={...}`` keys that may appear on a line. Anything else is dropped.
ALLOWED_EXTRA = (
    "event",
    "method",
    "route",
    "status",
    "status_class",
    "duration_ms",
    "outcome",
    "queue",
    "resource",
    "count",
    "attempts",
    "exc_type",
    *CORRELATION_FIELDS,
)

_SECRET_WORDS = (
    r"authorization|api[_-]?key|apikey|secret|token(?!s)|password|passwd|cookie|"
    r"credential|private[_-]?key|session|csrf|prompt|instructions|"
    r"input_value|initial_message|content"
)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), rf"\1 {REDACTED}"),
    # key=value, key: value, "key": "value" for secret-like key names.
    (
        re.compile(
            rf"""(?ix)(["']?[\w-]*(?:{_SECRET_WORDS})[\w-]*["']?\s*[=:]\s*)
            ("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,;&}}\]]+)"""
        ),
        rf"\1{REDACTED}",
    ),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\b(?:wvs|wlt|ghp|gho|xox[abp])[_-][A-Za-z0-9_-]{8,}"), REDACTED),
    (
        re.compile(
            r"(?i)([?&](?:code|state|token|key|secret|password|api_key|"
            r"access_token|refresh_token|signature)=)[^&\s]+"
        ),
        rf"\1{REDACTED}",
    ),
    # Long opaque blobs (keys, JWTs, digests); UUID-length ids stay readable.
    (re.compile(r"\b(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{41,}\b"), REDACTED),
)


def redact(text: str) -> str:
    """Remove secret-looking values from free text."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _exception_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Type, a redacted short message and code locations only (no source text)."""
    if not record.exc_info or record.exc_info[1] is None:
        return {}
    error = record.exc_info[1]
    frames = traceback.extract_tb(record.exc_info[2])[-MAX_STACK_FRAMES:]
    return {
        "exc_type": type(error).__name__,
        "exc_message": _clip(redact(str(error)), 300),
        "stack": [f"{frame.filename}:{frame.lineno}:{frame.name}" for frame in frames],
    }


class JsonFormatter(logging.Formatter):
    """Format records as single-line JSON using only allow-listed fields."""

    def __init__(self, service: str) -> None:
        """Name the process (``api`` or ``worker``) on every line."""
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        """Render one record; never raises on bad message arguments."""
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": _clip(redact(message), MAX_MESSAGE_CHARS),
        }
        payload.update(current_fields())
        for key in ALLOWED_EXTRA:
            value = record.__dict__.get(key)
            if isinstance(value, str):
                payload[key] = _clip(redact(value), 200)
            elif isinstance(value, int | float) and not isinstance(value, bool):
                payload[key] = value
        payload.update(_exception_fields(record))
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


_HANDLER_MARK = "_weaves_json_handler"


def configure_logging(
    service: str,
    *,
    level: str = "INFO",
    replace_root: bool = False,
    stream: IO[str] | None = None,
) -> logging.Handler:
    """Install the JSON handler on the root logger (idempotent) and return it.

    ``replace_root`` drops other root handlers (the worker owns its process).
    The API leaves them alone so test harnesses keep their capture handlers.
    uvicorn's own loggers are folded into the root handler, and its per-request
    access line is silenced because the API logs one richer line per request.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False) or replace_root:
            root.removeHandler(existing)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter(service))
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    return handler
