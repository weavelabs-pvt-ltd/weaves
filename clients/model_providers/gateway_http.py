"""Shared safe HTTP error translation for model provider adapters."""

from __future__ import annotations

import re

import httpx

from shared.errors import ModelGatewayError


def raise_upstream_error(response: httpx.Response) -> None:
    """Expose only a validated provider error code, never its raw message."""
    if 300 <= response.status_code < 400:
        raise ModelGatewayError(
            "model.redirect_blocked", "Model provider redirects are not permitted"
        )
    code = None
    try:
        error = response.json().get("error", {})
        candidate = error.get("code") or error.get("type")
        if isinstance(candidate, str) and re.fullmatch(
            r"[A-Za-z0-9_.-]{1,80}", candidate
        ):
            code = candidate
    except (AttributeError, ValueError):
        pass
    suffix = f" ({code})" if code else ""
    retryable = response.status_code == 429 or response.status_code >= 500
    raise ModelGatewayError(
        "model.upstream_error",
        f"Model provider returned HTTP {response.status_code}{suffix}",
        retryable=retryable,
        http_status=response.status_code,
    )


def empty_response_error(input_tokens: int, output_tokens: int) -> ModelGatewayError:
    """The provider answered (and billed tokens) without any text content."""
    return ModelGatewayError(
        "model.empty_response",
        "Model provider returned no text content",
        usage=(input_tokens, output_tokens),
    )
