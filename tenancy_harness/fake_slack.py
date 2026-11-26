"""A fake Slack for the disposable tenancy stack and the Slack unit tests.

An external-provider fake like ``fake_google.py``: product code talks to it
unchanged, with ``WEAVES_SLACK_API_BASE_URL`` pointing at ``/slack/api``. It
holds no real credentials: the signing secret, codes and tokens are made up
(``fake_slack_state``). No real Slack host is ever contacted.

* ``POST /slack/api/{method}``: Web API (form or JSON body, bearer token) for
  ``oauth.v2.access``, ``auth.test|revoke``, ``chat.postMessage|update|delete|
  postEphemeral``, ``reactions.add``, ``assistant.threads.setStatus``,
  ``conversations.replies`` (one request per minute per workspace, 15 messages)
  and ``conversations.list``. Details: ``fake_slack_methods``.
* ``POST /slack/response/{id}``: a ``response_url`` receiver, five uses in 30
  minutes.
* ``POST /slack/_stub/events``: a minimal Request URL that verifies the
  signature with product code and echoes ``url_verification`` challenges.
* ``/slack/_control/*``: ``deliver_event``, ``deliver_interaction`` (signed with
  the test secret), ``calls``, ``state``, ``configure`` (fault injection) and
  ``reset``. Delivery timing is modelled in ``fake_slack_delivery``.
"""

import json
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from clients.slack.errors import SlackSignatureError
from clients.slack.signature import verify_slack_request
from tenancy_harness.fake_slack_delivery import deliver_event, deliver_interaction
from tenancy_harness.fake_slack_methods import HANDLERS, Fail
from tenancy_harness.fake_slack_state import (
    CONFIGURE_KEYS,
    DELIVERY_FAULTS,
    RESPONSE_URL_USES,
    RESPONSE_URL_WINDOW_SECONDS,
    STATE,
    signing_secret,
)

router = APIRouter(prefix="/slack")
NO_TOKEN_METHODS = {"oauth.v2.access"}
SECRET_PARAMS = {"token", "client_secret", "code"}


async def _params(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if "json" in request.headers.get("content-type", ""):
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
    return {k: v[-1] for k, v in parse_qs(raw.decode(), keep_blank_values=True).items()}


def _token_record(request: Request, params: dict[str, Any]) -> dict[str, Any] | None:
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip() or params.get("token", "")
    return STATE.tokens.get(token)


def _record(method, params, token, status, error):
    kept = {k: v for k, v in params.items() if k not in SECRET_PARAMS}
    STATE.calls.append(
        {
            "method": method,
            "params": kept,
            "team": token["team"] if token else None,
            "status": status,
            "error": error,
            "at": STATE.clock(),
        }
    )


def _fail(method, params, token, error, status=200, headers=None):
    _record(method, params, token, status, error)
    return JSONResponse(
        {"ok": False, "error": error}, status_code=status, headers=headers
    )


@router.post("/api/{method}")
async def api(method: str, request: Request):
    """Dispatch one Web API call, applying any injected fault first."""
    params = await _params(request)
    token = _token_record(request, params)
    fault = STATE.take_api_fault(method)
    if fault and fault["kind"] == "500":
        _record(method, params, token, 500, "fault_500")
        return PlainTextResponse("fixture internal error", status_code=500)
    if fault and fault["kind"] == "429":
        headers = {"Retry-After": str(fault["retry_after"])}
        return _fail(method, params, token, "ratelimited", 429, headers)
    if fault and fault["kind"] == "slow":
        await STATE.sleep(fault["delay_seconds"])
    handler = HANDLERS.get(method)
    if handler is None:
        return _fail(method, params, token, "unknown_method")
    if method not in NO_TOKEN_METHODS:
        if token is None:
            return _fail(method, params, token, "invalid_auth")
        if token["revoked"]:
            return _fail(method, params, token, "token_revoked")
    try:
        body = handler(params, token or {})
    except Fail as failure:
        return _fail(
            method, params, token, failure.error, failure.status, failure.headers
        )
    _record(method, params, token, 200, None)
    return {"ok": True, **body}


@router.post("/response/{response_id}")
async def response_url(response_id: str):
    """Accept a ``response_url`` post five times within 30 minutes."""
    now = STATE.clock()
    uses = STATE.response_urls.setdefault(response_id, [])
    first = uses[0] if uses else now
    if now - first > RESPONSE_URL_WINDOW_SECONDS or len(uses) >= RESPONSE_URL_USES:
        return PlainTextResponse("used_url", status_code=404)
    uses.append(now)
    return PlainTextResponse("ok")


@router.post("/_stub/events")
async def stub_events(request: Request):
    """A Request URL that verifies signatures and answers url_verification."""
    raw = await request.body()
    try:
        verify_slack_request(
            signing_secret(), dict(request.headers), raw, STATE.clock()
        )
    except SlackSignatureError as error:
        return JSONResponse({"error": error.reason}, status_code=401)
    payload = json.loads(raw or b"{}")
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    return Response(status_code=200)


@router.post("/_control/deliver_event")
async def control_deliver_event(request: Request):
    """Control deliver event."""
    body = await request.json()
    return await deliver_event(body["url"], body["payload"])


@router.post("/_control/deliver_interaction")
async def control_deliver_interaction(request: Request):
    """Control deliver interaction."""
    body = await request.json()
    payload = dict(body["payload"])
    if "response_url" not in payload:
        origin = body.get("public_base") or str(request.base_url).rstrip("/")
        payload["response_url"] = f"{origin}/slack/response/{STATE.next_id('R')}"
    return await deliver_interaction(body["url"], payload)


@router.get("/_control/calls")
def control_calls():
    """Control calls."""
    return {"calls": STATE.calls}


@router.get("/_control/state")
def control_state():
    """Control state."""
    now = STATE.clock()
    live = {k: v for k, v in STATE.statuses.items() if v["expires_at"] > now}
    STATE.statuses = live
    return {
        "messages": STATE.messages,
        "ephemerals": STATE.ephemerals,
        "reactions": sorted(["|".join(r) for r in STATE.reactions]),
        "statuses": {f"{c}|{t}": v["status"] for (c, t), v in live.items()},
        "deliveries": STATE.deliveries,
    }


@router.post("/_control/configure")
async def control_configure(request: Request):
    """Control configure."""
    body = await request.json()
    unknown = set(body) - CONFIGURE_KEYS
    if unknown or body.get("delivery_fault") not in DELIVERY_FAULTS:
        return JSONResponse({"error": "invalid configuration"}, status_code=422)
    if body.get("status_mode", "supported") not in {"supported", "unsupported"}:
        return JSONResponse({"error": "invalid configuration"}, status_code=422)
    try:
        STATE.configure(body)
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "invalid configuration"}, status_code=422)
    return {"configured": sorted(body)}


@router.post("/_control/reset")
def control_reset():
    """Control reset."""
    STATE.reset()
    return {"reset": True}
