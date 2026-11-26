"""Check order, Ask limits and the specific rules of design 3.5 and 3.9."""

from __future__ import annotations

from datetime import timedelta

import pytest

from contracts.v1.slack_access import AskLimits
from modules.slack.access import messages
from modules.slack.access.engine import evaluate
from modules.slack.access.models import (
    Action,
    AskUsage,
    InstallationState,
    MemberStanding,
    Mode,
    OrgState,
    Outcome,
    PlatformState,
    Reason,
    RepoTarget,
    ToolHealth,
    WorkflowTarget,
)
from modules.slack.access.ratelimit import check_ask_limits
from tests.support.slack_access_cases import (
    FULL,
    NOW,
    REPO,
    WORKFLOW,
    request,
    settings,
    tool,
)


def test_state_checks_come_before_everything_else():
    everything_wrong = dict(
        platform=PlatformState(slack_enabled=False, admissions_paused=True),
        org_state=OrgState.QUARANTINED,
        installation=InstallationState.REVOKED,
        settings=settings(paused=True, ask_enabled=False),
        question_chars=99999,
        ask_usage=lambda: AskUsage(user=(NOW,) * 50),
    )
    steps = [
        (Reason.SLACK_DISABLED, Outcome.DROPPED),
        (Reason.PLATFORM_PAUSED, Outcome.BLOCKED_STATE),
        (Reason.ORG_UNAVAILABLE, Outcome.BLOCKED_STATE),
        (Reason.INSTALLATION_INACTIVE, Outcome.DROPPED),
        (Reason.BOT_PAUSED, Outcome.BLOCKED_STATE),
        (Reason.QUESTION_TOO_LONG, Outcome.RATE_LIMITED),
        (Reason.RATE_LIMIT_USER, Outcome.RATE_LIMITED),
        (Reason.ASK_DISABLED, Outcome.BLOCKED_RULE),
    ]
    for reason, outcome in steps:
        decision = evaluate(request(Action.ASK, **everything_wrong))
        assert (decision.reason, decision.outcome) == (reason, outcome)
        # Fix the failing check and the next one shows.
        if reason is Reason.SLACK_DISABLED:
            everything_wrong["platform"] = PlatformState(admissions_paused=True)
        elif reason is Reason.PLATFORM_PAUSED:
            everything_wrong["platform"] = PlatformState()
        elif reason is Reason.ORG_UNAVAILABLE:
            everything_wrong["org_state"] = OrgState.ACTIVE
        elif reason is Reason.INSTALLATION_INACTIVE:
            everything_wrong["installation"] = InstallationState.ACTIVE
        elif reason is Reason.BOT_PAUSED:
            everything_wrong["settings"] = settings(ask_enabled=False)
        elif reason is Reason.QUESTION_TOO_LONG:
            everything_wrong["question_chars"] = 10
        elif reason is Reason.RATE_LIMIT_USER:
            everything_wrong["ask_usage"] = AskUsage


def test_dropped_decisions_say_nothing_and_blocked_ones_say_unavailable():
    for state in (InstallationState.UNINSTALLED, InstallationState.MISSING):
        assert evaluate(request(installation=state)).message is None
    assert (
        evaluate(request(platform=PlatformState(slack_enabled=False))).message is None
    )
    paused = evaluate(request(settings=settings(paused=True)))
    assert paused.message == messages.UNAVAILABLE
    quarantined = evaluate(request(org_state=OrgState.QUARANTINED))
    assert quarantined.message == messages.UNAVAILABLE


def test_read_only_allows_ask_and_refuses_execute():
    flag = PlatformState(read_only=True)
    assert evaluate(request(Action.ASK, platform=flag)).allowed
    assert evaluate(
        request(
            Action.ASK_PRIVATE,
            platform=flag,
            settings=settings(rule="user_only"),
            user_listed=True,
        )
    ).allowed
    blocked = evaluate(
        request(
            Action.CREATE_TASK,
            platform=flag,
            settings=settings(rule="user_only"),
            user_listed=True,
        )
    )
    assert blocked.reason is Reason.READ_ONLY
    assert blocked.message == messages.READ_ONLY


def test_ask_is_open_to_unlisted_unmapped_users_and_needs_no_rule():
    decision = evaluate(request(Action.ASK))
    assert decision.allowed and decision.outcome is Outcome.MODEL_ONLY
    assert decision.green_tools == () and not decision.answer_ephemeral


def test_ask_can_be_turned_off():
    decision = evaluate(request(Action.ASK, settings=settings(ask_enabled=False)))
    assert decision.reason is Reason.ASK_DISABLED
    assert decision.message == messages.ASK_DISABLED


def test_execute_defaults_to_off_and_names_no_one():
    decision = evaluate(
        request(Action.CREATE_TASK, user_listed=True, channel_listed=True)
    )
    assert decision.reason is Reason.RULE_OFF
    assert decision.message == messages.RULE_OFF


def test_dm_never_satisfies_a_channel_rule():
    kwargs = dict(settings=settings(rule="channel_only"), channel_listed=True)
    assert evaluate(
        request(Action.CREATE_TASK, channel_id="C0CHAN001", **kwargs)
    ).allowed
    dm = evaluate(request(Action.CREATE_TASK, channel_id="D0DIRECT1", **kwargs))
    assert dm.reason is Reason.CHANNEL_NOT_LISTED


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (None, Reason.MEMBER_NOT_MAPPED),
        (MemberStanding("p", False, FULL), Reason.MEMBER_INACTIVE),
        (
            MemberStanding("p", True, frozenset({"tasks.create"})),
            Reason.PERMISSION_MISSING,
        ),
        (
            MemberStanding("p", True, frozenset({"agents.run"})),
            Reason.PERMISSION_MISSING,
        ),
    ],
)
def test_listed_users_still_need_an_active_mapped_member_with_permissions(
    member, reason
):
    decision = evaluate(
        request(
            Action.CREATE_TASK,
            settings=settings(rule="user_only"),
            user_listed=True,
            member=member,
        )
    )
    assert decision.reason is reason and not decision.allowed


def test_run_workflow_needs_workflows_run_not_tasks_create():
    only_tasks = MemberStanding("p", True, frozenset({"tasks.create", "agents.run"}))
    only_workflow = MemberStanding("p", True, frozenset({"workflows.run"}))
    base = dict(settings=settings(rule="user_only"), user_listed=True)
    assert (
        evaluate(request(Action.RUN_WORKFLOW, member=only_tasks, **base)).reason
        is Reason.PERMISSION_MISSING
    )
    assert evaluate(request(Action.RUN_WORKFLOW, member=only_workflow, **base)).allowed
    assert (
        evaluate(request(Action.CREATE_TASK, member=only_workflow, **base)).reason
        is Reason.PERMISSION_MISSING
    )


def test_pr_review_repo_checks_cover_then_allowlist_then_text_is_generic():
    base = dict(settings=settings(rule="user_only"), user_listed=True)
    missing = evaluate(request(Action.PR_REVIEW, repo=None, **base))
    assert missing.reason is Reason.TARGET_MISSING
    uncovered = evaluate(
        request(Action.PR_REVIEW, repo=RepoTarget(REPO, False), **base)
    )
    assert uncovered.reason is Reason.REPO_NOT_COVERED
    unlisted = evaluate(
        request(Action.PR_REVIEW, repo=RepoTarget("acme/other", True), **base)
    )
    assert unlisted.reason is Reason.REPO_NOT_ALLOWED
    assert uncovered.message == unlisted.message == messages.REPO_NOT_ENABLED
    assert evaluate(
        request(Action.PR_REVIEW, repo=RepoTarget("ACME/API", True), **base)
    ).allowed


def test_run_workflow_must_be_published_and_allowlisted():
    base = dict(settings=settings(rule="user_only"), user_listed=True)
    draft = evaluate(
        request(Action.RUN_WORKFLOW, workflow=WorkflowTarget(WORKFLOW, False), **base)
    )
    other = evaluate(
        request(Action.RUN_WORKFLOW, workflow=WorkflowTarget("wf-9", True), **base)
    )
    assert draft.reason is Reason.WORKFLOW_NOT_PUBLISHED
    assert other.reason is Reason.WORKFLOW_NOT_ALLOWED
    assert draft.message == other.message == messages.WORKFLOW_NOT_ENABLED


def test_rule_comes_before_member_and_member_before_repo():
    """An unlisted, unmapped asker learns only the first failing check."""
    decision = evaluate(
        request(
            Action.PR_REVIEW,
            settings=settings(rule="user_only"),
            user_listed=False,
            member=None,
            repo=RepoTarget("acme/other", False),
        )
    )
    assert decision.reason is Reason.USER_NOT_LISTED
    decision = evaluate(
        request(
            Action.PR_REVIEW,
            settings=settings(rule="user_only"),
            user_listed=True,
            member=None,
            repo=RepoTarget("acme/other", False),
        )
    )
    assert decision.reason is Reason.MEMBER_NOT_MAPPED


def test_audience_rules_for_ask():
    red = tool(health=ToolHealth.HEALTHY)
    everyone = evaluate(
        request(settings=settings(audience={"inst-jira": "everyone"}), tools=(red,))
    )
    assert (
        everyone.allowed
        and everyone.green_tools == ("jira.search",)
        and not everyone.answer_ephemeral
    )
    default = evaluate(request(tools=(red,)))
    assert default.reason is Reason.AUDIENCE_NOT_IN_SLACK
    assert (
        default.message == "Jira isn't open to everyone in Slack. Ask a Weaves admin."
    )
    private = settings(rule="user_only", audience={"inst-jira": "allowed_people"})
    listed = evaluate(request(settings=private, tools=(red,), user_listed=True))
    assert listed.allowed and listed.answer_ephemeral
    unlisted = evaluate(request(settings=private, tools=(red,), user_listed=False))
    assert unlisted.reason is Reason.AUDIENCE_NOT_ALLOWED
    # The two refusals read the same, so a connector's audience type is not probed.
    assert unlisted.message == default.message


def test_access_failure_hides_the_connectors_health():
    sick = tool(health=ToolHealth.ERROR)
    decision = evaluate(request(tools=(sick,)))  # default audience: not_in_slack
    assert decision.reason is Reason.AUDIENCE_NOT_IN_SLACK
    assert "unavailable" not in decision.message


def test_strict_refuses_with_no_data_and_partial_names_what_was_skipped():
    sources = (
        tool(health=ToolHealth.ERROR),
        tool("notion.search", "inst-notion", "Notion"),
    )
    audience = {"inst-jira": "everyone", "inst-notion": "everyone"}
    strict = evaluate(request(settings=settings(audience=audience), tools=sources))
    assert strict.outcome is Outcome.REFUSED_UNAVAILABLE
    assert strict.green_tools == () and strict.mode is Mode.STRICT
    assert (
        strict.message
        == "I can't check Jira right now (it's unavailable); nothing was looked up."
    )
    partial = evaluate(
        request(settings=settings(audience=audience, partial=True), tools=sources)
    )
    assert partial.outcome is Outcome.ALLOWED_PARTIAL
    assert partial.green_tools == ("notion.search",)
    assert partial.skipped_note == "Skipped: Jira (it's unavailable)"
    assert partial.skipped_tools == ("jira.search",)


def test_partial_with_nothing_green_refuses_rather_than_guessing():
    decision = evaluate(
        request(
            settings=settings(audience={"inst-jira": "everyone"}, partial=True),
            tools=(tool(health=ToolHealth.DISABLED),),
        )
    )
    assert decision.outcome is Outcome.REFUSED_UNAVAILABLE and not decision.allowed


def test_execute_is_always_strict_and_ignores_audience():
    sick = tool(
        "github.pr.review_context", "inst-gh", "GitHub", ToolHealth.TOKEN_EXPIRED
    )
    base = dict(settings=settings(rule="user_only", partial=True), user_listed=True)
    refused = evaluate(request(Action.PR_REVIEW, tools=(sick,), **base))
    assert refused.reason is Reason.TOOL_TOKEN_EXPIRED and refused.mode is Mode.STRICT
    healthy = tool("github.pr.review_context", "inst-gh", "GitHub")
    allowed = evaluate(request(Action.PR_REVIEW, tools=(healthy,), **base))
    assert allowed.allowed and allowed.green_tools == ("github.pr.review_context",)
    assert not allowed.answer_ephemeral


def test_ask_private_requires_the_rule_and_answers_privately():
    plain = evaluate(request(Action.ASK_PRIVATE, user_listed=True))
    assert plain.reason is Reason.RULE_OFF
    ok = evaluate(
        request(
            Action.ASK_PRIVATE,
            settings=settings(
                rule="user_only", audience={"inst-jira": "allowed_people"}
            ),
            user_listed=True,
            tools=(tool(),),
        )
    )
    assert ok.allowed and ok.answer_ephemeral
    # An unmapped Slack id may use trusted Ask (it is read-only).
    assert ok.green_tools == ("jira.search",)


def test_question_length_and_limit_text():
    limits = AskLimits(max_question_chars=100)
    long = evaluate(request(settings=settings(limits=limits), question_chars=101))
    assert long.reason is Reason.QUESTION_TOO_LONG
    assert long.message == "That question is too long. Keep it under 100 characters."
    assert evaluate(
        request(settings=settings(limits=limits), question_chars=100)
    ).allowed


def test_ask_requests_must_supply_usage():
    with pytest.raises(ValueError, match="ask_usage"):
        evaluate(request(Action.ASK, ask_usage=None))


def stamps(*minutes_ago: float):
    return tuple(NOW - timedelta(minutes=m) for m in minutes_ago)


def test_rate_limits_user_channel_org_and_retry_after():
    limits = AskLimits(user_per_10_min=2, channel_per_10_min=3, org_per_day=4)
    cfg = settings(limits=limits)
    user = evaluate(
        request(settings=cfg, ask_usage=lambda: AskUsage(user=stamps(9, 5)))
    )
    assert user.reason is Reason.RATE_LIMIT_USER
    assert user.retry_after_seconds == 60  # the oldest ask leaves the window in 1 min
    assert user.message == "You're asking a lot; try again in 1 minute."
    channel = evaluate(
        request(settings=cfg, ask_usage=lambda: AskUsage(channel=stamps(8, 6, 1)))
    )
    assert channel.reason is Reason.RATE_LIMIT_CHANNEL
    assert channel.retry_after_seconds == 120
    org = evaluate(
        request(settings=cfg, ask_usage=lambda: AskUsage(org=stamps(600, 300, 100, 5)))
    )
    assert org.reason is Reason.RATE_LIMIT_ORG
    assert org.retry_after_seconds == 24 * 3600 - 600 * 60
    assert org.message == "You're asking a lot; try again in 14 hours."
    under = evaluate(
        request(
            settings=cfg,
            ask_usage=lambda: AskUsage(
                user=stamps(9), channel=stamps(9, 8), org=stamps(900)
            ),
        )
    )
    assert under.allowed


def test_old_asks_age_out_and_the_binding_wait_wins():
    limits = AskLimits(user_per_10_min=1, channel_per_10_min=1, org_per_day=100)
    usage = AskUsage(user=stamps(11), channel=stamps(2))
    verdict = check_ask_limits(limits, usage, NOW)
    assert (
        verdict.reason is Reason.RATE_LIMIT_CHANNEL
        and verdict.retry_after_seconds == 480
    )
    both = AskUsage(user=stamps(1), channel=stamps(9))
    assert check_ask_limits(limits, both, NOW).reason is Reason.RATE_LIMIT_USER


def test_execute_requests_are_not_subject_to_ask_limits():
    full = AskUsage(user=stamps(*([1] * 50)))
    decision = evaluate(
        request(
            Action.CREATE_TASK,
            settings=settings(rule="user_only"),
            user_listed=True,
            ask_usage=lambda: full,
        )
    )
    assert decision.allowed


def test_checks_trace_records_the_order_that_ran():
    decision = evaluate(request(Action.ASK))
    assert decision.checks == (
        "state",
        "installation",
        "paused",
        "limits",
        "tier",
        "tools",
    )
    blocked = evaluate(request(Action.CREATE_TASK))
    assert blocked.checks == ("state", "installation", "paused", "tier")
