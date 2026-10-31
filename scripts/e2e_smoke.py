"""Exercise the deployed login → agent → model → artifact path.

Credentials are read from environment variables populated from Secret Manager
by the deployment workflow. This script never prints tokens or passwords.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def request(
    api_base: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, Any] | list[Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode() if payload is not None else None
    call = urllib.request.Request(
        f"{api_base.rstrip('/')}{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(call, timeout=45) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"{method} {path} returned HTTP {exc.code}: {detail}"
        ) from exc
    return json.loads(raw) if raw else {}


def main() -> int:
    """Exercise the deployed API with environment-provided credentials."""
    api_base = os.environ["WEAVES_E2E_API_BASE"].rstrip("/")
    token = _login(
        api_base, os.environ["WEAVES_E2E_EMAIL"], os.environ["WEAVES_E2E_PASSWORD"]
    )
    profile = _active_local_profile(api_base, token)
    agent_id = _smoke_agent(api_base, token, str(profile["id"]))
    _verify_smoke_run(api_base, token, agent_id)
    print("PASS API health, password login, agent creation, model completion, artifact")
    return 0


def _login(api_base: str, email: str, password: str) -> str:
    """Verify health and sign in with the deployment smoke-test account."""
    health = request(api_base, "/health")
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise RuntimeError("API health check did not return status=ok")
    session = request(
        api_base,
        "/auth/login",
        method="POST",
        payload={"email": email, "password": password},
    )
    if not isinstance(session, dict) or not session.get("access_token"):
        raise RuntimeError("Login did not return an active session")
    return str(session["access_token"])


def _active_local_profile(api_base: str, token: str) -> dict[str, Any]:
    """Select an active model profile backed by the local provider."""
    providers = request(api_base, "/model-providers", token=token)
    profiles = request(api_base, "/model-profiles", token=token)
    if not isinstance(providers, list) or not isinstance(profiles, list):
        raise RuntimeError("Model catalog endpoints returned an invalid response")
    provider_ids = {
        str(item["id"])
        for item in providers
        if item.get("provider_type") == "local" and item.get("status") == "active"
    }
    profile = next(
        (
            item
            for item in profiles
            if item.get("provider_id") in provider_ids
            and item.get("status") == "active"
        ),
        None,
    )
    if profile is None:
        raise RuntimeError(
            "No active local mock model profile is available for smoke run"
        )
    return profile


def _smoke_agent(api_base: str, token: str, profile_id: str) -> str:
    """Load or create the deployment smoke agent and return its ID."""
    agents = request(api_base, "/agents", token=token)
    if not isinstance(agents, list):
        raise RuntimeError("Agent list endpoint returned an invalid response")
    agent = next(
        (item for item in agents if item.get("name") == "Production E2E smoke"),
        None,
    )
    if agent is None:
        agent = request(
            api_base,
            "/agents",
            method="POST",
            token=token,
            payload={
                "name": "Production E2E smoke",
                "description": "Automated deployment smoke agent",
                "instructions": (
                    "Complete the requested task using the configured model."
                ),
                "model_profile_id": profile_id,
                "allowed_capability_ids": [],
                "max_tool_calls": 0,
            },
        )
    if not isinstance(agent, dict) or not agent.get("id"):
        raise RuntimeError("Could not create or load the deployment smoke agent")
    return str(agent["id"])


def _verify_smoke_run(api_base: str, token: str, agent_id: str) -> None:
    """Run the smoke agent and require a successful run with an artifact."""
    result = request(
        api_base,
        "/runs",
        method="POST",
        token=token,
        payload={
            "agent_id": agent_id,
            "task": "Confirm the end-to-end production run completed successfully.",
        },
    )
    if not isinstance(result, dict):
        raise RuntimeError("Run endpoint returned an invalid response")
    run = result.get("run", {})
    artifacts = result.get("artifacts", [])
    if run.get("status") != "succeeded" or not artifacts:
        raise RuntimeError(
            f"Production run did not succeed or create an artifact: {run.get('status')}"
        )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"FAIL deployment smoke: {error}", file=sys.stderr)
        raise SystemExit(1) from error
