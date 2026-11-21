"""Exercise the deployed agent → task worker → transcript path.

An account bearer session (``WEAVES_E2E_TOKEN``) and its organization
(``WEAVES_E2E_ORGANIZATION_ID``) come from the environment, for example from the
operator break-glass script. This script never prints tokens.
"""

from __future__ import annotations

import json
import os
import sys
import time
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
        organization_id = os.environ.get("WEAVES_E2E_ORGANIZATION_ID", "")
        if organization_id:
            headers["X-Organization-Id"] = organization_id
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
    token = _verified_session(api_base, os.environ["WEAVES_E2E_TOKEN"])
    profile = _active_local_profile(api_base, token)
    agent_id = _smoke_agent(api_base, token, str(profile["id"]))
    _verify_smoke_run(api_base, token, agent_id)
    print("PASS health, account session, agent, task worker and transcript")
    return 0


def _verified_session(api_base: str, token: str) -> str:
    """Verify health and that the supplied session resolves to an identity."""
    health = request(api_base, "/health")
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise RuntimeError("API health check did not return status=ok")
    identity = request(api_base, "/auth/me", token=token)
    if not isinstance(identity, dict) or not identity.get("principal"):
        raise RuntimeError("The supplied session did not resolve to a principal")
    return token


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
        (item for item in agents if item.get("name") == "deployment-smoke"),
        None,
    )
    if agent is None:
        agent = request(
            api_base,
            "/agents",
            method="POST",
            token=token,
            payload={
                "name": "deployment-smoke",
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
    """Queue a real task and verify the deployed worker, transcript and artifact."""
    task = request(
        api_base,
        "/tasks",
        method="POST",
        token=token,
        payload={
            "agent_id": agent_id,
            "title": "Deployment verification",
            "initial_message": "Confirm this deployment can execute a task.",
        },
    )
    if not isinstance(task, dict) or not task.get("task_id"):
        raise RuntimeError("Task creation did not return a task ID")
    task_id = task["task_id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = request(api_base, f"/tasks/{task_id}", token=token)
        if not isinstance(current, dict):
            raise RuntimeError("Task detail returned an invalid response")
        status = current.get("latest_turn_status")
        if status == "succeeded":
            _verify_task_evidence(api_base, token, agent_id, task_id)
            return
        if status in {"failed", "cancelled"}:
            raise RuntimeError(f"Deployment task ended with status={status}")
        time.sleep(2)
    raise RuntimeError("Worker did not finish the deployment task within 120 seconds")


def _verify_task_evidence(
    api_base: str, token: str, agent_id: str, task_id: str
) -> None:
    """Require a persisted assistant message and execution ID for the exact task."""
    messages = request(api_base, f"/tasks/{task_id}/messages", token=token)
    if not isinstance(messages, dict) or not any(
        item.get("role") == "assistant" and item.get("content")
        for item in messages.get("items", [])
    ):
        raise RuntimeError("Completed task has no persisted assistant message")
    tasks = request(
        api_base,
        f"/tasks?agent_id={agent_id}&include_execution=true&limit=100",
        token=token,
    )
    if not isinstance(tasks, dict):
        raise RuntimeError("Task execution summary returned an invalid response")
    row = next(item for item in tasks["items"] if item["task_id"] == task_id)
    run_id = row.get("execution", {}).get("agent_run_id")
    if not run_id:
        raise RuntimeError("Completed task has no persisted execution ID")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"FAIL deployment smoke: {error}", file=sys.stderr)
        raise SystemExit(1) from error
