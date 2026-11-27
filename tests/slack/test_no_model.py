"""No AI model connected: said once, in the thread, before anything is spent."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from contracts.v1 import (
    ErrorSummary,
    ModelProfileStatus,
    ModelProviderStatus,
    ModelProviderType,
)
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.model_status import (
    ModelConnectivity,
    ModelReadiness,
    is_connectivity_failure,
)
from modules.slack.reply import compose
from shared.errors import ModelGatewayError
from tests.support.slack_ask_world import AskWorld
from tests.support.slack_pipeline_world import ORG

SECRET = "sk-provider-secret-text-must-never-leak"
TEXT = "No AI model is connected. Ask a Weaves admin to connect one."


def _provider(w: AskWorld) -> Any:
    (provider,) = w.runtime.providers.list()
    return provider


def _break_provider(w: AskWorld, **update: Any) -> None:
    w.runtime.providers.put(_provider(w).model_copy(update=update))


def _decisions(w: AskWorld) -> list[Any]:
    return [
        e
        for e in w.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.decision"
    ]


def _public(w: AskWorld) -> list[dict[str, Any]]:
    return [m for m in w.slack.messages if not m.get("deleted")]


def _mock(w: AskWorld) -> None:
    _break_provider(w, provider_type=ModelProviderType.LOCAL, auth_ref=None)


def _no_credential(w: AskWorld) -> None:
    _break_provider(w, auth_ref=None)


def _provider_disabled(w: AskWorld) -> None:
    _break_provider(w, status=ModelProviderStatus.DISABLED)


def _profile_disabled(w: AskWorld) -> None:
    for profile in w.runtime.model_profiles.list():
        w.runtime.model_profiles.put(
            profile.model_copy(update={"status": ModelProfileStatus.DISABLED})
        )


def _no_profile(w: AskWorld) -> None:
    for profile in w.runtime.model_profiles.list():
        w.runtime.model_profiles.delete(profile.id)


BROKEN = [
    ("mock", _mock, ModelReadiness.TEST_PROVIDER),
    ("no_credential", _no_credential, ModelReadiness.NO_CREDENTIAL),
    ("provider_disabled", _provider_disabled, ModelReadiness.DISABLED),
    ("profile_disabled", _profile_disabled, ModelReadiness.NO_PROFILE),
    ("no_profile", _no_profile, ModelReadiness.NO_PROFILE),
]


def _connectivity(w: AskWorld) -> ModelConnectivity:
    runtime = w.runtime
    return ModelConnectivity(
        model_profiles=runtime.model_profiles,
        providers=runtime.providers,
        model_configurations=runtime.model_configurations,
    )


# -- the helper ------------------------------------------------------------------


def test_a_real_provider_kind_with_a_credential_is_connected(aworld: AskWorld) -> None:
    assert _connectivity(aworld).readiness(ORG) is ModelReadiness.CONNECTED
    assert _connectivity(aworld).connected(ORG)


@pytest.mark.parametrize(("name", "break_it", "expected"), BROKEN)
def test_the_readiness_reason_follows_the_records_not_names(
    aworld: AskWorld,
    name: str,
    break_it: Callable[[AskWorld], None],
    expected: ModelReadiness,
) -> None:
    break_it(aworld)
    assert _connectivity(aworld).readiness(ORG) is expected
    assert not _connectivity(aworld).connected(ORG)


def test_a_display_name_never_decides(aworld: AskWorld) -> None:
    _break_provider(aworld, display_name="Deterministic mock")
    assert _connectivity(aworld).connected(ORG)  # real kind, real credential
    _break_provider(
        aworld, display_name="Anthropic", provider_type=ModelProviderType.LOCAL
    )
    assert not _connectivity(aworld).connected(ORG)


@pytest.mark.parametrize(
    ("code", "status", "summary", "lost"),
    [
        ("model.credential_unavailable", None, "", True),
        ("model.auth_ref_missing", None, "", True),
        ("model.adapter_missing", None, "", True),
        ("model.disabled", None, "", True),
        ("model.timeout", None, "", True),
        ("model.transport_error", None, "", True),
        ("model.upstream_error", 401, "", True),
        ("model.upstream_error", 403, "", True),
        ("model.upstream_error", 503, "", True),
        ("model.upstream_error", None, "Model provider returned HTTP 401 (x)", True),
        ("model.upstream_error", 400, "", False),
        ("model.upstream_error", 429, "", False),
        ("model.upstream_error", 500, "", False),
        ("model.upstream_error", None, "no status here", False),
        ("model.invalid_response", None, "", False),
        ("model.empty_response", None, "", False),
        ("model.not_allowed", None, "", False),
        ("tool.failed", None, "", False),
    ],
)
def test_error_classes_map_to_connectivity(
    code: str, status: int | None, summary: str, lost: bool
) -> None:
    assert is_connectivity_failure(code, status, summary) is lost


def test_a_failed_turn_with_a_connectivity_error_says_no_model_publicly() -> None:
    def turn(code: str, summary: str) -> Any:
        error = ErrorSummary(code=code, summary=summary)
        return SimpleNamespace(error=error, turn_id="turn-abc123")

    lost = compose.failure(turn("model.upstream_error", f"HTTP 401 {SECRET}"))
    assert (lost.kind, lost.text, lost.ephemeral) == ("no_model", TEXT, False)
    other = compose.failure(turn("model.invalid_response", SECRET))
    assert other.text == copy.failure("abc123") and SECRET not in other.text
    assert copy.NO_MODEL == messages.NO_MODEL == TEXT


# -- the dispatcher: answered at once -------------------------------------------


@pytest.mark.parametrize(("name", "break_it", "expected"), BROKEN)
def test_the_dispatcher_answers_at_once_before_spending_anything(
    aworld: AskWorld,
    name: str,
    break_it: Callable[[AskWorld], None],
    expected: ModelReadiness,
) -> None:
    break_it(aworld)
    event = aworld.mention("what is WEAVES-1?")

    aworld.dispatch()

    (message,) = _public(aworld)
    assert message["text"] == TEXT
    assert message["thread_ts"] == event.message_ts  # threaded, visible to all
    assert aworld.slack.ephemerals == []
    assert list(aworld.runtime.tasks.list()) == []
    assert aworld.threads() == []
    assert aworld.runtime.slack_access_service.qa_ids() == (None, None)
    assert _decisions(aworld) == []  # nothing counted against the Ask limits
    assert aworld.world.models.planner_calls == aworld.world.models.answer_calls == 0
    assert aworld.world.connectors.total() == 0
    assert {e.state for e in aworld.inbox()} == {"done"}


def test_a_burst_in_one_thread_gets_one_message_and_another_thread_its_own(
    aworld: AskWorld,
) -> None:
    _mock(aworld)
    first = aworld.mention("one?")
    aworld.mention("two?", thread_ts=first.message_ts)
    aworld.mention("three?", thread_ts=first.message_ts)
    aworld.dispatch()
    assert [m["text"] for m in _public(aworld)] == [TEXT]

    aworld.mention("elsewhere?")  # a new thread is told too
    aworld.dispatch()
    assert [m["text"] for m in _public(aworld)] == [TEXT, TEXT]

    # The same thread is told again once the ten minutes have passed.
    later = datetime.now(UTC) + timedelta(minutes=11)
    aworld.runtime.slack_dispatcher._clock = lambda: later
    aworld.mention("again?", thread_ts=first.message_ts)
    aworld.dispatch()
    assert len(_public(aworld)) == 3


def test_a_user_who_may_not_ask_gets_their_own_refusal_not_the_model_notice(
    aworld: AskWorld,
) -> None:
    _mock(aworld)
    aworld.world.set_slack_settings(ask_enabled=False)
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    assert _public(aworld) == []
    assert [e["text"] for e in aworld.slack.ephemerals] == [messages.ASK_DISABLED]


def test_connecting_a_model_restores_normal_answers(aworld: AskWorld) -> None:
    _mock(aworld)
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    assert [m["text"] for m in _public(aworld)] == [TEXT]
    _break_provider(
        aworld, provider_type=ModelProviderType.OPENAI_COMPATIBLE, auth_ref="secret:k"
    )
    aworld.ask("what does idempotent mean?")
    assert any("general knowledge" in t for t in [m["text"] for m in _public(aworld)])


# -- late: the model fails while in use -----------------------------------------


def _raise(error: Exception) -> Callable[[Any], str]:
    def reply(_: Any) -> str:
        raise error

    return reply


LOST = [
    ModelGatewayError("model.upstream_error", f"HTTP 401 {SECRET}", http_status=401),
    ModelGatewayError("model.upstream_error", f"HTTP 403 {SECRET}", http_status=403),
    ModelGatewayError("model.transport_error", SECRET, retryable=True),
    ModelGatewayError("model.timeout", SECRET, retryable=True),
    ModelGatewayError("model.credential_unavailable", SECRET),
    ModelGatewayError("model.adapter_missing", SECRET),
]


@pytest.mark.parametrize("error", LOST, ids=lambda e: e.code + str(e.http_status))
def test_a_planner_connectivity_failure_says_no_model_in_the_thread(
    aworld: AskWorld, error: ModelGatewayError
) -> None:
    aworld.open_audience()
    aworld.world.models.reply = _raise(error)

    aworld.ask("what is going on?")

    (message,) = _public(aworld)
    assert message["text"] == TEXT
    assert message["thread_ts"] == aworld.latest().thread_ts
    assert aworld.slack.ephemerals == []
    assert aworld.world.models.answer_calls == 0
    (decision,) = _decisions(aworld)
    assert decision.metadata["planner"]["status"] == "no_model"
    assert decision.metadata["outcome"] == "refused_unavailable"
    row = aworld.latest()
    assert row.reply_state == "posted" and row.posts == 1
    dump = repr(aworld.slack.calls) + repr([d.metadata for d in _decisions(aworld)])
    assert SECRET not in dump


def test_a_slow_planner_is_told_as_no_model(aworld: AskWorld) -> None:
    aworld.open_audience()
    aworld.world.models.delay = 0.5
    planner = aworld.runtime.task_worker_service.turn_guard._pipeline._planner
    planner._timeout = 0.1

    aworld.ask("what is going on?")

    assert [m["text"] for m in _public(aworld)] == [TEXT]


def test_other_planner_failures_keep_their_own_messages(aworld: AskWorld) -> None:
    aworld.open_audience()
    aworld.world.models.reply = _raise(
        ModelGatewayError("model.upstream_error", SECRET, http_status=500)
    )
    aworld.ask("what is going on?")
    assert [e["text"] for e in aworld.slack.ephemerals] == [messages.PLANNER_FAILED]
    assert _public(aworld) == []


def test_unusable_planner_output_from_a_reachable_model_keeps_the_planner_text(
    aworld: AskWorld,
) -> None:
    aworld.open_audience()
    aworld.world.models.reply = lambda _: "not json at all"
    aworld.ask("what is going on?")
    assert [e["text"] for e in aworld.slack.ephemerals] == [messages.PLANNER_FAILED]
    assert _public(aworld) == []


def test_a_planner_with_no_usable_target_says_no_model(aworld: AskWorld) -> None:
    """The model went away between the dispatcher's check and the worker's pickup."""
    aworld.open_audience()
    aworld.mention("what is going on?")
    aworld.dispatch()
    _mock(aworld)

    aworld.work()
    aworld.reply()

    assert [m["text"] for m in _public(aworld)] == [TEXT]
    assert aworld.slack.ephemerals == []
    assert aworld.world.models.planner_calls == 0


def test_a_late_notice_in_a_told_thread_is_withheld_not_repeated(
    aworld: AskWorld,
) -> None:
    aworld.open_audience()
    aworld.world.models.reply = _raise(LOST[0])
    first = aworld.mention("one?")
    aworld.dispatch()
    aworld.work()
    aworld.reply()
    assert [m["text"] for m in _public(aworld)] == [TEXT]

    aworld.mention("two?", thread_ts=first.message_ts)
    aworld.dispatch()
    aworld.work()
    aworld.reply()

    assert [m["text"] for m in _public(aworld)] == [TEXT]  # not repeated
    row = aworld.latest()
    assert row.reply_state == "posted" and row.run_state == "failed"
    assert aworld.runtime.slack_reply_tick.run() == 0  # settled: nothing to retry
