"""Builders and a pairwise generator for the Slack access policy tests."""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from contracts.v1.slack_access import AskLimits, ExecuteRules
from modules.slack.access.models import (
    AccessRequest,
    Action,
    AskUsage,
    InstallationState,
    MemberStanding,
    NeededTool,
    OrgState,
    PlatformState,
    PolicySettings,
    RepoTarget,
    ToolHealth,
    WorkflowTarget,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
USER = "U0ASKER01"
CHANNEL = "C0CHAN001"
REPO = "acme/api"
WORKFLOW = "wf-1"
FULL = frozenset({"tasks.create", "agents.run", "workflows.run"})
GOOD_MEMBER = MemberStanding("principal-1", True, FULL)


def tool(
    tool_id: str = "jira.search",
    installation: str = "inst-jira",
    label: str = "Jira",
    health: ToolHealth = ToolHealth.HEALTHY,
) -> NeededTool:
    """One needed connector."""
    return NeededTool(tool_id, installation, label, health)


def settings(
    *,
    rule: str = "off",
    audience: Mapping[str, str] | None = None,
    partial: bool = False,
    ask_enabled: bool = True,
    paused: bool = False,
    limits: AskLimits | None = None,
    repos: Sequence[str] = (REPO,),
    workflows: Sequence[str] = (WORKFLOW,),
    version: int = 7,
) -> PolicySettings:
    """Settings with one rule applied to every action."""
    return PolicySettings(
        version=version,
        ask_enabled=ask_enabled,
        connector_audience=dict(audience or {}),  # type: ignore[arg-type]
        partial_answers=partial,
        ask_limits=limits or AskLimits(),
        rules=ExecuteRules(
            create_task=rule,  # type: ignore[arg-type]
            pr_review=rule,  # type: ignore[arg-type]
            run_workflow=rule,  # type: ignore[arg-type]
            ask_private=rule,  # type: ignore[arg-type]
        ),
        workflow_ids=frozenset(workflows),
        pr_review_repos=frozenset(repos),
        paused=paused,
    )


def request(action: Action = Action.ASK, **overrides: Any) -> AccessRequest:
    """A passing request; override anything to break one check."""
    base: dict[str, Any] = {
        "action": action,
        "now": NOW,
        "settings": settings(),
        "slack_user_id": USER,
        "channel_id": CHANNEL,
        "ask_usage": AskUsage,
    }
    if action.is_execute:
        base["member"] = GOOD_MEMBER
        base["repo"] = RepoTarget(REPO, True) if action is Action.PR_REVIEW else None
        base["workflow"] = (
            WorkflowTarget(WORKFLOW, True) if action is Action.RUN_WORKFLOW else None
        )
    base.update(overrides)
    return AccessRequest(**base)


def _gain(
    chosen: Mapping[int, Any], uncovered: set[tuple[Any, ...]], k: int, value: Any
) -> int:
    """How many still-uncovered pairs choosing ``value`` for factor ``k`` covers."""
    total = 0
    for m, other in chosen.items():
        p, q = sorted((k, m))
        values = {k: value, m: other}
        total += (p, values[p], q, values[q]) in uncovered
    return total


def pairwise(factors: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """A deterministic greedy pairwise (2-way) covering array."""
    names = list(factors)
    uncovered = {
        (i, a, j, b)
        for i, j in itertools.combinations(range(len(names)), 2)
        for a in factors[names[i]]
        for b in factors[names[j]]
    }
    rows: list[dict[str, Any]] = []
    while uncovered:
        i, a, j, b = min(uncovered, key=repr)
        chosen: dict[int, Any] = {i: a, j: b}
        for k in range(len(names)):
            if k in chosen:
                continue

            chosen[k] = max(
                factors[names[k]],
                key=lambda value, k=k: _gain(chosen, uncovered, k, value),
            )
        for p, q in itertools.combinations(sorted(chosen), 2):
            uncovered.discard((p, chosen[p], q, chosen[q]))
        rows.append({names[k]: chosen[k] for k in range(len(names))})
    return rows


def org_state_kwargs(label: str) -> dict[str, Any]:
    """Map a matrix org-state label to request keyword arguments."""
    if label == "quarantined":
        return {"org_state": OrgState.QUARANTINED}
    if label == "read_only":
        return {"platform": PlatformState(read_only=True)}
    if label == "installation_revoked":
        return {"installation": InstallationState.REVOKED}
    return {}
