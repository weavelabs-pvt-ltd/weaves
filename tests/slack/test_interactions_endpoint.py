"""The signed Interactions endpoint: verification, form decoding, dedupe, the ack."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from clients.slack.signature import compute_signature
from modules.slack.http.interactions_route import INTERACTIONS_PATH
from modules.slack.interactions.payload import (
    BUCKET_SECONDS,
    Parsed,
    Press,
    parse_press,
    press_event_id,
)
from tests.slack.support import (
    SIGNING_SECRET,
    World,
    forbid_database,
    running_world,
)

TEAM = "T0ACME001"
FORM = "application/x-www-form-urlencoded"


def press_payload(
    *,
    action_id: str = "weaves.stop",
    value: str = "ref-abcdefghijklmnop",
    user: str = "U0HUMAN01",
    team: str = TEAM,
    action_ts: str | None = "1700000001.000100",
    message_ts: str = "1700000000.000200",
    thread_ts: str = "1700000000.000100",
) -> dict[str, Any]:
    """A ``block_actions`` payload as Slack sends it for one button press."""
    action: dict[str, Any] = {
        "type": "button",
        "action_id": action_id,
        "block_id": "weaves.run",
        "value": value,
    }
    if action_ts is not None:
        action["action_ts"] = action_ts
    return {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "container": {
            "type": "message",
            "message_ts": message_ts,
            "channel_id": "C0CHAN001",
            "is_ephemeral": False,
        },
        "channel": {"id": "C0CHAN001"},
        "message": {"ts": message_ts, "thread_ts": thread_ts},
        "response_url": "https://hooks.slack.com/actions/T0/1/never-read",
        "actions": [action],
    }


def form(payload: dict[str, Any]) -> bytes:
    """The body Slack sends: one ``payload`` field holding JSON."""
    return urlencode({"payload": json.dumps(payload)}).encode()


def sign(body: bytes, *, secret: str = SIGNING_SECRET, age: int = 0) -> dict[str, str]:
    stamp = str(int(time.time()) - age)
    return {
        "x-slack-request-timestamp": stamp,
        "x-slack-signature": compute_signature(secret.encode(), stamp, body),
        "content-type": FORM,
    }


def post(w: World, payload: dict[str, Any], **kw: Any) -> httpx.Response:
    body = form(payload)
    return w.client.post(INTERACTIONS_PATH, content=body, headers=sign(body, **kw))


def _rows(w: World) -> list[Any]:
    return [
        r for r in w.runtime.slack_store.list_events() if r.event_type == "interaction"
    ]


@pytest.fixture
def installed(world: World) -> World:
    world.install()
    return world


def test_the_endpoint_is_off_without_a_signing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running_world(
        monkeypatch, WEAVES_SLACK_ENABLED="false", WEAVES_SLACK_SIGNING_SECRET=""
    ) as dark:
        forbid_database(dark, monkeypatch)
        response = post(dark, press_payload())
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]


@pytest.mark.parametrize(
    "kind", ["wrong_secret", "stale", "future", "tampered", "none", "malformed"]
)
def test_a_forged_stale_or_unsigned_press_is_refused_and_stores_nothing(
    installed: World, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    forbid_database(installed, monkeypatch)
    body = form(press_payload())
    headers = sign(body)
    if kind == "wrong_secret":
        headers = sign(body, secret="not-the-secret")
    elif kind == "stale":
        headers = sign(body, age=301)
    elif kind == "future":
        headers = sign(body, age=-301)
    elif kind == "tampered":
        body = form(press_payload(value="another-value-here"))
    elif kind == "none":
        headers = {"content-type": FORM}
    else:
        headers = {**headers, "x-slack-signature": "v0=zz"}

    response = installed.client.post(INTERACTIONS_PATH, content=body, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature"}


def test_a_press_inside_the_window_is_acked_and_stored_once(installed: World) -> None:
    response = post(installed, press_payload(), age=299)

    assert response.status_code == 200 and response.content == b""
    (row,) = _rows(installed)
    assert row.state == "pending" and row.team_id == TEAM
    assert (row.user_id, row.channel_id, row.message_ts) == (
        "U0HUMAN01",
        "C0CHAN001",
        "1700000000.000200",
    )
    assert row.thread_ts == "1700000000.000100"
    assert row.event_id == (
        "ia:T0ACME001:U0HUMAN01:weaves.stop:1700000000.000200:1700000001.000100"
    )
    assert (row.text, row.reaction) == ("ref-abcdefghijklmnop", "weaves.stop")
    assert row.org_id == installed.alpha.org_id


def test_a_redelivered_press_is_one_row_and_a_second_press_is_another(
    installed: World,
) -> None:
    for _ in range(3):
        assert post(installed, press_payload()).status_code == 200
    assert len(_rows(installed)) == 1

    post(installed, press_payload(action_ts="1700000009.000100"))
    post(installed, press_payload(action_id="weaves.retry"))
    post(installed, press_payload(user="U0HUMAN02"))

    assert len(_rows(installed)) == 4


def test_without_an_action_ts_a_two_second_bucket_keeps_a_double_click_to_one() -> None:
    press = Press(TEAM, "U1", "weaves.stop", "v", "C1", "1.1", "1.0", None)
    first = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    same_bucket = first.replace(second=1)
    next_bucket = first.replace(second=BUCKET_SECONDS)

    assert press_event_id(press, first) == press_event_id(press, same_bucket)
    assert press_event_id(press, first) != press_event_id(press, next_bucket)
    assert press_event_id(press, first).endswith(f":b{int(first.timestamp()) // 2}")


def test_a_missing_action_ts_is_accepted_through_the_endpoint(
    installed: World,
) -> None:
    assert post(installed, press_payload(action_ts=None)).status_code == 200

    (row,) = _rows(installed)
    assert row.event_id.rsplit(":", 1)[-1].startswith("b")  # the bucket fallback


def test_concurrent_deliveries_of_one_press_store_one_row(installed: World) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(
            pool.map(lambda _: post(installed, press_payload()).status_code, range(6))
        )

    assert codes == [200] * 6 and len(_rows(installed)) == 1


def test_an_unknown_workspace_is_acked_and_nothing_is_stored(installed: World) -> None:
    response = post(installed, press_payload(team="T0NOBODY1"))

    assert response.status_code == 200 and _rows(installed) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "view_submission", "team": {"id": TEAM}},
        {"type": "shortcut", "team": {"id": TEAM}},
        {
            **press_payload(),
            "actions": [{"action_id": "weaves.view_task", "value": "x"}],
        },
        {**press_payload(), "actions": []},
        {**press_payload(), "actions": "nope"},
    ],
)
def test_other_payloads_and_buttons_are_acked_and_ignored(
    installed: World, payload: dict[str, Any]
) -> None:
    assert post(installed, payload).status_code == 200
    assert _rows(installed) == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"team": {"id": "bad id!"}},
        {"user": {"id": ""}},
        {
            "container": {"message_ts": "not-a-ts", "channel_id": "C0CHAN001"},
            "message": {},
        },
    ],
)
def test_a_malformed_press_is_a_400_and_stores_nothing(
    installed: World, mutation: dict[str, Any]
) -> None:
    assert post(installed, {**press_payload(), **mutation}).status_code == 400
    assert _rows(installed) == []


def test_an_odd_button_value_is_malformed(installed: World) -> None:
    assert (
        post(installed, press_payload(value="has spaces and <markup>")).status_code
        == 400
    )
    assert _rows(installed) == []


@pytest.mark.parametrize(
    "raw", [b"", b"payload=", b"payload=%7Bnot-json", b"other=1", b"payload=%5B1%5D"]
)
def test_a_body_without_a_json_object_payload_is_a_400(
    installed: World, raw: bytes
) -> None:
    response = installed.client.post(INTERACTIONS_PATH, content=raw, headers=sign(raw))

    assert response.status_code == 400 and _rows(installed) == []


def test_an_oversized_body_is_refused_before_anything_else(installed: World) -> None:
    raw = b"payload=" + b"a" * (1024 * 1024 + 1)

    response = installed.client.post(INTERACTIONS_PATH, content=raw, headers=sign(raw))

    assert response.status_code == 413


def test_the_bot_pressing_its_own_button_is_ignored(installed: World) -> None:
    post(installed, press_payload(user="U0BOT0001"))

    assert _rows(installed) == []


def test_a_paused_platform_still_stores_the_press(
    installed: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = installed.runtime.platform_controls
    snapshot = controls.snapshot()
    monkeypatch.setattr(
        controls,
        "snapshot",
        lambda: type(snapshot)(**{**vars(snapshot), "admissions_paused": True}),
    )

    assert post(installed, press_payload()).status_code == 200

    (row,) = _rows(installed)
    assert row.state == "pending"  # Stop must keep working during maintenance


def test_a_quarantined_organization_still_stores_the_press(installed: World) -> None:
    lifecycle = installed.runtime.organization_lifecycle_service
    lifecycle.suspend(installed.alpha.org_id, "ops@weaves.test", "pause")

    assert post(installed, press_payload()).status_code == 200

    (row,) = _rows(installed)
    assert row.state == "pending"


def test_a_slow_database_does_not_hold_the_ack_past_the_budget(
    installed: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(*_a: Any, **_k: Any) -> bool:
        time.sleep(0.6)
        return True

    monkeypatch.setattr(installed.runtime.slack_store, "record_event", slow)
    for route in installed.client.app.routes:  # a short budget for the test
        endpoint = getattr(route, "endpoint", None)
        if getattr(route, "path", "") == INTERACTIONS_PATH:
            endpoint._budget = 0.1

    response = post(installed, press_payload())

    assert response.status_code == 503 and response.headers["retry-after"] == "1"


def test_the_response_url_is_never_stored_or_logged(
    installed: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    post(installed, press_payload())

    assert "hooks.slack.com" not in caplog.text
    assert "never-read" not in repr(_rows(installed))


def test_the_parser_reads_only_our_buttons() -> None:
    assert parse_press(press_payload()).press is not None
    assert parse_press(press_payload(action_id="weaves.retry")).press is not None
    assert parse_press(press_payload(action_id="weaves.confirm")) == Parsed()
    # Another message type's container still resolves the channel and message.
    payload = press_payload()
    payload["container"] = {"message_ts": "1700000000.000200"}
    parsed = parse_press(payload)
    assert parsed.press is not None and parsed.press.channel_id == "C0CHAN001"
