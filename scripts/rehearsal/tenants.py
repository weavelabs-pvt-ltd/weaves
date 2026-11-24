"""Synthetic tenants driven only through real operator tools and HTTP routes."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from rehearsal.stack import HERE, OPERATOR_DIR, RehearsalError, Stack


@dataclass
class Tenant:
    """One organization with an account session and the records written for it."""

    label: str
    email: str
    org_id: str = ""
    workspace_id: str = ""
    token: str = ""
    account_id: str = ""
    agents: dict[str, str] = field(default_factory=dict)  # name -> id
    workflows: dict[str, str] = field(default_factory=dict)  # id -> agent id
    secrets: dict[str, str] = field(default_factory=dict)  # ref -> value


def call(
    stack: Stack,
    tenant: Tenant,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    expected: int | tuple[int, ...] = (200, 201),
) -> Any:
    """Call the real API as the tenant's bearer session; never log the token."""
    headers = {
        "Authorization": "Bearer " + tenant.token,
        "Content-Type": "application/json",
    }
    if tenant.org_id:
        headers["X-Organization-Id"] = tenant.org_id
    if tenant.workspace_id:
        headers["X-Workspace-ID"] = tenant.workspace_id
    request = urllib.request.Request(
        f"http://127.0.0.1:{stack.api_port}/api/v0{path}",
        method=method,
        headers=headers,
        data=None if body is None else json.dumps(body).encode(),
    )
    wanted = (expected,) if isinstance(expected, int) else expected
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, b""
    if status not in wanted:
        raise RehearsalError(f"{method} {path}: HTTP {status}, expected {wanted}")
    return json.loads(raw) if raw else None


def _operator(stack: Stack, role: str, script: str, *args: str) -> str:
    return stack.exec_script(role, OPERATOR_DIR / script, *args)


def provision(stack: Stack, role: str, tenant: Tenant) -> None:
    """Create the account, organization and a bearer session through operator tools."""
    _operator(
        stack, role, "bootstrap_account_owner.py",
        "--email", tenant.email, "--organization-name", f"Rehearsal {tenant.label}",
    )  # fmt: skip
    tenant.token = issue_session(stack, role, tenant)
    me = _account(stack, tenant)
    tenant.org_id = me["memberships"][0]["organization_id"]
    tenant.account_id = me["account"]["id"]
    workspaces = call(stack, tenant, "GET", "/workspaces")
    tenant.workspace_id = workspaces[0]["id"]


def issue_session(stack: Stack, role: str, tenant: Tenant) -> str:
    """Issue a fresh bearer session (the operator tool prints the token once)."""
    return _operator(
        stack, role, "issue_account_session.py",
        "--email", tenant.email, "--minutes", "120",
    )  # fmt: skip


def _account(stack: Stack, tenant: Tenant) -> Any:
    return call(stack, tenant, "GET", "/account/me")


def write_records(
    stack: Stack, role: str, tenant: Tenant, prefix: str, count: int
) -> None:
    """Create agents, workflows that reference them, and one encrypted secret."""
    for number in range(count):
        name = f"{prefix}-agent-{number}"
        body = {
            "name": name,
            "instructions": f"{tenant.label} {name} instructions",
            "description": f"{tenant.label} rehearsal record",
            "allowed_capability_ids": [],
        }
        agent = call(stack, tenant, "POST", "/agents", body)
        tenant.agents[name] = agent["id"]
        if number % 2 == 0:
            flow = {"name": f"{prefix}-flow-{number}", "agent_id": agent["id"]}
            made = call(stack, tenant, "POST", "/workflows", flow)
            tenant.workflows[made["definition"]["workflow_id"]] = agent["id"]
    value = f"rehearsal-secret-{tenant.label}-{prefix}"
    probe = HERE / "secret_probe.py"
    ref = stack.exec_script(
        role, probe, "put", tenant.org_id, tenant.workspace_id, value
    )
    tenant.secrets[ref] = value


def verify_records(stack: Stack, role: str, tenant: Tenant) -> list[str]:
    """Read everything written so far; return a list of problems (empty is good)."""
    problems: list[str] = []
    if _account(stack, tenant)["account"]["id"] != tenant.account_id:
        problems.append("session resolved to a different account")
    for name, agent_id in tenant.agents.items():
        agent = call(stack, tenant, "GET", f"/agents/{agent_id}", expected=(200, 404))
        if agent is None or agent.get("name") != name:
            problems.append(f"{tenant.label}: agent {name} is not readable")
    for workflow_id, agent_id in tenant.workflows.items():
        detail = call(
            stack, tenant, "GET", f"/workflows/{workflow_id}", expected=(200, 404)
        )
        if detail is None or agent_id not in json.dumps(detail):
            problems.append(
                f"{tenant.label}: workflow {workflow_id[:8]} lost its agent"
            )
    probe = HERE / "secret_probe.py"
    for ref, value in tenant.secrets.items():
        got = stack.exec_script(
            role, probe, "check", tenant.org_id, tenant.workspace_id, ref, value
        )
        if got != "ok":
            problems.append(f"{tenant.label}: encrypted secret unreadable ({got})")
    return problems


def check_isolation(stack: Stack, tenant: Tenant, others: list[Tenant]) -> list[str]:
    """No other tenant's agent may appear in this tenant's list."""
    listing = call(stack, tenant, "GET", "/agents?limit=100")
    text = json.dumps(listing)
    leaked = [i for other in others for i in other.agents.values()]
    return (
        [f"{tenant.label}: another tenant's agent is visible"]
        if any(i in text for i in leaked)
        else []
    )
