"""``POST /api/v0/slack/events``: the signed Slack Events API Request URL.

Order of work, each step before the next one's cost: the endpoint is off unless
the signing secret is set (503, nothing done); the signature is verified on the
raw body with the five-minute window; ``url_verification`` echoes its challenge
with no database work; ``event_callback`` goes to the ingest (one lookup, one
insert) under a 2.5 s budget so the ack stays inside Slack's three seconds. The
``:eyes:`` reaction runs after the response. Bodies and tokens are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from application.ports.slack import SlackInstallation
from clients.slack.errors import SlackSignatureError
from clients.slack.signature import verify_slack_request
from modules.slack.events import Ingested, SlackEventIngest, metric_type
from modules.slack.ingest_limiter import IngestLimiter
from shared.correlation import extend_context, validated_request_id
from shared.metrics import SLACK_ACK_SECONDS, SLACK_EVENTS

logger = logging.getLogger("product.slack")
EVENTS_PATH = "/api/v0/slack/events"
MAX_BODY_BYTES = 1024 * 1024
ACK_BUDGET_SECONDS = 2.5
MAX_CHALLENGE_CHARS = 1024
_DIGITS = re.compile(r"[0-9]{1,2}")

AckReaction = Callable[[SlackInstallation, str, str], object]
Served = tuple[JSONResponse, str, str]  # response, event type, outcome


def _reply(
    status: int,
    content: dict[str, Any],
    kind: str,
    outcome: str,
    retry_after: str | None = None,
) -> Served:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return JSONResponse(content, status_code=status, headers=headers), kind, outcome


def _parse(raw: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _short(value: str | None) -> str:
    return value[:32] if value and value.isascii() and value.isprintable() else "-"


class SlackEventsEndpoint:
    """The Events API Request URL: verify, answer, ingest, acknowledge."""

    def __init__(
        self,
        *,
        signing_secret: bytes,
        ingest: SlackEventIngest,
        react: AckReaction,
        limiter: IngestLimiter | None = None,
        clock: Callable[[], float] = time.time,
        budget_seconds: float | None = None,
    ) -> None:
        """An empty ``signing_secret`` means the endpoint answers 503."""
        self._secret = signing_secret
        self._ingest = ingest
        self._react = react
        self._limiter = limiter or IngestLimiter()
        self._clock = clock
        self._budget = ACK_BUDGET_SECONDS if budget_seconds is None else budget_seconds

    async def __call__(
        self, request: Request, background: BackgroundTasks
    ) -> JSONResponse:
        """Serve one request and record its outcome and latency."""
        started = time.perf_counter()
        response, kind, outcome = await self._serve(request, background)
        SLACK_EVENTS.inc(type=kind, outcome=outcome)
        SLACK_ACK_SECONDS.observe(time.perf_counter() - started)
        return response

    async def _serve(self, request: Request, background: BackgroundTasks) -> Served:
        if not self._secret:
            body = {"detail": "Slack events are not configured"}
            return _reply(503, body, "other", "not_configured")
        too_large = _reply(413, {"detail": "Request body too large"}, "other", "size")
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
            body = {"detail": "Invalid signature"}
            return _reply(401, body, "other", "bad_signature")
        payload = _parse(raw)
        if payload is None:
            return _reply(400, {"detail": "Invalid request body"}, "other", "malformed")
        if payload.get("type") == "url_verification":
            return self._verification(payload)
        if not self._limiter.acquire():
            body = {"detail": "Too many events"}
            return _reply(429, body, "other", "rate_limited", retry_after="1")
        return await self._accept(request, background, payload)

    @staticmethod
    def _verification(payload: dict[str, Any]) -> Served:
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or len(challenge) > MAX_CHALLENGE_CHARS:
            body = {"detail": "Invalid request body"}
            return _reply(400, body, "url_verification", "malformed")
        return _reply(200, {"challenge": challenge}, "url_verification", "verified")

    async def _accept(
        self, request: Request, background: BackgroundTasks, payload: dict[str, Any]
    ) -> Served:
        event = payload.get("event")
        kind = metric_type(event.get("type") if isinstance(event, dict) else None)
        request_id = validated_request_id(f"slack-{payload.get('event_id')}")
        retry = (
            int(request.headers.get("x-slack-retry-num", "0"))
            if _DIGITS.fullmatch(request.headers.get("x-slack-retry-num", ""))
            else 0
        )
        try:
            with extend_context(request_id=request_id):
                result: Ingested = await asyncio.wait_for(
                    run_in_threadpool(
                        self._ingest.handle,
                        payload,
                        retry_num=retry,
                        request_id=request_id,
                    ),
                    timeout=self._budget,
                )
                logger.info(
                    "slack_event type=%s outcome=%s retry_num=%d retry_reason=%s",
                    kind,
                    result.outcome,
                    retry,
                    _short(request.headers.get("x-slack-retry-reason")),
                )
        except Exception as error:
            # Not acked: Slack retries, and the unique event id makes that safe.
            logger.error(
                "slack_event_failed type=%s kind=%s", kind, type(error).__name__
            )
            body = {"detail": "Temporarily unavailable"}
            return _reply(503, body, kind, "error", retry_after="1")
        if result.ack_reaction is not None:
            background.add_task(self._react, *result.ack_reaction)
        return _reply(200, {"ok": True}, result.event_type, result.outcome)


def register_events_route(app: FastAPI, endpoint: SlackEventsEndpoint) -> None:
    """Mount the endpoint (hidden from the OpenAPI schema)."""
    app.post(EVENTS_PATH, include_in_schema=False)(endpoint)
