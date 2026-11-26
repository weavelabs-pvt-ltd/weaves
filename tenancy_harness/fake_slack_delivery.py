"""Models how Slack delivers events and interactions to a Request URL.

Events: signed POST, success only if a 2xx arrives within 3 seconds; otherwise
up to ``len(retry_schedule)`` retries after the scheduled delays, carrying
``x-slack-retry-num`` and ``x-slack-retry-reason``, stopped early by a response
header ``x-slack-no-retry: 1``. Every attempt is re-signed with a fresh
timestamp. Interactions are one signed, form-encoded ``payload=`` POST and are
not retried. ``url_verification`` reports whether the challenge was echoed.
"""

import hashlib
import hmac
import json
from typing import Any
from urllib.parse import urlencode

import httpx

from tenancy_harness.fake_slack_state import (
    ACK_DEADLINE_SECONDS,
    STATE,
    signing_secret,
)

STALE_OFFSET_SECONDS = 600


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    """Slack's v0 signature, computed independently of the product code."""
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret, base, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, content_type: str) -> dict[str, str]:
    """Headers for one attempt, honouring the delivery fault switch."""
    now = int(STATE.clock())
    fault = STATE.delivery_fault
    stamp = now - STALE_OFFSET_SECONDS if fault == "stale_timestamp" else now
    secret = (
        b"wrong-" + signing_secret() if fault == "bad_signature" else signing_secret()
    )
    return {
        "content-type": content_type,
        "x-slack-request-timestamp": str(stamp),
        "x-slack-signature": sign(secret, str(stamp), body),
    }


async def _post(url: str, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """One attempt; reports status, ack seconds and Slack's failure reason."""
    started = STATE.clock()
    outcome: dict[str, Any] = {"status": None, "reason": None, "no_retry": False}
    text = ""
    try:
        async with httpx.AsyncClient(
            transport=STATE.transport,
            timeout=ACK_DEADLINE_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(url, content=body, headers=headers)
        outcome["status"] = response.status_code
        outcome["no_retry"] = response.headers.get("x-slack-no-retry") == "1"
        text = response.text
    except httpx.TimeoutException:
        outcome["reason"] = "http_timeout"
    except httpx.HTTPError:
        outcome["reason"] = "connection_failed"
    elapsed = STATE.clock() - started
    outcome["ack_seconds"] = round(elapsed, 3)
    if outcome["reason"] is None:
        if elapsed > ACK_DEADLINE_SECONDS:
            outcome["reason"] = "http_timeout"
        elif not 200 <= (outcome["status"] or 0) < 300:
            outcome["reason"] = "http_error"
    outcome["ok"] = outcome["reason"] is None
    outcome["body"] = text
    return outcome


def _echoed(challenge: str, body: str) -> bool:
    if body.strip() == challenge:
        return True
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("challenge") == challenge


async def deliver_event(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Deliver ``payload`` with Slack's retry behaviour; return a timing report."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    attempts: list[dict[str, Any]] = []
    delays = [None, *STATE.retry_schedule]
    reason = None
    for number, delay in enumerate(delays):
        if delay is not None:
            await STATE.sleep(delay)
        headers = signed_headers(body, "application/json")
        if number:
            headers["x-slack-retry-num"] = str(number)
            headers["x-slack-retry-reason"] = reason or "http_error"
        result = await _post(url, body, headers)
        body_text = result.pop("body")
        result.update(
            retry_num=number, delay_seconds=delay or 0, sent_headers=_visible(headers)
        )
        attempts.append(result)
        reason = result["reason"]
        if result["ok"]:
            echoed = None
            if payload.get("type") == "url_verification":
                echoed = _echoed(str(payload.get("challenge")), body_text)
            return _record(attempts, True, echoed)
        if result["no_retry"]:
            break
    return _record(attempts, False, None)


async def deliver_interaction(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Deliver one form-encoded interaction payload (Slack does not retry it)."""
    body = urlencode({"payload": json.dumps(payload, separators=(",", ":"))}).encode()
    headers = signed_headers(body, "application/x-www-form-urlencoded")
    result = await _post(url, body, headers)
    result.pop("body")
    result.update(retry_num=0, delay_seconds=0, sent_headers=_visible(headers))
    return _record([result], result["ok"], None)


def _visible(headers: dict[str, str]) -> dict[str, str]:
    """Headers a test may assert on; the signature itself is not exposed."""
    return {k: v for k, v in headers.items() if k != "x-slack-signature"}


def _record(attempts, delivered, echoed):
    report = {"delivered": delivered, "attempts": attempts, "challenge_echoed": echoed}
    STATE.deliveries.append(report)
    return report
