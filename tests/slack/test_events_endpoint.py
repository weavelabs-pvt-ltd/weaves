"""The signed Events endpoint: verification, url_verification, dedupe and ack."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest

import modules.slack.http.events_route as events_route
from clients.slack.signature import compute_signature
from tests.slack.support import (
    BOT_TOKEN,
    EVENTS,
    SIGNING_SECRET,
    World,
    forbid_database,
    mention,
    running_world,
)


@pytest.fixture
def installed(world: World) -> World:
    world.install()
    world.api.calls.clear()
    world.api.tokens.clear()
    return world


def _inbox(world: World) -> list[Any]:
    return world.runtime.slack_store.list_events()


def test_the_endpoint_is_off_without_a_signing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running_world(
        monkeypatch, WEAVES_SLACK_ENABLED="false", WEAVES_SLACK_SIGNING_SECRET=""
    ) as dark:
        forbid_database(dark, monkeypatch)
        body = json.dumps({"type": "url_verification", "challenge": "c"}).encode()
        response = dark.client.post(EVENTS, content=body, headers=dark.sign(body))
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]
        assert dark.api.calls == []


def test_url_verification_echoes_the_challenge_with_no_database_work(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_database(world, monkeypatch)
    response = world.deliver({"type": "url_verification", "challenge": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc123"}


@pytest.mark.parametrize(
    "kind", ["wrong_secret", "stale", "future", "tampered", "none"]
)
def test_a_forged_or_stale_request_is_refused_and_does_nothing(
    installed: World, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    forbid_database(installed, monkeypatch)
    payload = {"type": "url_verification", "challenge": "secret-challenge"}
    body = json.dumps(payload).encode()
    headers = installed.sign(body)
    if kind == "wrong_secret":
        headers = installed.sign(body, secret="not-the-secret")
    elif kind == "stale":
        headers = installed.sign(body, age=301)
    elif kind == "future":
        headers = installed.sign(body, age=-301)
    elif kind == "tampered":
        body = json.dumps({**payload, "challenge": "other"}).encode()
    elif kind == "none":
        headers = {"content-type": "application/json"}
    response = installed.client.post(EVENTS, content=body, headers=headers)
    assert response.status_code == 401
    assert "challenge" not in response.text
    assert installed.api.calls == []


def test_a_request_just_inside_the_window_is_accepted(installed: World) -> None:
    response = installed.deliver(mention(), age=299)
    assert response.status_code == 200
    assert len(_inbox(installed)) == 1


def test_an_oversized_body_is_refused(installed: World) -> None:
    body = b"{" + b" " * (events_route.MAX_BODY_BYTES + 1) + b"}"
    response = installed.client.post(EVENTS, content=body, headers=installed.sign(body))
    assert response.status_code == 413


def test_an_unknown_team_is_acked_and_dropped(installed: World) -> None:
    response = installed.deliver(mention(team="T0STRANGER", event_id="Ev0UNK001"))
    assert response.status_code == 200
    assert response.json() == {"ok": True}  # no oracle: same answer as a known team
    assert _inbox(installed) == []
    assert installed.api.calls == []


def test_a_mention_is_stored_and_acknowledged_with_eyes(installed: World) -> None:
    started = time.perf_counter()
    response = installed.deliver(mention())
    assert response.status_code == 200
    assert time.perf_counter() - started < 3.0
    (row,) = _inbox(installed)
    assert (row.state, row.event_type, row.org_id) == (
        "pending",
        "app_mention",
        installed.alpha.org_id,
    )
    assert (row.channel_id, row.message_ts) == ("C0CHAN001", "1700000000.000100")
    assert row.text == "<@U0BOT0001> what is open?"
    assert row.request_id == "slack-Ev0AAA001"
    (call,) = installed.api.calls
    assert call["method"] == "reactions.add"
    assert call["params"] == {
        "channel": "C0CHAN001",
        "timestamp": "1700000000.000100",
        "name": "eyes",
    }
    assert installed.api.tokens == [BOT_TOKEN]


def test_a_slack_retry_is_deduplicated(installed: World) -> None:
    first = installed.deliver(mention())
    again = installed.deliver(
        mention(),
        extra={"x-slack-retry-num": "1", "x-slack-retry-reason": "http_timeout"},
    )
    assert first.status_code == again.status_code == 200
    assert len(_inbox(installed)) == 1
    assert installed.api.methods() == ["reactions.add"]  # one reaction, not two


def test_a_retry_that_arrives_first_is_recorded_as_a_later_attempt(
    installed: World,
) -> None:
    installed.deliver(mention(), extra={"x-slack-retry-num": "2"})
    assert [row.attempts for row in _inbox(installed)] == [3]


def test_one_message_under_two_event_ids_is_one_row(installed: World) -> None:
    installed.deliver(mention(event_id="Ev0AAA001"))
    installed.deliver(mention(event_id="Ev0AAA002"))
    assert [row.event_id for row in _inbox(installed)] == ["Ev0AAA001"]


def test_a_failing_reaction_never_blocks_the_ack(installed: World) -> None:
    installed.api.errors["reactions.add"] = "channel_not_found"
    response = installed.deliver(mention())
    assert response.status_code == 200
    assert [row.state for row in _inbox(installed)] == ["pending"]
    assert installed.status()["installations"][0]["status"] == "active"


def test_already_reacted_is_success(installed: World) -> None:
    installed.api.errors["reactions.add"] = "already_reacted"
    assert installed.deliver(mention()).status_code == 200
    assert installed.status()["installations"][0]["status"] == "active"


def test_a_dead_token_marks_the_installation_revoked(installed: World) -> None:
    installed.api.errors["reactions.add"] = "token_revoked"
    assert installed.deliver(mention()).status_code == 200
    (view,) = installed.status()["installations"]
    assert view["status"] == "revoked" and view["needs_reinstall"] is True
    again = installed.deliver(mention(event_id="Ev0AAA009", ts="1700000000.000200"))
    assert again.status_code == 200
    assert len(_inbox(installed)) == 1  # the revoked workspace is unknown now


def test_events_about_the_bot_itself_are_ignored(installed: World) -> None:
    installed.deliver(mention(user="U0BOT0001"))
    assert _inbox(installed) == []
    assert installed.api.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "event_callback", "team_id": "T0ACME001", "event_id": "Ev0X"},
        {**mention(), "event_id": "bad id with spaces"},
        {**mention(), "event": {"type": "app_mention", "user": "U0H"}},
        {**mention(), "event": {"type": "message", "text": "hi"}},
        {"type": "something_else"},
    ],
)
def test_malformed_or_unsupported_events_are_acked_and_stored_nowhere(
    installed: World, payload: dict[str, Any]
) -> None:
    assert installed.deliver(payload).status_code == 200
    assert _inbox(installed) == []
    assert installed.api.calls == []


def test_only_thumbs_reactions_are_kept(installed: World) -> None:
    def reaction(event_id: str, name: str) -> dict[str, Any]:
        return {
            "type": "event_callback",
            "team_id": "T0ACME001",
            "event_id": event_id,
            "event": {
                "type": "reaction_added",
                "user": "U0HUMAN01",
                "reaction": name,
                "item": {"type": "message", "channel": "C0CHAN001", "ts": "1.2"},
            },
        }

    for event_id, name in (("Ev0R1", "+1"), ("Ev0R2", "thumbsdown::skin-tone-3")):
        installed.deliver(reaction(event_id, name))
    installed.deliver(reaction("Ev0R3", "tada"))
    rows = _inbox(installed)
    assert [(r.event_id, r.reaction, r.text) for r in rows] == [
        ("Ev0R1", "+1", None),
        ("Ev0R2", "thumbsdown::skin-tone-3", None),
    ]
    assert installed.api.calls == []  # no acknowledgement for reactions


def test_a_database_failure_is_not_acked_so_slack_retries_safely(
    installed: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = installed.runtime.slack_store
    real = store.record_event
    monkeypatch.setattr(store, "record_event", lambda *_: 1 / 0)
    failed = installed.deliver(mention())
    assert failed.status_code == 503 and failed.headers["retry-after"] == "1"
    monkeypatch.setattr(store, "record_event", real)
    retried = installed.deliver(mention(), extra={"x-slack-retry-num": "1"})
    assert retried.status_code == 200
    assert len(_inbox(installed)) == 1


def test_a_slow_database_cannot_hold_the_ack_past_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events_route, "ACK_BUDGET_SECONDS", 0.2)
    with running_world(monkeypatch) as slow:
        slow.install()
        real = slow.runtime.slack_store.record_event

        def sluggish(event: Any) -> bool:
            time.sleep(0.6)
            return bool(real(event))

        monkeypatch.setattr(slow.runtime.slack_store, "record_event", sluggish)
        started = time.perf_counter()
        response = slow.deliver(mention())
        assert response.status_code == 503
        assert time.perf_counter() - started < 0.55


def test_nothing_logged_carries_message_text_or_tokens(
    installed: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    installed.deliver(mention(text="<@U0BOT0001> sensitive customer question"))
    installed.deliver(mention(), age=900)  # a rejected request is logged too
    product = " ".join(
        record.getMessage() for record in caplog.records if "product" in record.name
    )
    assert "sensitive customer question" not in product
    assert BOT_TOKEN not in product and SIGNING_SECRET not in product
    assert "slack_signature_rejected reason=stale" in product


def test_the_signature_uses_the_raw_bytes_not_a_reencoded_body(
    installed: World,
) -> None:
    # Same JSON, different whitespace and key order: signed as sent, so valid.
    body = (
        b'{ "event_id" : "Ev0RAW001", "team_id":"T0ACME001", '
        b'"type":"event_callback", "event": {"type":"app_mention", '
        b'"user":"U0H","ts":"1.2","channel":"C0C","text":"x"}}'
    )
    stamp = str(int(time.time()))
    headers = {
        "x-slack-request-timestamp": stamp,
        "x-slack-signature": compute_signature(SIGNING_SECRET.encode(), stamp, body),
    }
    response = installed.client.post(EVENTS, content=body, headers=headers)
    assert response.status_code == 200
    assert [row.event_id for row in _inbox(installed)] == ["Ev0RAW001"]
