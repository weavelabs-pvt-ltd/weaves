"""``POST /api/v0/slack/interactions``: the signed Interactivity Request URL.

Slack posts ``application/x-www-form-urlencoded`` with one field, ``payload``,
holding JSON. The order of work is that of the events endpoint, each step before
the next one's cost: the endpoint is off unless the signing secret is set (503,
nothing done); the signature is verified on the **raw** body with the five-minute
window (the form is only decoded afterwards); the ``block_actions`` payload for
one of our buttons goes to the ingest (one lookup, one insert) under a 2.5 s
budget; the response is an empty 200. The work itself (Stop, Try again) is done
by the worker's ``slack_interactions`` tick, so a slow Slack call can never make
the ack late. Bodies, references and ``response_url`` are never logged (and the
latter is never even read: replies are private messages with the bot token).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from clients.slack.errors import SlackSignatureError
from clients.slack.signature import verify_slack_request
from modules.slack.ingest_limiter import IngestLimiter
from modules.slack.interactions.ingest import INTERACTION, SlackInteractionIngest
from modules.slack.interactions.payload import Press, parse_press
from shared.metrics import SLACK_ACK_SECONDS, SLACK_EVENTS

logger = logging.getLogger("product.slack")
INTERACTIONS_PATH = "/api/v0/slack/interactions"
MAX_BODY_BYTES = 1024 * 1024
ACK_BUDGET_SECONDS = 2.5
MAX_PAYLOAD_CHARS = 512 * 1024

Served = tuple[Response, str]  # response, outcome


def _reply(
    status: int, content: dict[str, Any], outcome: str, retry_after: str | None = None
) -> Served:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return JSONResponse(content, status_code=status, headers=headers), outcome


def _payload(raw: bytes) -> dict[str, Any] | None:
    """The JSON object in the form's ``payload`` field, or ``None``."""
    try:
        form = parse_qs(raw.decode("utf-8"), strict_parsing=True, max_num_fields=20)
        (text,) = form["payload"]
        if len(text) > MAX_PAYLOAD_CHARS:
            return None
        payload = json.loads(text)
    except (ValueError, KeyError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class SlackInteractionsEndpoint:
    """The Interactivity Request URL: verify, decode, store, acknowledge."""

    def __init__(
        self,
        *,
        signing_secret: bytes,
        ingest: SlackInteractionIngest,
        limiter: IngestLimiter | None = None,
        clock: Callable[[], float] = time.time,
        budget_seconds: float | None = None,
    ) -> None:
        """An empty ``signing_secret`` means the endpoint answers 503."""
        self._secret = signing_secret
        self._ingest = ingest
        self._limiter = limiter or IngestLimiter()
        self._clock = clock
        self._budget = ACK_BUDGET_SECONDS if budget_seconds is None else budget_seconds

    async def __call__(self, request: Request) -> Response:
        """Serve one request and record its outcome and latency."""
        started = time.perf_counter()
        response, outcome = await self._serve(request)
        SLACK_EVENTS.inc(type=INTERACTION, outcome=outcome)
        SLACK_ACK_SECONDS.observe(time.perf_counter() - started)
        return response

    async def _serve(self, request: Request) -> Served:
        if not self._secret:
            return _reply(503, {"detail": "Slack is not configured"}, "not_configured")
        too_large = _reply(413, {"detail": "Request body too large"}, "size")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return too_large
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return too_large
        try:
            verify_slack_request(self._secret, request.headers, raw, self._clock())
        except SlackSignatureError as error:
            logger.warning("slack_signature_rejected reason=%s", error.reason)
            return _reply(401, {"detail": "Invalid signature"}, "bad_signature")
        payload = _payload(raw)
        if payload is None:
            return _reply(400, {"detail": "Invalid request body"}, "malformed")
        parsed = parse_press(payload)
        if parsed.press is None:
            outcome = parsed.outcome
            if outcome == "malformed":
                return _reply(400, {"detail": "Invalid request body"}, outcome)
            return Response(status_code=200), outcome  # not one of our buttons
        if not self._limiter.acquire():
            body = {"detail": "Too many requests"}
            return _reply(429, body, "rate_limited", retry_after="1")
        return await self._accept(parsed.press)

    async def _accept(self, press: Press) -> Served:
        try:
            outcome: str = await asyncio.wait_for(
                run_in_threadpool(self._ingest.handle, press), timeout=self._budget
            )
        except Exception as error:
            logger.error("slack_interaction_failed kind=%s", type(error).__name__)
            body = {"detail": "Temporarily unavailable"}
            return _reply(503, body, "error", retry_after="1")
        logger.info("slack_interaction action=%s outcome=%s", press.action, outcome)
        return Response(status_code=200), outcome


def register_interactions_route(
    app: FastAPI, endpoint: SlackInteractionsEndpoint
) -> None:
    """Mount the endpoint (hidden from the OpenAPI schema)."""
    app.post(INTERACTIONS_PATH, include_in_schema=False)(endpoint)
