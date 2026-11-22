"""Public waitlist routes under ``/api/v0/waitlist``.

No sign-in, cookie or CSRF token is used or accepted: the authentication
middleware lets this prefix through untouched, nothing here reads a cookie or
an ``Authorization`` header, and no response sets a cookie. A submission is
reached only through the ``X-Waitlist-Token`` header.

The client address for rate limiting is ``request.client.host``. In production
the API sits behind Caddy, which replaces ``X-Forwarded-For`` with the real peer
address, and uvicorn runs with ``--proxy-headers`` so that header becomes
``request.client.host``. Nothing the browser sends is trusted, and no address is
stored (only hashed, short-lived limiter buckets).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from contracts.v1 import WaitlistSource
from modules.waitlist.errors import InvalidInput
from modules.waitlist.questions import consent_number
from modules.waitlist.service import (
    InvalidToken,
    JoinCommand,
    QuestionVersionChanged,
    UnknownQuestionSet,
    WaitlistService,
)
from shared.errors import LoginThrottled

PREFIX = "/api/v0/waitlist"
MAX_BODY_BYTES = 8 * 1024
TokenHeader = Annotated[str | None, Header(alias="X-Waitlist-Token")]
KeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


class JoinBody(BaseModel):
    """The join request. The honeypot is ``company_website``."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=1000)
    consent: bool
    consent_version: str = Field(max_length=20)
    source: WaitlistSource | None = None
    question_version: int | None = None
    company_website: str = Field(default="", max_length=2000)


class AnswerBody(BaseModel):
    """One answer: an option id, a list of ids, a band id or short text."""

    model_config = ConfigDict(extra="forbid")

    value: str | list[str]
    other: str | None = Field(default=None, max_length=1000)
    question_version: int | None = None


class PayloadTooLarge(Exception):
    """The request body is over the cap."""


def refuse(
    status: int,
    detail: str,
    code: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    """A JSON refusal that is never cached and never echoes the input."""
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code, **extra},
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def _address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _read_body(request: Request) -> bytes:
    """Read at most 8 KB, whatever the client declares."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise PayloadTooLarge
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAX_BODY_BYTES:
            raise PayloadTooLarge
    return bytes(received)


def _parse(model: type[BaseModel], body: bytes) -> Any:
    """Validate a JSON body; a bad one is a 422 that names fields, not values."""
    try:
        return model.model_validate(json.loads(body or b"null"))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise InvalidInput("Invalid request: " + ", ".join(fields or ["body"])) from exc
    except ValueError as exc:
        raise InvalidInput("Request body must be JSON") from exc


def _throttled(exc: LoginThrottled) -> JSONResponse:
    seconds = exc.retry_after_seconds
    return refuse(
        429,
        f"Too many requests. Try again in {seconds} seconds.",
        "rate_limited",
        {"Retry-After": str(seconds)},
        retry_after=seconds,
    )


async def _guarded(call: Callable[[], Any], ok: int = 200) -> JSONResponse:
    """Run a blocking use case off the event loop and map its refusals."""
    try:
        body = await run_in_threadpool(call)
    except PayloadTooLarge:
        return refuse(413, "Request body is too large", "payload_too_large")
    except InvalidInput as exc:
        return refuse(422, str(exc), "invalid_request")
    except InvalidToken:
        return refuse(401, "Invalid or expired token", "invalid_token")
    except QuestionVersionChanged as exc:
        return refuse(
            409,
            "The questions changed. Reload them and try again.",
            "question_version_changed",
            question_version=exc.submission_version,
            current_version=exc.active_version,
        )
    except UnknownQuestionSet:
        return refuse(404, "Unknown question set", "unknown_question_set")
    except LoginThrottled as exc:
        return _throttled(exc)
    return JSONResponse(
        status_code=ok, content=body, headers={"Cache-Control": "no-store"}
    )


def _idempotency(key: str | None) -> str | None:
    if key is None:
        return None
    if not 1 <= len(key) <= 128:
        raise InvalidInput("Idempotency-Key must be 1 to 128 characters")
    return key


def _join_route(app: FastAPI, service: WaitlistService) -> None:
    @app.post(f"{PREFIX}/submissions")
    async def waitlist_join(request: Request, idempotency_key: KeyHeader = None) -> Any:
        try:
            body = await _read_body(request)
        except PayloadTooLarge:
            return refuse(413, "Request body is too large", "payload_too_large")

        def join() -> Any:
            parsed: JoinBody = _parse(JoinBody, body)
            if parsed.consent is not True:
                raise InvalidInput("Consent is required to join")
            command = JoinCommand(
                email=parsed.email,
                consent_version=consent_number(parsed.consent_version),
                source=parsed.source or WaitlistSource(),
                question_version=parsed.question_version,
                idempotency_key=_idempotency(idempotency_key),
                honeypot_filled=bool(parsed.company_website.strip()),
            )
            return service.join(command, _address(request)).public()

        return await _guarded(join, ok=201)


def _read_routes(app: FastAPI, service: WaitlistService) -> None:
    @app.get(f"{PREFIX}/questions")
    async def waitlist_questions(request: Request, version: int | None = None) -> Any:
        response = await _guarded(lambda: service.questions(version, _address(request)))
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=300"
        return response

    @app.get(f"{PREFIX}/submissions/current")
    async def waitlist_current(request: Request, token: TokenHeader = None) -> Any:
        return await _guarded(
            lambda: service.current(token or "", _address(request)).public()
        )


def _answer_routes(app: FastAPI, service: WaitlistService) -> None:
    @app.put(f"{PREFIX}/submissions/current/answers/{{question_id}}")
    async def waitlist_answer(
        question_id: str, request: Request, token: TokenHeader = None
    ) -> Any:
        try:
            body = await _read_body(request)
        except PayloadTooLarge:
            return refuse(413, "Request body is too large", "payload_too_large")

        def save() -> Any:
            parsed: AnswerBody = _parse(AnswerBody, body)
            return service.save_answer(
                token or "",
                question_id,
                parsed.value,
                parsed.other,
                parsed.question_version,
                _address(request),
            ).public()

        return await _guarded(save)

    @app.delete(f"{PREFIX}/submissions/current/answers/{{question_id}}")
    async def waitlist_clear(
        question_id: str,
        request: Request,
        token: TokenHeader = None,
        question_version: int | None = None,
    ) -> Any:
        return await _guarded(
            lambda: service.clear_answer(
                token or "", question_id, question_version, _address(request)
            ).public()
        )


def register_waitlist_routes(app: FastAPI, service: WaitlistService) -> None:
    """Register the five public routes."""
    _read_routes(app, service)
    _join_route(app, service)
    _answer_routes(app, service)
