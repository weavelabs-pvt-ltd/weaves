"""Budget-gated real Anthropic relay, used only by opt-in testing containers."""

import hmac
import json
import os
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from starlette.responses import Response

from tenancy_harness.budget import MAX_OUTPUT_TOKENS, BudgetExhausted, Ledger


def credentials(request):
    """Verify adapter authentication against the mounted credential."""
    path = Path(os.environ["TENANCY_LIVE_CONFIG"])
    config = json.loads(path.read_text())
    if not hmac.compare_digest(request.headers.get("x-api-key", ""), config["api_key"]):
        raise HTTPException(401, "Testing provider credential mismatch")
    return config


def validate(body):
    """Reject costly/unpriced features before reserving or dispatching a request."""
    allowed = {"model", "system", "messages", "max_tokens", "temperature"}
    if set(body) - allowed or not 1 <= body.get("max_tokens", 0) <= MAX_OUTPUT_TOKENS:
        raise HTTPException(400, "Live smoke only permits bounded text messages")
    encoded = json.dumps(body)
    if (
        len(encoded.encode()) > 32000
        or '"cache_control"' in encoded
        or '"image"' in encoded
    ):
        raise HTTPException(400, "Unsupported or oversized live testing payload")


async def relay(request: Request):
    """Forward only models/messages to the fixed real origin after a durable hold."""
    config = credentials(request)
    target = request.path_params["endpoint"]
    if (request.method, target) not in {("GET", "models"), ("POST", "messages")}:
        raise HTTPException(404, "No such live testing endpoint")
    body = await request.json() if request.method == "POST" else None
    ledger = Ledger(Path(os.environ["TENANCY_BUDGET_DIR"]))
    reservation = None
    if body is not None:
        validate(body)
        try:
            reservation = ledger.reserve(body["model"], config["run_id"], 3)
        except BudgetExhausted as exc:
            raise HTTPException(402, str(exc)) from exc
    try:
        async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
            response = await client.request(
                request.method,
                "https://api.anthropic.com/v1/" + target,
                json=body,
                headers={
                    "x-api-key": config["api_key"],
                    "anthropic-version": "2023-06-01",
                },
            )
        if reservation:
            if response.status_code != 200:
                ledger.uncertain(reservation)
            else:
                ledger.settle(
                    reservation, body["model"], response.json().get("usage", {})
                )
        return Response(
            response.content,
            status_code=response.status_code,
            media_type="application/json",
            headers={"request-id": response.headers.get("request-id", "")},
        )
    except Exception:
        if reservation:
            ledger.uncertain(reservation)
        raise HTTPException(
            502, "Live provider failed; retained any uncertain budget hold"
        ) from None
