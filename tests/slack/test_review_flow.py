"""``@Weaves review <PR url>``: the pre-flight matrix and the task it makes."""

from __future__ import annotations

import itertools
import json

import pytest

from contracts.v1 import TaskAudience
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.review.agent import ReviewEnabler
from tenancy_harness.fake_github import STATE
from tests.support.slack_pipeline_world import CHANNEL, ORG, TEAM, USER
from tests.support.slack_review_world import (
    MEMBER,
    ReviewWorld,
    build_review_world,
)

RULES = ("off", "user_only", "channel_only", "user_and_channel", "user_or_channel")


def _expected(
    rule: str, user: bool, channel: bool, mapped: bool, covered: bool, ok: bool
):
    """Independent oracle for the pre-flight: ``None`` (allowed) or the refusal text."""
    passes = {
        "off": False,
        "user_only": user,
        "channel_only": channel,
        "user_and_channel": user and channel,
        "user_or_channel": user or channel,
    }[rule]
    if not passes:
        if rule == "off":
            return f":lock: {copy.NOT_SET_UP['review']}"
        if rule == "channel_only" or (rule == "user_and_channel" and user):
            return f":lock: {messages.CHANNEL_NOT_LISTED}"
        return f":lock: {messages.USER_NOT_LISTED}"
    if not (user and mapped):
        return f":lock: {messages.NOT_MAPPED}"
    if not (covered and ok):
        return f":lock: {messages.REPO_NOT_ENABLED}"
    return None


def test_the_full_preflight_matrix_rule_user_channel_mapped_covered_allowlisted(
    rworld: ReviewWorld,
) -> None:
    runtime = rworld.runtime
    cases = list(itertools.product(RULES, (True, False), (True, False), (True, False)))
    seen_allowed = seen_blocked = 0
    for rule, user, channel, mapped in cases:
        for covered, allowlisted in itertools.product((True, False), repeat=2):
            repo = "acme/api" if covered else "acme/elsewhere"
            _clear(runtime.slack_allowed_users, f"{ORG}:{USER}")
            _clear(runtime.slack_allowed_channels, f"{ORG}:{CHANNEL}")
            rworld.allow(
                rule=rule,
                repos=(repo,) if allowlisted else ("acme/unrelated",),
                mapped=mapped,
                listed=user,
                channel_listed=channel,
            )
            tasks_before = len(rworld.tasks())
            before_calls = len(STATE.calls)
            rworld.review(5, repo=repo)
            rworld.aworld.dispatch()

            expect = _expected(rule, user, channel, mapped, covered, allowlisted)
            context = (rule, user, channel, mapped, covered, allowlisted)
            if expect is None:
                seen_allowed += 1
                assert len(rworld.tasks()) == tasks_before + 1, context
            else:
                seen_blocked += 1
                assert len(rworld.tasks()) == tasks_before, context
                assert rworld.aworld.slack.ephemerals[-1]["text"] == expect, context
            # Pre-flight never touches GitHub or a model.
            assert len(STATE.calls) == before_calls, context
            assert (
                rworld.review_calls == 0
                and rworld.aworld.world.models.planner_calls == 0
            )
    assert seen_allowed + seen_blocked == len(cases) * 4 == 160
    assert seen_allowed > 0 and seen_blocked > 0


def _clear(repository, record_id: str) -> None:
    try:
        repository.delete(record_id)
    except KeyError:
        return


def test_an_allowed_review_makes_one_private_task_for_the_mapped_member(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    event = rworld.review(123)

    rworld.aworld.dispatch()

    (task,) = rworld.tasks()
    assert task.audience is TaskAudience.PRIVATE
    assert task.created_by_principal_id == MEMBER
    assert task.title == "Review acme/api#123"
    assert task.idempotency_key == f"slack:{TEAM}:{event.event_id}"
    turn = rworld.runtime.task_turns.get(task.active_turn_id)
    origin = turn.origin
    assert origin is not None and origin.kind == "slack"
    assert (origin.action, origin.repo, origin.pr_number) == (
        "pr_review",
        "acme/api",
        123,
    )
    # The only text the model task carries is built from the validated link.
    (message,) = rworld.runtime.task_messages.list()
    assert message.content == "Review pull request https://github.com/acme/api/pull/123"
    (row,) = rworld.aworld.threads()
    assert (row.kind, row.tier, row.task_id) == ("review", "execute", task.task_id)
    assert row.requester_principal_id == MEMBER and row.reply_state == "pending"
    (inbox,) = rworld.aworld.inbox()
    assert inbox.state == "done" and inbox.text is None
    assert rworld.review_calls == 0  # nothing ran yet
    assert STATE.calls == []  # and nothing was read


def test_the_review_agent_is_created_once_with_one_capability_and_a_strict_schema(
    rworld: ReviewWorld,
) -> None:
    from modules.slack.review.prompt import REVIEW_OUTPUT_SCHEMA

    rworld.allow()
    rworld.review(123)
    rworld.review(41)
    rworld.aworld.dispatch()

    first, second = rworld.tasks()
    assert first.agent_id == second.agent_id
    agent = rworld.runtime.agents.get(first.agent_id)
    assert agent.name == "Slack PR review"
    version = rworld.runtime.agent_versions.get(agent.current_version_id)
    assert version.allowed_capability_ids == ("github.pr.review_context",)
    assert json.loads(json.dumps(dict(version.output_schema), default=_plain)) == (
        json.loads(json.dumps(REVIEW_OUTPUT_SCHEMA))
    )
    assert (
        version.budget.max_tool_calls == 1 and version.budget.max_output_tokens <= 1500
    )
    assert version.version == 1


def _plain(value):
    if hasattr(value, "items"):
        return dict(value)
    return list(value)


def test_the_reader_is_enabled_once_on_the_covering_connection_and_audited(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    before = rworld.runtime.installations.get("gh-install-1").enabled_capability_ids
    assert "github.pr.review_context" not in before

    rworld.review(123)
    rworld.review(41)
    rworld.aworld.dispatch()

    after = rworld.runtime.installations.get("gh-install-1").enabled_capability_ids
    assert after.count("github.pr.review_context") == 1
    enabled = [
        e
        for e in rworld.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.review.capability_enabled"
    ]
    assert len(enabled) == 1 and enabled[0].target_id == "gh-install-1"
    assert enabled[0].actor_principal_id == "system:slack"
    assert isinstance(ReviewEnabler, type)


def test_a_blocked_review_is_audited_without_the_link_or_a_task(
    rworld: ReviewWorld,
) -> None:
    rworld.allow(repos=("acme/unrelated",))
    rworld.review(123)
    rworld.aworld.dispatch()

    assert rworld.tasks() == [] and rworld.aworld.threads() == []
    blocked = [
        e
        for e in rworld.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.execute.blocked"
    ]
    assert len(blocked) == 1
    assert blocked[0].metadata["action"] == "pr_review"
    assert blocked[0].metadata["reason"] == "repo_not_allowed"
    assert "acme" not in blocked[0].model_dump_json()  # no repo, no link


@pytest.mark.parametrize(
    "text",
    [
        "review",
        "review https://github.com.evil.test/acme/api/pull/123",
        "review https://github.com/acme/api/pull/1 https://github.com/acme/api/pull/2",
        "review https://gitlab.com/acme/api/pull/1",
    ],
)
def test_a_missing_or_bad_link_asks_in_words_and_does_nothing(
    rworld: ReviewWorld, text: str
) -> None:
    rworld.allow()
    rworld.aworld.mention(text)
    rworld.aworld.dispatch()

    assert rworld.aworld.slack.texts() == [copy.MISSING_PR]
    assert rworld.tasks() == [] and STATE.calls == []


def test_a_replayed_event_creates_one_task(rworld: ReviewWorld) -> None:
    rworld.allow()
    event = rworld.review(123)
    rworld.aworld.dispatch()
    # The inbox row is settled; replay the same event through the flow directly.
    installation = rworld.runtime.slack_store.find_by_team(TEAM)
    from datetime import UTC, datetime

    from modules.slack.dispatch.ask_flow import AskKind
    from shared.request_context import set_request_identity

    set_request_identity(
        installation.installed_by_principal_id,
        installation.org_id,
        installation.workspace_id,
    )
    result = rworld.runtime.slack_dispatcher._review.run(
        installation, event, "acme/api#123", datetime.now(UTC)
    )
    assert result.kind is AskKind.REPLAY
    assert len(rworld.tasks()) == 1


def test_a_second_review_waits_for_the_first_then_gets_its_own_task(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    first = rworld.review(123)
    rworld.aworld.dispatch()
    rworld.review(41, thread_ts=first.message_ts)

    assert rworld.aworld.dispatch() == 0  # waits: the first reply is pending
    assert len(rworld.tasks()) == 1

    rworld.aworld.work()
    rworld.aworld.reply()
    rworld.aworld.dispatch()

    assert len(rworld.tasks()) == 2
    (row,) = rworld.aworld.threads()
    assert row.request_count == 2 and row.reply_state == "pending"
    assert row.task_id == max(rworld.tasks(), key=lambda t: t.created_at).task_id


def test_a_review_and_an_ask_can_share_a_thread(rworld: ReviewWorld) -> None:
    rworld.allow()
    first = rworld.aworld.mention("what is idempotency?")
    rworld.aworld.dispatch()
    rworld.review(123, thread_ts=first.message_ts)
    rworld.aworld.dispatch()

    kinds = sorted(row.kind for row in rworld.aworld.threads())
    assert kinds == ["ask", "review"]
    assert len(rworld.tasks()) == 2


def test_a_review_without_an_ai_model_says_so_in_the_thread(
    rworld: ReviewWorld,
) -> None:
    from contracts.v1 import ModelProfileStatus

    rworld.allow()
    for profile in rworld.runtime.model_profiles.list():
        rworld.runtime.model_profiles.put(
            profile.model_copy(update={"status": ModelProfileStatus.DISABLED})
        )
    rworld.review(123)
    rworld.aworld.dispatch()

    assert rworld.aworld.slack.texts()[-1] == copy.NO_MODEL  # said in the thread
    assert rworld.tasks() == []


def test_a_connection_that_does_not_cover_the_repository_never_enables_anything(
    rworld: ReviewWorld,
) -> None:
    rworld.aworld.world.close()
    world = build_review_world(covered=False)
    try:
        world.allow()
        world.review(123)
        world.aworld.dispatch()
        assert world.tasks() == []
        assert world.aworld.slack.ephemerals[-1]["text"] == (
            f":lock: {messages.REPO_NOT_ENABLED}"
        )
    finally:
        world.aworld.world.close()


def test_the_ask_planner_and_the_qa_agent_never_see_the_review_tool(
    rworld: ReviewWorld,
) -> None:
    rworld.allow()
    rworld.review(123)
    rworld.aworld.dispatch()  # enables the reader on the connection
    rworld.aworld.world.snapshot(rworld.github_installation, "healthy")
    rworld.aworld.plan()
    rworld.aworld.mention("what do you think of https://github.com/acme/api/pull/123 ?")
    rworld.aworld.dispatch()

    qa = next(t for t in rworld.tasks() if t.title == "Slack question")
    version = rworld.runtime.agent_versions.get(qa.agent_version_id)
    assert "github.pr.review_context" not in version.allowed_capability_ids
    rworld.aworld.work()  # the review task is first in the queue
    rworld.aworld.work()
    assert all(
        "github.pr.review_context" not in prompt
        for prompt in rworld.aworld.world.models.planner_prompts
    )
