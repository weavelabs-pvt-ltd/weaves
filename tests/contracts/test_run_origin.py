"""Capability narrowing by turn origin (design 3.5.1, runtime_run_capabilities)."""

from __future__ import annotations

import threading

import pytest
from pydantic import ValidationError

from contracts.v1 import TaskTurn, TaskTurnStatus, TurnOrigin, TurnVerdict
from shared.clock import utc_now
from shared.run_origin import RunOrigin, allowed_capabilities, bound_run_origin

CONFIGURED = ("jira.issues.search", "slack.messages.search", "github.issues.search")


def test_a_run_with_no_origin_keeps_the_whole_configured_list():
    assert allowed_capabilities(CONFIGURED) == CONFIGURED
    assert allowed_capabilities(()) == ()


@pytest.mark.parametrize("kind", ["slack", "slack_qa"])
def test_slack_turns_run_only_their_approved_capabilities_in_configured_order(kind):
    approved = ("github.issues.search", "jira.issues.search")
    with bound_run_origin(RunOrigin(kind, approved)):
        assert allowed_capabilities(CONFIGURED) == (
            "jira.issues.search",
            "github.issues.search",
        )


@pytest.mark.parametrize("kind", ["slack", "slack_qa"])
def test_a_slack_turn_without_an_approved_list_runs_nothing(kind):
    with bound_run_origin(RunOrigin(kind, None)):
        assert allowed_capabilities(CONFIGURED) == ()
    with bound_run_origin(RunOrigin(kind, ())):
        assert allowed_capabilities(CONFIGURED) == ()


def test_narrowing_never_adds_a_capability_the_agent_version_lacks():
    with bound_run_origin(RunOrigin("slack_qa", ("jira.issues.comment", "made.up"))):
        assert allowed_capabilities(CONFIGURED) == ()


def test_other_origin_kinds_are_not_narrowed():
    with bound_run_origin(RunOrigin("workflow", ())):
        assert allowed_capabilities(CONFIGURED) == CONFIGURED


def test_the_binding_is_scoped_and_isolated_between_threads():
    seen: list[tuple[str, ...]] = []
    with bound_run_origin(RunOrigin("slack_qa", ("jira.issues.search",))):
        worker = threading.Thread(
            target=lambda: seen.append(allowed_capabilities(CONFIGURED))
        )
        worker.start()
        worker.join()
        assert allowed_capabilities(CONFIGURED) == ("jira.issues.search",)
    assert allowed_capabilities(CONFIGURED) == CONFIGURED
    assert seen == [CONFIGURED]  # a bare thread starts unbound and unrestricted
    with bound_run_origin(None):  # None binds nothing
        assert allowed_capabilities(CONFIGURED) == CONFIGURED


def _origin(**changes) -> TurnOrigin:
    fields = {
        "kind": "slack_qa",
        "team_id": "T0FAKE001",
        "channel_id": "C0CHAN001",
        "thread_ts": "1700000000.000100",
        "slack_user_id": "U0ASKER01",
        "event_id": "evt1",
    }
    fields.update(changes)
    return TurnOrigin(**fields)


def test_the_origin_contract_is_strict_and_round_trips_on_a_turn():
    origin = _origin(
        capability_ids=("jira.issues.search",),
        verdict=TurnVerdict(outcome="allowed", planner_status="valid"),
    )
    now = utc_now()
    turn = TaskTurn(
        turn_id="turn-1",
        task_id="task-1",
        user_message_id="msg-1",
        requested_by_principal_id="p-1",
        status=TaskTurnStatus.QUEUED,
        org_id="org-a",
        workspace_id="ws",
        origin=origin,
        created_at=now,
        updated_at=now,
    )
    assert TaskTurn.model_validate_json(turn.model_dump_json()).origin == origin
    assert TaskTurn.model_validate(turn.model_dump()).origin == origin
    with pytest.raises(ValidationError):
        _origin(kind="web")
    with pytest.raises(ValidationError):
        _origin(text="what is the weather")  # no field can carry message text
    with pytest.raises(ValidationError):
        _origin(action="delete_everything")
    assert _origin().capability_ids is None  # unset means fail closed


READER = "github.pr.review_context"


def test_the_pull_request_reader_runs_only_for_a_slack_execute_turn():
    listed = (*CONFIGURED, READER)
    # A web task, a workflow or any run with no Slack origin never runs it.
    assert allowed_capabilities(listed) == CONFIGURED
    # A hidden Slack Q&A turn never runs it, even if it were approved.
    with bound_run_origin(RunOrigin("slack_qa", (*CONFIGURED, READER))):
        assert READER not in allowed_capabilities(listed)
    # A Slack review turn runs exactly what its pipeline approved.
    with bound_run_origin(RunOrigin("slack", (READER,))):
        assert allowed_capabilities(listed) == (READER,)
    with bound_run_origin(RunOrigin("slack", ())):
        assert allowed_capabilities(listed) == ()
