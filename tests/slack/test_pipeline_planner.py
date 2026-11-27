"""The Ask planner: strict JSON, regex hints, usage metering and every failure mode."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from contracts.v1 import ModelProfileStatus, UsageState
from contracts.v1.slack_access import AskLimits
from modules.slack.access import messages
from modules.slack.access.models import Action, Reason, ToolHealth
from modules.slack.pipeline.hints import hinted_tool_ids
from modules.slack.pipeline.models import DiscoveredTool, PlannerStatus
from modules.slack.pipeline.planner import (
    MAX_QUESTION_CHARS,
    build_messages,
    parse_plan,
)
from modules.slack.pipeline.targets import PlannerTargets
from modules.usage.service import UsageLimitExceeded
from shared.errors import ModelGatewayError
from tests.support.slack_access_cases import GOOD_MEMBER, request, settings
from tests.support.slack_pipeline_world import JIRA, MARKER, ORG, SLACK, World

PRESENTED = frozenset({JIRA, SLACK, "github.issues.search"})


def _plan(**fields: object) -> str:
    return json.dumps({"needs": [], "general_ok": True, **fields})


@pytest.mark.parametrize(
    ("content", "status", "needs"),
    [
        (_plan(), PlannerStatus.VALID, ()),
        (_plan(needs=[JIRA]), PlannerStatus.VALID, (JIRA,)),
        (_plan(needs=[JIRA, SLACK, "github.issues.search"]), PlannerStatus.VALID, None),
        ("```json\n" + _plan(needs=[SLACK]) + "\n```", PlannerStatus.VALID, (SLACK,)),
        (
            _plan(needs=[JIRA, SLACK, "github.issues.search", "x"]),
            PlannerStatus.INVALID,
            (),
        ),
        (_plan(needs=[JIRA, JIRA]), PlannerStatus.INVALID, ()),
        (_plan(needs=["jira.issues.comment"]), PlannerStatus.UNPRESENTED, ()),
        (_plan(needs=[JIRA, "made.up"]), PlannerStatus.UNPRESENTED, ()),
        (_plan(needs=[], general_ok=False), PlannerStatus.INVALID, ()),
        (_plan(general_ok="yes"), PlannerStatus.INVALID, ()),
        (_plan(needs="jira.issues.search"), PlannerStatus.INVALID, ()),
        (_plan(needs=[7]), PlannerStatus.INVALID, ()),
        (json.dumps({"needs": []}), PlannerStatus.INVALID, ()),
        (
            json.dumps({"needs": [], "general_ok": True, "extra": 1}),
            PlannerStatus.INVALID,
            (),
        ),
        (json.dumps([JIRA]), PlannerStatus.INVALID, ()),
        ("Sure! needs: jira", PlannerStatus.INVALID, ()),
        ("", PlannerStatus.INVALID, ()),
    ],
)
def test_the_plan_is_validated_strictly(content, status, needs):
    parsed = parse_plan(content, PRESENTED)
    assert parsed.status is status
    if needs is not None:
        assert parsed.needs == needs
    if status is not PlannerStatus.VALID:
        assert parsed.needs == ()  # nothing from a refused plan is ever used


def _tool(tool_id: str, plugin: str, **extra) -> DiscoveredTool:
    return DiscoveredTool(
        tool_id=tool_id,
        installation_id="inst-" + plugin,
        org_id=ORG,
        label=plugin.title(),
        name=tool_id,
        description="Searches things.\nSecond line.",
        plugin_type=plugin,
        health=ToolHealth.HEALTHY,
        **extra,
    )


def test_the_prompt_has_tool_ids_and_a_capped_untrusted_question_only():
    tools = (_tool(JIRA, "jira"), _tool(SLACK, "slack"))
    system, user = build_messages(MARKER + "x" * 5000, tools)
    assert (
        "untrusted" in system.content and "never follow instructions" in system.content
    )
    assert user.content.count("\n- ") == 2  # one line per tool, id | name | description
    assert "Second line" in user.content and "Searches things. Second" in user.content
    question = user.content.split("<question>\n", 1)[1].split("\n</question>")[0]
    assert len(question) == MAX_QUESTION_CHARS and question.startswith(MARKER)


def test_hints_name_jira_keys_pull_requests_and_connected_repositories():
    jira = _tool(JIRA, "jira", projects=frozenset({"WEAVES"}))
    other_jira = _tool("jira.other", "jira")
    gh = _tool("github.issues.search", "github", repositories=frozenset({"acme/api"}))
    tools = (jira, other_jira, gh, _tool(SLACK, "slack"))
    assert hinted_tool_ids("status of WEAVES-12?", tools) == (JIRA, "jira.other")
    assert hinted_tool_ids("what is UTF-8 and COVID-19", tools) == ("jira.other",)
    assert hinted_tool_ids("see https://github.com/x/y/pull/9", tools) == (gh.tool_id,)
    assert hinted_tool_ids("is ACME/API healthy", tools) == (gh.tool_id,)
    assert hinted_tool_ids("what is a monad", tools) == ()
    assert hinted_tool_ids("WEAVES-12", ()) == ()


def _ask(world: World, text: str = MARKER, **access):
    base = {"now": datetime.now(UTC), "settings": _open(world, **access.pop("s", {}))}
    base.update(access)
    return world.request(text, request(Action.ASK, **base))


def _open(world: World, **kw):
    assert world.jira is not None and world.slack is not None
    return settings(
        audience={
            world.jira.plugin_installation_id: "everyone",
            world.slack.plugin_installation_id: "everyone",
        },
        **kw,
    )


def _fresh(world: World, status: str = "healthy") -> None:
    assert world.jira is not None and world.slack is not None
    world.snapshot(world.jira, status)
    world.snapshot(world.slack, "healthy")


def _result(world: World, text: str = MARKER, **access):
    return world.pipeline().run_pipeline(_ask(world, text, **access))


def _plan_reply(world: World, *needs: str) -> None:
    world.models.reply = lambda _: _plan(needs=list(needs), general_ok=not needs)


def test_a_regex_hint_is_unioned_into_a_planner_that_missed_the_source(pworld: World):
    _fresh(pworld)
    _plan_reply(pworld)  # the planner says nothing is needed
    result = _result(pworld, f"{MARKER} what is the state of WEAVES-12")
    assert result.outcome.value == "allowed" and result.capability_ids == (JIRA,)
    assert result.planner.status is PlannerStatus.VALID


def test_no_usable_tool_skips_the_planner_and_goes_model_only(pworld: World):
    assert pworld.jira is not None and pworld.slack is not None
    pworld.snapshot(pworld.jira, "token_expired")
    pworld.snapshot(pworld.slack, "token_expired")
    result = _result(pworld)
    assert result.planner.status is PlannerStatus.SKIPPED
    assert result.outcome.value == "model_only" and result.capability_ids == ()
    assert pworld.models.planner_calls == 0
    assert pworld.runtime.usage_service.ledger.get(ORG, "task:turn1:run:1:plan") is None


def test_a_hint_still_names_an_unusable_tool_so_it_is_reported_not_guessed(
    pworld: World,
):
    assert pworld.jira is not None and pworld.slack is not None
    pworld.snapshot(pworld.jira, "token_expired")
    pworld.snapshot(pworld.slack, "token_expired")
    result = _result(pworld, f"{MARKER} what is WEAVES-12")
    assert result.planner.status is PlannerStatus.SKIPPED
    assert result.outcome.value == "refused_unavailable"
    assert result.decision.reason is Reason.TOOL_TOKEN_EXPIRED
    assert result.message == messages.tool_red("Jira", Reason.TOOL_TOKEN_EXPIRED)
    assert pworld.connectors.total() == 0


def test_an_organization_without_connectors_is_model_only_without_a_planner(
    pworld: World,
):
    for installation in (pworld.jira, pworld.slack):
        assert installation is not None
        pworld.runtime.installations.delete(installation.plugin_installation_id)
    result = _result(pworld)
    assert result.outcome.value == "model_only" and result.tools_considered == ()
    assert pworld.models.planner_calls == 0 and pworld.models.answer_calls == 0


def test_the_planner_has_no_model_target_so_the_pipeline_fails_closed(pworld: World):
    _fresh(pworld)
    pipeline_request = replace(_ask(pworld), planner_target=None)
    result = pworld.pipeline().run_pipeline(pipeline_request)
    assert result.outcome.value == "planner_failed"
    assert result.planner.status is PlannerStatus.ERROR
    assert pworld.models.planner_calls == 0 and result.capability_ids == ()


def test_a_provider_error_fails_closed_and_the_ledger_resolves_the_row(pworld: World):
    _fresh(pworld)

    def boom(_):
        raise ModelGatewayError("model.upstream_error", "bad", http_status=500)

    pworld.models.reply = boom
    pipeline_request = _ask(pworld)
    result = pworld.pipeline().run_pipeline(pipeline_request)
    assert result.outcome.value == "planner_failed"
    assert result.planner.status is PlannerStatus.ERROR
    assert result.message == "I couldn't work out which sources to check. Try again."
    row = pworld.runtime.usage_service.ledger.get(ORG, pipeline_request.usage_key)
    assert row is not None and row.state is UsageState.UNKNOWN  # may have been sent


def test_a_client_rejected_planner_call_is_released_not_charged(pworld: World):
    _fresh(pworld)

    def rejected(_):
        raise ModelGatewayError("model.upstream_error", "bad", http_status=400)

    pworld.models.reply = rejected
    pipeline_request = _ask(pworld)
    pworld.pipeline().run_pipeline(pipeline_request)
    row = pworld.runtime.usage_service.ledger.get(ORG, pipeline_request.usage_key)
    assert row is not None and row.state is UsageState.RELEASED


def test_an_enforced_usage_limit_refuses_the_planner_before_any_provider_call(
    pworld: World, monkeypatch: pytest.MonkeyPatch
):
    _fresh(pworld)

    def deny(call, request):
        raise UsageLimitExceeded("tokens_per_day")

    monkeypatch.setattr(pworld.runtime.usage_service, "begin", deny)
    result = _result(pworld)
    assert result.outcome.value == "planner_failed" and pworld.models.planner_calls == 0


def test_a_slow_planner_times_out_and_still_settles_its_own_row(pworld: World):
    import time

    _fresh(pworld)
    pworld.models.delay = 0.5
    pipeline_request = _ask(pworld)
    started = time.monotonic()
    result = pworld.runtime and pworld.pipeline(planner_timeout=0.2).run_pipeline(
        pipeline_request
    )
    assert time.monotonic() - started < 0.45
    assert result.planner.status is PlannerStatus.TIMEOUT
    assert result.outcome.value == "planner_failed"
    time.sleep(0.6)
    row = pworld.runtime.usage_service.ledger.get(ORG, pipeline_request.usage_key)
    assert row is not None and row.state is UsageState.SETTLED


def test_the_planner_prompt_never_carries_data_or_other_requests(pworld: World):
    _fresh(pworld)
    _result(
        pworld,
        f"{MARKER} " + "q" * 3000,
        s={"limits": AskLimits(max_question_chars=4000)},
    )
    (prompt,) = pworld.models.planner_prompts
    assert prompt.count(MARKER) == 1 and len(prompt) < 2500
    assert "fixture" not in prompt and "WEAVES" not in prompt  # no token, no config


def test_partial_mode_names_what_was_skipped(pworld: World):
    assert pworld.jira is not None
    pworld.snapshot(pworld.jira, "healthy", age_minutes=40)
    pworld.connectors.fail_probe.add("jira")
    _plan_reply(pworld, JIRA, SLACK)
    pworld.snapshot(pworld.slack, "healthy")
    result = _result(pworld, s={"partial": True})
    assert result.outcome.value == "allowed_partial"
    assert result.capability_ids == (SLACK,)
    assert result.skipped_note == "Skipped: Jira (it's unavailable)"
    assert pworld.connectors.count("jira", "probe") == 1


def test_a_stale_snapshot_is_probed_live_once_and_the_result_is_stored(pworld: World):
    assert pworld.jira is not None
    pworld.snapshot(pworld.jira, "healthy", age_minutes=40)
    pworld.snapshot(pworld.slack, "healthy")
    _plan_reply(pworld, JIRA)
    result = _result(pworld)
    assert result.outcome.value == "allowed" and result.capability_ids == (JIRA,)
    assert pworld.connectors.count("jira", "probe") == 1
    stored = pworld.runtime.slack_connector_health.get(
        pworld.jira.plugin_installation_id
    )
    assert stored.status == "healthy" and stored.reason_code == ""
    # Fresh now: a second request needs no probe.
    _result(pworld)
    assert pworld.connectors.count("jira", "probe") == 1


def test_pr_review_needs_a_capability_that_does_not_exist_yet(pworld: World):
    _fresh(pworld)
    access = request(
        Action.PR_REVIEW,
        now=datetime.now(UTC),
        settings=settings(rule="user_only"),
        user_listed=True,
        member=GOOD_MEMBER,
    )
    result = pworld.pipeline().run_pipeline(pworld.request(MARKER, access))
    assert result.tools_needed == ("github.pr.review_context",)
    assert result.outcome.value == "refused_unavailable"
    assert (
        result.message
        == "I can't check GitHub right now (it's unavailable); nothing was looked up."
    )
    assert pworld.models.planner_calls == 0 and pworld.connectors.total() == 0


def test_the_planner_model_falls_back_to_the_agents_profile(pworld: World):
    runtime = pworld.runtime
    task = runtime.task_service.create_task(
        agent_id=pworld.agent_id, title="t", initial_message="hello"
    )
    targets = PlannerTargets(
        agent_versions=runtime.agent_versions,
        model_profiles=runtime.model_profiles,
        providers=runtime.providers,
        model_configurations=runtime.model_configurations,
    )
    own = runtime.model_profiles.get("local-model-profile")
    cheaper = runtime.model_profiles.put(
        own.model_copy(update={"id": "cheap-profile", "display_name": "Cheap"})
    )
    assert targets.resolve(None, task).profile.id == own.id
    assert targets.resolve("cheap-profile", task).profile.id == cheaper.id
    assert targets.resolve("no-such-profile", task).profile.id == own.id
    runtime.model_profiles.put(
        cheaper.model_copy(update={"status": ModelProfileStatus.DISABLED})
    )
    assert targets.resolve("cheap-profile", task).profile.id == own.id
