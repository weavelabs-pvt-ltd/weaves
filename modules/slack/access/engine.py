"""The Slack access policy engine: one pure function, ``evaluate``.

Check order (design 3.5 and 3.9), each step only reached if the previous one
passed:

1. platform and organization state, including the ``read_only`` flag;
2. the Slack installation;
3. the bot's own ``paused`` switch;
4. Ask rate limits and question length;
5. the tier rule: Ask on/off, or the Execute rule for the action, then the hard
   checks (mapped active Weaves member holding the Weaves permission, the repo
   allowlist for ``pr_review``, the workflow allowlist for ``run_workflow``);
6. per needed tool: connector health and, for Ask, the connector's audience.

The engine does no I/O and reads no clock. It sees only the asker's own
standing on the allowlists (two booleans), never the lists, so no user-facing
text can name who is on one. User text comes from ``messages`` only.
"""

from __future__ import annotations

from dataclasses import replace

from modules.slack.access import messages
from modules.slack.access.models import (
    ACCESS_REASONS,
    AccessRequest,
    Action,
    Decision,
    InstallationState,
    Mode,
    OrgState,
    Outcome,
    Reason,
    ToolHealth,
    ToolResult,
)
from modules.slack.access.ratelimit import check_ask_limits

_PERMISSIONS = {
    Action.CREATE_TASK: frozenset({"tasks.create", "agents.run"}),
    Action.PR_REVIEW: frozenset({"tasks.create", "agents.run"}),
    Action.RUN_WORKFLOW: frozenset({"workflows.run"}),
}
_HEALTH_REASON = {
    ToolHealth.ERROR: Reason.TOOL_UNAVAILABLE,
    ToolHealth.STALE: Reason.TOOL_UNAVAILABLE,
    ToolHealth.UNAVAILABLE: Reason.TOOL_UNAVAILABLE,
    ToolHealth.DISABLED: Reason.TOOL_DISABLED,
    ToolHealth.TOKEN_EXPIRED: Reason.TOOL_TOKEN_EXPIRED,
    ToolHealth.QUARANTINED: Reason.TOOL_QUARANTINED,
}


def rule_failure(
    rule: str, *, user_listed: bool, channel_listed: bool, channel_id: str
) -> Reason | None:
    """Return why the rule refuses the asker, or ``None`` when it passes.

    A direct message (channel id starting with ``D``) never satisfies a channel.
    """
    channel_ok = channel_listed and not channel_id.startswith("D")
    if rule == "user_only":
        return None if user_listed else Reason.USER_NOT_LISTED
    if rule == "channel_only":
        return None if channel_ok else Reason.CHANNEL_NOT_LISTED
    if rule == "user_and_channel":
        if not user_listed:
            return Reason.USER_NOT_LISTED
        return None if channel_ok else Reason.CHANNEL_NOT_LISTED
    if rule == "user_or_channel":
        return None if (user_listed or channel_ok) else Reason.USER_NOT_LISTED
    return Reason.RULE_OFF  # "off" and anything unknown fail closed


def _decision(
    request: AccessRequest,
    outcome: Outcome,
    reason: Reason | None,
    checks: list[str],
    *,
    message: str | None = None,
    retry: int | None = None,
    mode: Mode = Mode.STRICT,
) -> Decision:
    """Build a refusal whose text is the reason's fixed text unless overridden."""
    text = (
        message if message is not None else (messages.fixed(reason) if reason else None)
    )
    return Decision(
        allowed=False,
        outcome=outcome,
        action=request.action,
        mode=mode,
        settings_version=request.settings.version,
        reason=reason,
        message=text,
        retry_after_seconds=retry,
        checks=tuple(checks),
    )


def _state_gate(request: AccessRequest, checks: list[str]) -> Decision | None:
    platform = request.platform
    checks.append("state")
    if not platform.slack_enabled:
        return _decision(request, Outcome.DROPPED, Reason.SLACK_DISABLED, checks)
    if platform.admissions_paused:
        return _decision(request, Outcome.BLOCKED_STATE, Reason.PLATFORM_PAUSED, checks)
    if request.org_state is not OrgState.ACTIVE:
        return _decision(request, Outcome.BLOCKED_STATE, Reason.ORG_UNAVAILABLE, checks)
    if platform.read_only and request.action.is_execute:
        return _decision(request, Outcome.BLOCKED_STATE, Reason.READ_ONLY, checks)
    checks.append("installation")
    if request.installation is not InstallationState.ACTIVE:
        return _decision(request, Outcome.DROPPED, Reason.INSTALLATION_INACTIVE, checks)
    checks.append("paused")
    if request.settings.paused:
        return _decision(request, Outcome.BLOCKED_STATE, Reason.BOT_PAUSED, checks)
    return None


def _limits_gate(request: AccessRequest, checks: list[str]) -> Decision | None:
    if not request.action.is_ask:
        return None
    checks.append("limits")
    limits = request.settings.ask_limits
    if request.question_chars > limits.max_question_chars:
        return _decision(
            request,
            Outcome.RATE_LIMITED,
            Reason.QUESTION_TOO_LONG,
            checks,
            message=messages.question_too_long(limits.max_question_chars),
        )
    if request.ask_usage is None:
        raise ValueError("Ask requests must supply ask_usage")
    verdict = check_ask_limits(limits, request.ask_usage(), request.now)
    if verdict.reason is not None:
        return _decision(
            request,
            Outcome.RATE_LIMITED,
            verdict.reason,
            checks,
            message=messages.rate_limited(verdict.retry_after_seconds),
            retry=verdict.retry_after_seconds,
        )
    return None


def _rules_for(request: AccessRequest) -> str:
    rules = request.settings.rules
    return {
        Action.ASK_PRIVATE: rules.ask_private,
        Action.CREATE_TASK: rules.create_task,
        Action.PR_REVIEW: rules.pr_review,
        Action.RUN_WORKFLOW: rules.run_workflow,
    }.get(request.action, "off")


def _private_rule_ok(request: AccessRequest) -> bool:
    return (
        rule_failure(
            request.settings.rules.ask_private,
            user_listed=request.user_listed,
            channel_listed=request.channel_listed,
            channel_id=request.channel_id,
        )
        is None
    )


def _tier_gate(request: AccessRequest, checks: list[str]) -> Decision | None:
    checks.append("tier")
    if request.action is Action.ASK:
        if not request.settings.ask_enabled:
            return _decision(request, Outcome.BLOCKED_RULE, Reason.ASK_DISABLED, checks)
        return None
    failure = rule_failure(
        _rules_for(request),
        user_listed=request.user_listed,
        channel_listed=request.channel_listed,
        channel_id=request.channel_id,
    )
    if failure is not None:
        return _decision(request, Outcome.BLOCKED_RULE, failure, checks)
    return None


def _member_gate(request: AccessRequest) -> Reason | None:
    member = request.member
    if member is None:
        return Reason.MEMBER_NOT_MAPPED
    if not member.active:
        return Reason.MEMBER_INACTIVE
    if not _PERMISSIONS[request.action] <= member.permissions:
        return Reason.PERMISSION_MISSING
    return None


def _target_gate(request: AccessRequest) -> Reason | None:
    settings = request.settings
    if request.action is Action.PR_REVIEW:
        if request.repo is None:
            return Reason.TARGET_MISSING
        if not request.repo.covered:
            return Reason.REPO_NOT_COVERED
        if request.repo.full_name.lower() not in settings.pr_review_repos:
            return Reason.REPO_NOT_ALLOWED
    if request.action is Action.RUN_WORKFLOW:
        if request.workflow is None:
            return Reason.TARGET_MISSING
        if not request.workflow.published:
            return Reason.WORKFLOW_NOT_PUBLISHED
        if request.workflow.workflow_id not in settings.workflow_ids:
            return Reason.WORKFLOW_NOT_ALLOWED
    return None


def _preflight_gate(request: AccessRequest, checks: list[str]) -> Decision | None:
    if not request.action.is_execute:
        return None
    checks.append("preflight")
    failure = _member_gate(request) or _target_gate(request)
    if failure is not None:
        return _decision(request, Outcome.BLOCKED_RULE, failure, checks)
    return None


def _tool_result(request: AccessRequest, tool_index: int) -> ToolResult:
    tool = request.tools[tool_index]
    reason: Reason | None = None
    if request.action.is_ask:
        audience = request.settings.audience_of(tool.installation_id)
        if audience == "not_in_slack":
            reason = Reason.AUDIENCE_NOT_IN_SLACK
        elif (
            audience == "allowed_people"
            and request.action is Action.ASK
            and not _private_rule_ok(request)
        ):
            reason = Reason.AUDIENCE_NOT_ALLOWED
    if reason is None and tool.health is not ToolHealth.HEALTHY:
        reason = _HEALTH_REASON[tool.health]
    return ToolResult(
        tool.tool_id, tool.installation_id, tool.label, reason is None, reason
    )


def _refuse_tool(
    request: AccessRequest,
    results: tuple[ToolResult, ...],
    checks: list[str],
    mode: Mode,
) -> Decision:
    """Refuse on the first red tool, access failures before health ones."""
    red = [r for r in results if not r.green and r.reason is not None]
    red.sort(key=lambda r: r.reason not in ACCESS_REASONS)
    first = red[0]
    assert first.reason is not None
    outcome = (
        Outcome.REFUSED_ACCESS
        if first.reason in ACCESS_REASONS
        else Outcome.REFUSED_UNAVAILABLE
    )
    decision = _decision(
        request,
        outcome,
        first.reason,
        checks,
        message=messages.tool_red(first.label, first.reason),
        mode=mode,
    )
    return _with_results(decision, results)


def _with_results(decision: Decision, results: tuple[ToolResult, ...]) -> Decision:
    return replace(decision, tool_results=results)


def _tools_gate(request: AccessRequest, checks: list[str]) -> Decision:
    checks.append("tools")
    mode = (
        Mode.PARTIAL
        if request.settings.partial_answers and request.action is Action.ASK
        else Mode.STRICT
    )
    results = tuple(_tool_result(request, i) for i in range(len(request.tools)))
    green = tuple(r.tool_id for r in results if r.green)
    red = [r for r in results if not r.green]
    ephemeral = request.action is Action.ASK_PRIVATE or any(
        r.green and request.settings.audience_of(r.installation_id) == "allowed_people"
        for r in results
        if request.action.is_ask
    )
    if not results:
        outcome = Outcome.MODEL_ONLY if request.action.is_ask else Outcome.ALLOWED
        return _allowed(request, outcome, mode, (), results, None, ephemeral, checks)
    if not red:
        return _allowed(
            request, Outcome.ALLOWED, mode, green, results, None, ephemeral, checks
        )
    if mode is Mode.PARTIAL and green:
        note = messages.skipped_note(
            [(r.label, r.reason) for r in red if r.reason is not None]
        )
        return _allowed(
            request,
            Outcome.ALLOWED_PARTIAL,
            mode,
            green,
            results,
            note,
            ephemeral,
            checks,
        )
    return _refuse_tool(request, results, checks, mode)


def _allowed(
    request: AccessRequest,
    outcome: Outcome,
    mode: Mode,
    green: tuple[str, ...],
    results: tuple[ToolResult, ...],
    note: str | None,
    ephemeral: bool,
    checks: list[str],
) -> Decision:
    return Decision(
        allowed=True,
        outcome=outcome,
        action=request.action,
        mode=mode,
        settings_version=request.settings.version,
        green_tools=green,
        tool_results=results,
        skipped_note=note,
        answer_ephemeral=ephemeral,
        checks=tuple(checks),
    )


def check_state(request: AccessRequest) -> Decision | None:
    """Only the platform, organization, installation and pause gates (cheap exits).

    ``None`` means the state allows the bot to speak; it never looks at limits or
    rules, so a help or greeting reply is not refused for a user's Ask quota.
    """
    return _state_gate(request, [])


def evaluate(request: AccessRequest) -> Decision:
    """Decide one Slack request; see the module docstring for the check order."""
    checks: list[str] = []
    return (
        _state_gate(request, checks)
        or _limits_gate(request, checks)
        or _tier_gate(request, checks)
        or _preflight_gate(request, checks)
        or _tools_gate(request, checks)
    )
