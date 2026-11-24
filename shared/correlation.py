"""Request, tenant and job correlation carried in ``contextvars``.

One mutable field map is bound per request or claimed unit of work. Log lines
read it through :mod:`shared.logging_setup`; it never carries secrets, prompts
or payloads, only opaque identifiers. Fields are bound by ``bind_context`` and
restored on exit, so concurrent requests, worker claims and threads (which start
with an empty context) never see one another's values.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from shared.ids import new_id

# The only fields a binding may carry. Anything else is a programming error, so
# a payload or secret can never be attached to log context by accident.
CORRELATION_FIELDS = (
    "request_id",
    "org_id",
    "workspace_id",
    "principal",
    "job_id",
    "turn_id",
    "run_id",
    "worker_id",
)

# Inbound ``X-Request-Id`` is accepted only in this shape: printable, bounded
# and free of whitespace, quotes and control characters, so it cannot forge log
# lines or inject markup. Anything else is replaced with a generated id.
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,63}")

_context: ContextVar[dict[str, str] | None] = ContextVar(
    "weaves_correlation", default=None
)


def validated_request_id(candidate: str | None) -> str | None:
    """Return ``candidate`` when it is a well-formed request id, else None."""
    if candidate is not None and _REQUEST_ID.fullmatch(candidate):
        return candidate
    return None


def resolve_request_id(inbound: str | None) -> str:
    """Use a well-formed inbound id, otherwise generate an opaque one."""
    return validated_request_id(inbound) or new_id()


def principal_digest(principal_id: str) -> str:
    """A short stable digest, so logs link a principal without naming it."""
    return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:12]


def _checked(fields: dict[str, str | None]) -> dict[str, str]:
    unknown = set(fields) - set(CORRELATION_FIELDS)
    if unknown:
        raise ValueError(f"unknown correlation fields: {sorted(unknown)}")
    return {key: value for key, value in fields.items() if value}


@contextmanager
def bind_context(**fields: str | None) -> Iterator[None]:
    """Start a fresh correlation scope (inheriting nothing), restored on exit."""
    token = _context.set(_checked(fields))
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def extend_context(**fields: str | None) -> Iterator[None]:
    """Add fields on top of the current scope, restoring the previous map."""
    token = _context.set({**(_context.get() or {}), **_checked(fields)})
    try:
        yield
    finally:
        _context.reset(token)


def note(**fields: str | None) -> None:
    """Add fields to the current scope in place (a no-op outside any scope).

    The map is shared with work spawned from the scope, so tenant fields noted
    during authentication are still present on the closing access-log line.
    """
    current = _context.get()
    if current is not None:
        current.update(_checked(fields))


def note_tenant(principal_id: str, org_id: str, workspace_id: str) -> None:
    """Record the authenticated tenant scope on the current request."""
    note(
        principal=principal_digest(principal_id),
        org_id=org_id,
        workspace_id=workspace_id,
    )


def current_fields() -> dict[str, str]:
    """A detached copy of the active correlation fields (empty when none)."""
    return dict(_context.get() or {})


def current_request_id() -> str | None:
    """The request id active here, or None outside a request or claimed job."""
    return (_context.get() or {}).get("request_id")
