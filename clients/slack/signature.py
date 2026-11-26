"""Verify Slack request signatures (``v0`` HMAC-SHA256 over the raw body)."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping

from clients.slack.errors import SlackSignatureError

SIGNATURE_HEADER = "x-slack-signature"
TIMESTAMP_HEADER = "x-slack-request-timestamp"
_TIMESTAMP = re.compile(r"[0-9]{1,12}")
_SIGNATURE = re.compile(r"v0=[0-9a-f]{64}")


def compute_signature(secret: bytes, timestamp: str, raw_body: bytes) -> str:
    """Return ``v0=`` plus the hex HMAC-SHA256 of ``v0:{timestamp}:{body}``."""
    base = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    return "v0=" + hmac.new(secret, base, hashlib.sha256).hexdigest()


def verify_slack_request(
    secret: bytes,
    headers: Mapping[str, str],
    raw_body: bytes,
    now: float,
    tolerance_seconds: int = 300,
) -> None:
    """Raise ``SlackSignatureError`` unless the request is fresh and authentic.

    Order matters: header presence, header shape, freshness, then the
    constant-time comparison. Header names are case-insensitive. The body must
    be the exact bytes received, before any parsing.
    """
    if not secret:
        raise ValueError("a signing secret is required")
    lowered = {name.lower(): value for name, value in headers.items()}
    timestamp = lowered.get(TIMESTAMP_HEADER)
    signature = lowered.get(SIGNATURE_HEADER)
    if not timestamp or not signature:
        raise SlackSignatureError("missing_header")
    if not _TIMESTAMP.fullmatch(timestamp) or not _SIGNATURE.fullmatch(signature):
        raise SlackSignatureError("malformed")
    if abs(now - int(timestamp)) > tolerance_seconds:
        raise SlackSignatureError("stale")
    expected = compute_signature(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii")):
        raise SlackSignatureError("bad_signature")
