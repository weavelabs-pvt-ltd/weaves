"""A created task: its connectors, what the task API exposes, links, retention."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from modules.slack.qa_retention import SlackRetention
from modules.tasks.http.routes import _turn_view
from tests.support.slack_ask_world import AskWorld
from tests.support.slack_pipeline_world import JIRA, SLACK, USER
from tests.support.slack_task_world import (
    APP_URL,
    TASK_TEXT,
    enable_tasks,
    make_agent,
    run_task,
    task_rows,
    tasks,
)


@pytest.fixture(autouse=True)
def _app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", APP_URL)


def _mention_in_new_thread(w: AskWorld, text: str = TASK_TEXT, **kw) -> None:
    w.mention(f"task: {text}", thread_ts=f"17000{w.serial + 1:05d}.000100", **kw)


# -- connectors of the created task ----------------------------------------------------


def _task_with(w: AskWorld, *caps: str) -> None:
    enable_tasks(w, agent=False)
    agent_id = make_agent(w, caps=caps)
    w.world.set_slack_settings(default_task_agent_id=agent_id)


def test_a_task_uses_its_agents_healthy_connectors_whatever_the_ask_audience(
    aworld: AskWorld,
) -> None:
    _task_with(aworld, JIRA, SLACK)
    w = aworld.world
    assert w.jira is not None and w.slack is not None
    w.snapshot(w.jira, "healthy")
    w.snapshot(w.slack, "healthy")  # both connectors keep the default audience

    turn = run_task(aworld)

    assert turn.status.value == "succeeded"
    assert set(turn.origin.capability_ids) == {JIRA, SLACK}
    assert w.connectors.count("jira", "data") == 1
    assert w.connectors.count("slack", "data") == 1
    assert w.models.planner_calls == 0  # Execute plans by grammar, not by a model


def test_a_red_connector_is_skipped_and_named_but_the_task_still_runs(
    aworld: AskWorld,
) -> None:
    _task_with(aworld, JIRA, SLACK)
    w = aworld.world
    assert w.jira is not None and w.slack is not None
    w.disable(w.jira)
    w.snapshot(w.slack, "healthy")

    turn = run_task(aworld)

    assert turn.status.value == "succeeded"
    assert set(turn.origin.capability_ids) == {SLACK}
    assert turn.origin.verdict.outcome == "allowed_partial"
    assert "Jira" in (turn.origin.verdict.skipped_note or "")
    assert w.connectors.count("jira") == 0  # never called
    assert w.connectors.count("slack", "data") == 1


def test_a_task_whose_every_connector_is_red_is_refused_without_a_model_call(
    aworld: AskWorld,
) -> None:
    _task_with(aworld, JIRA)
    w = aworld.world
    assert w.jira is not None
    w.disable(w.jira)

    turn = run_task(aworld)

    assert turn.origin.capability_ids == ()
    assert turn.agent_run_id is None and w.models.answer_calls == 0
    assert w.connectors.total("data") == 0
    first = aworld.slack.messages[0]["text"]
    assert "I can't check" in first and "nothing was looked up" in first
    assert len(aworld.slack.messages) == 1  # no result post for a refusal


def test_an_agent_without_connectors_runs_with_none(aworld: AskWorld) -> None:
    _task_with(aworld)

    turn = run_task(aworld)

    assert turn.status.value == "succeeded"
    assert turn.origin.capability_ids == ()
    assert aworld.world.connectors.total() == 0


# -- what the task API exposes ---------------------------------------------------------


def test_the_task_api_shows_the_origin_without_slack_internals(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld, channel_listed=True)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()
    (task,) = tasks(aworld)

    shown = aworld.runtime.task_service.get_task(task.task_id).model_dump(mode="json")

    origin = shown["origin"]
    assert set(origin) == {
        "schema_version",
        "kind",
        "channel_id",
        "channel_name",
        "requested_by_name",
        "permalink",
    }
    assert origin["kind"] == "slack" and origin["channel_name"] == "ops"
    assert origin["requested_by_name"] == "Local developer"
    row = aworld.latest()
    assert origin["permalink"] == (
        "https://acme-test.slack.com/archives/C0CHAN001/"
        f"p{row.thread_ts.replace('.', '')}?thread_ts={row.thread_ts}&cid=C0CHAN001"
    )
    blob = json.dumps(shown)
    # The thread timestamp appears only inside the permalink; the Slack user, the
    # settings version and the verdict never appear at all.
    for secret in (USER, "settings_version", "verdict", "capability_ids"):
        assert secret not in blob
    assert row.thread_ts not in blob.replace(origin["permalink"], "")


def test_the_turn_api_never_shows_the_slack_origin_or_the_verdict(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    turn = run_task(aworld)
    assert turn.origin is not None and turn.origin.verdict is not None

    view = _turn_view(turn, lambda m: m.model_dump(mode="json"))

    assert "origin" not in view
    assert "verdict" not in json.dumps(view) and USER not in json.dumps(view)


def test_slack_tasks_are_in_the_normal_list_and_filterable(aworld: AskWorld) -> None:
    enable_tasks(aworld)
    other = aworld.runtime.task_service.create_task(
        agent_id=make_agent(aworld, name="Other"),
        title="A dashboard task",
        initial_message="hello",
    )
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    everything = aworld.runtime.task_service.list_tasks()
    slack_only = aworld.runtime.task_service.list_tasks(origin_kind="slack")

    assert {t.title for t in everything} == {"A dashboard task", TASK_TEXT}
    assert [t.title for t in slack_only] == [TASK_TEXT]
    assert other.origin is None


def test_hidden_ask_tasks_stay_out_of_the_list(aworld: AskWorld) -> None:
    aworld.ask("what does idempotent mean?")

    assert aworld.runtime.task_service.list_tasks() == ()
    assert aworld.runtime.task_service.list_tasks(origin_kind="slack") == ()


# -- the Slack permalink ---------------------------------------------------------------


def test_the_workspace_address_is_fetched_once_and_cached(aworld: AskWorld) -> None:
    enable_tasks(aworld)
    _mention_in_new_thread(aworld)
    _mention_in_new_thread(aworld)
    aworld.dispatch()

    assert aworld.slack.count("auth.test") == 1
    urls = [t.origin.permalink for t in tasks(aworld)]
    assert all(
        u and u.startswith("https://acme-test.slack.com/archives/") for u in urls
    )
    assert aworld.runtime.slack_access_service.workspace_url("T0FAKE001") == (
        "https://acme-test.slack.com/"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.slack.com/",
        "https://evil.example.com/",
        "https://slack.com.evil.io/",
        "",
    ],
)
def test_a_workspace_address_that_is_not_slack_gives_no_permalink(
    aworld: AskWorld, url: str
) -> None:
    enable_tasks(aworld)
    aworld.slack.workspace_url = url

    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    (task,) = tasks(aworld)
    assert task.origin is not None and task.origin.permalink is None
    assert aworld.runtime.slack_access_service.workspace_url("T0FAKE001") is None


def test_a_failed_address_lookup_does_not_stop_the_task(aworld: AskWorld) -> None:
    enable_tasks(aworld)
    aworld.slack.faults["auth.test"] = ["500"]

    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    (task,) = tasks(aworld)
    assert task.origin is not None and task.origin.permalink is None


def test_without_an_app_address_the_messages_carry_no_button(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WEAVES_PUBLIC_FRONTEND_URL")
    enable_tasks(aworld)

    run_task(aworld)

    assert all("actions" not in (m.get("blocks") or "") for m in aworld.slack.messages)
    assert aworld.slack.messages[0]["text"].startswith(":white_check_mark: Task done")


# -- retention -------------------------------------------------------------------------


def test_a_created_task_is_never_swept_by_the_hidden_conversation_retention(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    run_task(aworld)
    aworld.ask("what does idempotent mean?")  # a hidden one in another thread
    retention: SlackRetention = aworld.runtime.slack_retention
    retention._clock = lambda: datetime.now(UTC) + timedelta(days=90)

    retention.run()

    assert len(task_rows(aworld)) == 1  # the Slack-created task and its row remain
    (task,) = aworld.runtime.task_service.list_tasks()
    assert task.origin is not None
    assert [r.kind for r in aworld.threads()] == ["task"]  # the hidden Ask is gone
