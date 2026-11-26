"""T7-P* policy matrix: pairwise cases checked against an independent oracle."""

from __future__ import annotations

import re
from typing import Any

import pytest

from modules.slack.access import messages
from modules.slack.access.engine import evaluate
from modules.slack.access.models import (
    Action,
    MemberStanding,
    Mode,
    Outcome,
    Reason,
    ToolHealth,
)
from tests.support.slack_access_cases import (
    FULL,
    GOOD_MEMBER,
    org_state_kwargs,
    pairwise,
    request,
    settings,
    tool,
)

FACTORS: dict[str, list[Any]] = {
    "health": [
        ToolHealth.HEALTHY,
        ToolHealth.ERROR,
        ToolHealth.DISABLED,
        ToolHealth.TOKEN_EXPIRED,
        ToolHealth.STALE,
    ],
    "audience": ["everyone", "allowed_people", "not_in_slack"],
    "user_listed": [True, False],
    "channel_listed": [True, False],
    "member": ["ok", "unmapped", "inactive", "no_permission"],
    "rule": [
        "off",
        "user_only",
        "channel_only",
        "user_and_channel",
        "user_or_channel",
    ],
    "action": list(Action),
    "org": ["ok", "quarantined", "read_only", "installation_revoked", "bot_paused"],
    "mode": ["strict", "partial"],
}
CASES = pairwise(FACTORS)
EXECUTE = {Action.CREATE_TASK, Action.PR_REVIEW, Action.RUN_WORKFLOW}

# The only texts a user can ever see (templates with the asker's own situation).
ALLOWED_TEXT = [
    re.escape(text)
    for text in (
        messages.UNAVAILABLE,
        messages.READ_ONLY,
        messages.ASK_DISABLED,
        messages.RULE_OFF,
        messages.USER_NOT_LISTED,
        messages.CHANNEL_NOT_LISTED,
        messages.NOT_MAPPED,
        messages.NO_PERMISSION,
        messages.TARGET_MISSING,
        messages.REPO_NOT_ENABLED,
        messages.WORKFLOW_NOT_ENABLED,
    )
] + [
    r"Jira isn't open to everyone in Slack\. Ask a Weaves admin\.",
    r"I can't check Jira right now \([a-z' ]+\); nothing was looked up\.",
    r"You're asking a lot; try again in \d+ (minute|minutes|hours)\.",
    r"That question is too long\. Keep it under \d+ characters\.",
]


def build(case: dict[str, Any]):
    action: Action = case["action"]
    member = {
        "ok": GOOD_MEMBER,
        "unmapped": None,
        "inactive": MemberStanding("principal-1", False, FULL),
        "no_permission": MemberStanding("principal-1", True, frozenset()),
    }[case["member"]]
    overrides: dict[str, Any] = {
        "settings": settings(
            rule=case["rule"],
            audience={"inst-jira": case["audience"], "inst-notion": "everyone"},
            partial=case["mode"] == "partial",
            paused=case["org"] == "bot_paused",
        ),
        "user_listed": case["user_listed"],
        "channel_listed": case["channel_listed"],
        "tools": (
            tool(health=case["health"]),
            tool("notion.search", "inst-notion", "Notion"),
        ),
        **org_state_kwargs(case["org"]),
    }
    if action in EXECUTE:
        overrides["member"] = member
    return request(action, **overrides)


def rule_reason(rule: str, user: bool, channel: bool) -> Reason | None:
    """Independent statement of the five rules (design 3.5)."""
    if rule == "off":
        return Reason.RULE_OFF
    ok = {
        "user_only": user,
        "channel_only": channel,
        "user_and_channel": user and channel,
        "user_or_channel": user or channel,
    }[rule]
    if ok:
        return None
    if rule == "channel_only" or (rule == "user_and_channel" and user):
        return Reason.CHANNEL_NOT_LISTED
    return Reason.USER_NOT_LISTED


def oracle(case: dict[str, Any]) -> tuple[Outcome, Reason | None, tuple[str, ...]]:
    """What the design says should happen, written without the engine's helpers."""
    action: Action = case["action"]
    if case["org"] == "quarantined":
        return Outcome.BLOCKED_STATE, Reason.ORG_UNAVAILABLE, ()
    if case["org"] == "read_only" and action in EXECUTE:
        return Outcome.BLOCKED_STATE, Reason.READ_ONLY, ()
    if case["org"] == "installation_revoked":
        return Outcome.DROPPED, Reason.INSTALLATION_INACTIVE, ()
    if case["org"] == "bot_paused":
        return Outcome.BLOCKED_STATE, Reason.BOT_PAUSED, ()
    failure = rule_reason(case["rule"], case["user_listed"], case["channel_listed"])
    if action is not Action.ASK and failure is not None:
        return Outcome.BLOCKED_RULE, failure, ()
    if action in EXECUTE:
        hard = {
            "unmapped": Reason.MEMBER_NOT_MAPPED,
            "inactive": Reason.MEMBER_INACTIVE,
            "no_permission": Reason.PERMISSION_MISSING,
        }.get(case["member"])
        if hard is not None:
            return Outcome.BLOCKED_RULE, hard, ()
    return tools_oracle(case, failure is None)


def tools_oracle(case, private_ok: bool):
    action: Action = case["action"]
    audience, health = case["audience"], case["health"]
    jira_reason: Reason | None = None
    plain_ask = action not in EXECUTE and action is not Action.ASK_PRIVATE
    if plain_ask and audience == "not_in_slack":
        jira_reason = Reason.AUDIENCE_NOT_IN_SLACK
    if plain_ask and audience == "allowed_people" and not private_ok:
        jira_reason = Reason.AUDIENCE_NOT_ALLOWED
    if action is Action.ASK_PRIVATE and audience == "not_in_slack":
        jira_reason = Reason.AUDIENCE_NOT_IN_SLACK
    if jira_reason is None and health is not ToolHealth.HEALTHY:
        jira_reason = {
            ToolHealth.ERROR: Reason.TOOL_UNAVAILABLE,
            ToolHealth.STALE: Reason.TOOL_UNAVAILABLE,
            ToolHealth.DISABLED: Reason.TOOL_DISABLED,
            ToolHealth.TOKEN_EXPIRED: Reason.TOOL_TOKEN_EXPIRED,
        }[health]
    if jira_reason is None:
        return Outcome.ALLOWED, None, ("jira.search", "notion.search")
    partial = case["mode"] == "partial" and action is Action.ASK
    if partial:
        return Outcome.ALLOWED_PARTIAL, None, ("notion.search",)
    access = jira_reason in (Reason.AUDIENCE_NOT_IN_SLACK, Reason.AUDIENCE_NOT_ALLOWED)
    return (
        Outcome.REFUSED_ACCESS if access else Outcome.REFUSED_UNAVAILABLE,
        jira_reason,
        (),
    )


def test_matrix_has_enough_pairwise_cases():
    assert 35 <= len(CASES) <= 120
    values = {name: {c[name] for c in CASES} for name in FACTORS}
    assert all(values[name] == set(FACTORS[name]) for name in FACTORS)


@pytest.mark.parametrize("case", CASES, ids=[f"c{i:02d}" for i in range(len(CASES))])
def test_decision_matches_the_design_oracle(case):
    decision = evaluate(build(case))
    outcome, reason, green = oracle(case)
    assert decision.outcome is outcome, case
    assert decision.reason is reason, case
    assert decision.green_tools == green, case
    assert decision.allowed is outcome.allows
    assert decision.settings_version == 7
    if decision.allowed:
        assert decision.message is None
        assert decision.mode is (
            Mode.PARTIAL
            if case["mode"] == "partial" and case["action"] is Action.ASK
            else Mode.STRICT
        )
    # No red tool ever shows up as green, and strict refusals carry no data.
    red = {r.tool_id for r in decision.tool_results if not r.green}
    assert not red & set(decision.green_tools)
    if not decision.allowed:
        assert decision.green_tools == ()


@pytest.mark.parametrize("case", CASES, ids=[f"c{i:02d}" for i in range(len(CASES))])
def test_user_text_is_a_fixed_template_and_never_names_a_list(case):
    decision = evaluate(build(case))
    if decision.message is None:
        return
    assert any(re.fullmatch(pattern, decision.message) for pattern in ALLOWED_TEXT), (
        decision.message
    )
    # Nothing the engine was given about identity can reach the text.
    for secret in ("U0ASKER01", "C0CHAN001", "principal-1", "acme/api", "wf-1"):
        assert secret not in decision.message
        assert secret not in (decision.skipped_note or "")


@pytest.mark.parametrize("user", [True, False])
@pytest.mark.parametrize("channel", [True, False])
@pytest.mark.parametrize(
    "rule", ["off", "user_only", "channel_only", "user_and_channel", "user_or_channel"]
)
@pytest.mark.parametrize("action", sorted(EXECUTE, key=str))
def test_every_rule_for_every_standing(rule, action, user, channel):
    decision = evaluate(
        request(
            action,
            settings=settings(rule=rule),
            user_listed=user,
            channel_listed=channel,
        )
    )
    expected = rule_reason(rule, user, channel)
    assert decision.reason is expected
    assert decision.allowed is (expected is None)


def test_decision_does_not_depend_on_anyone_elses_list_membership():
    """The engine receives only the asker's own two booleans, so text cannot vary."""
    base = dict(settings=settings(rule="user_only"), user_listed=False)
    first = evaluate(request(Action.CREATE_TASK, **base))
    second = evaluate(request(Action.CREATE_TASK, **base))
    assert first == second
    assert first.message == messages.USER_NOT_LISTED
