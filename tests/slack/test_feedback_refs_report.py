"""Reaction feedback: asker references, retention, the analytics read path."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from apps.worker import ticks
from apps.worker.ticks import build_tick_registry
from contracts.v1.slack_feedback import SlackFeedback
from modules.slack.feedback import retention as retention_module
from modules.slack.feedback.asker_ref import PEPPER_PURPOSE, AskerRefs, hash_user
from modules.slack.feedback.report import COLUMNS, LOOKUP_COLUMNS, to_csv
from shared.request_context import reset_request_principal, set_request_identity
from tests.support.operator_scripts import slack_feedback_report
from tests.support.slack_ask_world import AskWorld
from tests.support.slack_feedback_world import ORG, USER, answered, react

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


@contextmanager
def as_org(org: str) -> Iterator[None]:
    token = set_request_identity("local-developer", org, f"ws-{org}")
    try:
        yield
    finally:
        reset_request_principal(token)


def rating(
    n: int, *, org: str = ORG, at: datetime = NOW, **fields: object
) -> SlackFeedback:
    values: dict[str, object] = {
        "id": f"{org}:turn-{n}:hmac:{n}",
        "org_id": org,
        "answer_ref": f"C0CHAN001:1700000{n:03d}.000100",
        "turn_id": f"turn-{n}",
        "task_id": f"task-{n}",
        "request_id": f"slack-Ev{n}",
        "asker_ref": f"hmac:{n}",
        "asker_kind": "hashed",
        "value": 1,
        "at": at,
        "created_at": at,
        "updated_at": at,
        **fields,
    }
    return SlackFeedback(**values)  # type: ignore[arg-type]


def test_the_hash_is_stable_per_org_and_never_the_raw_id() -> None:
    first = hash_user("pepper-a", "U0ASKER01")
    assert first == hash_user("pepper-a", "U0ASKER01")
    assert first != hash_user("pepper-b", "U0ASKER01")  # another organization
    assert first != hash_user("pepper-a", "U0ASKER02")
    assert first.startswith("hmac:") and "U0ASKER01" not in first
    assert len(first) == len("hmac:") + 32


def test_the_pepper_is_created_once_kept_in_the_secret_store_and_never_shown(
    aworld: AskWorld,
) -> None:
    runtime = aworld.runtime
    aworld.world.set_slack_settings()  # an organization that already saved settings
    refs = AskerRefs(access=runtime.slack_access_service, secrets=runtime.secret_store)
    version = runtime.slack_access_service.view()["settings_version"]

    first = refs.resolve(USER)
    # a second worker process with an empty cache reads the same pepper
    again = AskerRefs(access=runtime.slack_access_service, secrets=runtime.secret_store)

    assert again.resolve(USER) == first and first.kind == "hashed"
    ref = runtime.slack_access_service.feedback_pepper_ref()
    assert ref is not None and runtime.secret_store.get(ref)
    assert hash_user(runtime.secret_store.get(ref), USER) == first.ref
    # not a settings change, and never part of what the admin API returns
    assert runtime.slack_access_service.view()["settings_version"] == version
    assert ref not in json.dumps(runtime.slack_access_service.view())
    assert PEPPER_PURPOSE == "slack_feedback_pepper"


def test_two_organizations_never_share_a_reference_for_one_slack_user(
    aworld: AskWorld,
) -> None:
    runtime = aworld.runtime

    def resolve() -> str:
        refs = AskerRefs(
            access=runtime.slack_access_service, secrets=runtime.secret_store
        )
        return refs.resolve(USER).ref

    mine = resolve()
    with as_org("org-other"):
        theirs = resolve()
    assert mine != theirs and mine == resolve()


def test_a_lost_pepper_starts_a_new_keyspace_instead_of_storing_the_raw_id(
    aworld: AskWorld,
) -> None:
    runtime = aworld.runtime
    refs = AskerRefs(access=runtime.slack_access_service, secrets=runtime.secret_store)
    first = refs.resolve(USER).ref
    runtime.secret_store.delete(runtime.slack_access_service.feedback_pepper_ref())
    second = (
        AskerRefs(access=runtime.slack_access_service, secrets=runtime.secret_store)
        .resolve(USER)
        .ref
    )
    assert second != first and USER not in second


def test_an_unreadable_secret_store_stores_nothing(aworld: AskWorld) -> None:
    thread = answered(aworld)

    class Broken:
        def get(self, ref: str) -> str:
            raise RuntimeError("secret store down")

        put = replace = delete = purge_organization = get

    tick = aworld.runtime.slack_feedback_tick
    tick._refs = AskerRefs(access=aworld.runtime.slack_access_service, secrets=Broken())  # type: ignore[arg-type]
    event = react(aworld, thread.answer_ts)
    for _ in range(6):
        tick.run()
    assert list(aworld.runtime.slack_feedback.list()) == []
    (row,) = [e for e in aworld.inbox() if e.event_id == event.event_id]
    assert (row.state, row.drop_reason) == ("dropped", "feedback_error")


def test_retention_is_bounded_per_tick_and_keeps_recent_ratings(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = aworld.runtime.slack_feedback
    for n in range(7):
        repository.put(rating(n, at=NOW - timedelta(days=401 + n)))
    repository.put(rating(50, at=NOW - timedelta(days=399)))
    monkeypatch.setattr(retention_module, "BATCH", 2)
    monkeypatch.setattr(retention_module, "MAX_BATCHES", 2)
    expiry = retention_module.FeedbackRetention(
        aworld.runtime.slack_feedback_admin, days=400, clock=lambda: NOW
    )

    assert expiry.run() == 4  # two batches of two, oldest first
    assert len(repository.list()) == 4
    assert expiry.run() == 3
    assert [r.turn_id for r in repository.list()] == ["turn-50"]
    assert expiry.run() == 0


def test_the_retention_period_follows_the_usage_retention_setting(
    aworld: AskWorld,
) -> None:
    assert aworld.runtime.usage_settings.retention_days == 400
    assert aworld.runtime.slack_feedback_retention._days == 400


def _seed(aworld: AskWorld) -> None:
    repository = aworld.runtime.slack_feedback
    repository.put(rating(1, agent_id="agent-1", model_id="m-1", connector_set="jira"))
    repository.put(
        rating(2, value=-1, agent_id="agent-1", model_id="m-1", connector_set="jira")
    )
    repository.put(rating(3, agent_id="agent-2", model_id="m-2"))
    repository.put(rating(4, org="org-b", at=NOW + timedelta(days=40)))


def test_counts_group_by_period_agent_model_and_connector_set_without_any_asker(
    aworld: AskWorld,
) -> None:
    _seed(aworld)
    admin = aworld.runtime.slack_feedback_admin

    rows = [
        r.__dict__ for r in admin.counts(date(2026, 11, 1), date(2027, 1, 31), by="day")
    ]

    assert [(r["period"], r["agent_id"], r["up"], r["down"]) for r in rows] == [
        ("2026-11-27", "agent-1", 1, 1),
        ("2026-11-27", "agent-2", 1, 0),
        ("2027-01-06", "", 1, 0),
    ]
    by_month = admin.counts(
        date(2026, 11, 1), date(2027, 1, 31), by="month", org_id=ORG
    )
    assert [(r.period, r.up, r.down) for r in by_month] == [
        ("2026-11", 1, 1),
        ("2026-11", 1, 0),
    ]
    with pytest.raises(ValueError):
        admin.counts(date(2026, 12, 1), date(2026, 11, 1), by="day")
    with pytest.raises(ValueError):
        admin.counts(date(2026, 11, 1), date(2026, 11, 2), by="week")


def test_a_request_id_finds_its_rating_with_run_ids_and_no_asker(
    aworld: AskWorld,
) -> None:
    _seed(aworld)
    (row,) = aworld.runtime.slack_feedback_admin.by_request("slack-Ev2")
    assert row["turn_id"] == "turn-2" and row["value"] == -1 and row["org_id"] == ORG
    assert "asker_ref" not in row and "asker_kind" not in row
    assert aworld.runtime.slack_feedback_admin.by_request("slack-nope") == []


def test_the_operator_script_prints_counts_and_lookups_and_refuses_bad_input(
    aworld: AskWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(aworld)
    runtime = SimpleNamespace(slack_feedback_admin=aworld.runtime.slack_feedback_admin)

    assert (
        slack_feedback_report.run(
            ["--by", "day", "--from", "2026-11-01", "--to", "2026-11-30"], runtime
        )
        == 0
    )
    out = capsys.readouterr()
    rows = json.loads(out.out)
    assert {"asker_ref", "asker_kind"}.isdisjoint(rows[0]) and rows[0]["total"] == 2
    assert (
        json.loads(out.err.splitlines()[-1])["event"]
        == "operator.slack_feedback_report"
    )

    assert (
        slack_feedback_report.run(["--request-id", "slack-Ev1", "--csv"], runtime) == 0
    )
    csv_text = capsys.readouterr().out
    assert csv_text.splitlines()[0] == ",".join(LOOKUP_COLUMNS)
    assert "hmac" not in csv_text

    assert (
        slack_feedback_report.run(
            ["--from", "2026-12-02", "--to", "2026-12-01"], runtime
        )
        == 2
    )


def test_csv_cells_that_look_like_formulas_are_neutralized() -> None:
    text = to_csv([{"period": "=1+1", "up": 1}], COLUMNS)
    assert "'=1+1" in text


def test_the_ticks_are_registered_and_the_feedback_one_waits_for_a_paused_worker() -> (
    None
):
    registry = build_tick_registry(schedule_interval=5, lifecycle_interval=30)
    by_name = {t.name: t for t in registry.ticks}
    assert by_name["slack_feedback"].interval_seconds == 2
    assert by_name["slack_feedback_retention"].interval_seconds == 900
    calls: list[str] = []
    services = SimpleNamespace(
        feedback_slack=lambda: calls.append("feedback") or 0,
        purge_slack_feedback=lambda: calls.append("expire") or 0,
        worker_paused=lambda: True,
    )
    ticks._slack_feedback(services)  # type: ignore[arg-type]
    ticks._slack_feedback_retention(services)  # type: ignore[arg-type]
    assert calls == ["expire"]
