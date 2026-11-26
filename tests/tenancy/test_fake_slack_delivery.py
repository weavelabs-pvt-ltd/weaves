"""The fake Slack's delivery model: signing, 3 s ack, retries, url_verification."""

import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from clients.slack.errors import SlackSignatureError
from clients.slack.signature import verify_slack_request
from tenancy_harness.fake_slack_state import STATE, signing_secret
from tests.tenancy.conftest import FakeClock

STUB = "http://testserver/slack/_stub/events"
TARGET = "http://target.test/events"
EVENT = {"type": "event_callback", "event_id": "Ev0001", "team_id": "T0FAKE001"}


class Target:
    """A Request URL whose responses a test scripts; records what it received."""

    def __init__(
        self, clock: FakeClock, *script: tuple[int, float, dict[str, str]]
    ) -> None:
        self.clock = clock
        self.script = list(script)
        self.seen: list[dict] = []
        self.app = FastAPI()
        self.app.post("/events")(self.handle)

    async def handle(self, request: Request) -> Response:
        raw = await request.body()
        self.seen.append({"headers": dict(request.headers), "body": raw})
        status, delay, headers = self.script.pop(0) if self.script else (200, 0.0, {})
        self.clock.advance(delay)
        return Response(status_code=status, headers=headers)


def route_to(target: Target) -> None:
    STATE.transport = httpx.ASGITransport(app=target.app)


def deliver(slack: TestClient, url: str, payload: dict | None = None) -> dict:
    body = {"url": url, "payload": payload or EVENT}
    response = slack.post("/slack/_control/deliver_event", json=body)
    assert response.status_code == 200
    return response.json()


def test_url_verification_challenge_is_echoed_after_the_signature_passes(
    slack: TestClient,
) -> None:
    report = deliver(slack, STUB, {"type": "url_verification", "challenge": "3eZbrw1a"})
    assert report["delivered"] is True
    assert report["challenge_echoed"] is True
    assert len(report["attempts"]) == 1
    assert report["attempts"][0]["status"] == 200


def test_the_stub_rejects_a_forged_url_verification(slack: TestClient) -> None:
    slack.post("/slack/_control/configure", json={"delivery_fault": "bad_signature"})
    report = deliver(slack, STUB, {"type": "url_verification", "challenge": "abc"})
    assert report["delivered"] is False
    assert report["challenge_echoed"] is None
    assert {a["status"] for a in report["attempts"]} == {401}


def test_delivery_is_signed_for_the_product_verifier(
    slack: TestClient, clock: FakeClock
) -> None:
    target = Target(clock)
    route_to(target)
    assert deliver(slack, TARGET)["delivered"] is True
    received = target.seen[0]
    verify_slack_request(
        signing_secret(), received["headers"], received["body"], clock.now
    )
    assert json.loads(received["body"]) == EVENT
    assert "x-slack-retry-num" not in received["headers"]


def test_stale_timestamp_and_bad_signature_faults_are_detected_by_the_verifier(
    slack: TestClient, clock: FakeClock
) -> None:
    target = Target(clock)
    route_to(target)
    for fault, reason in (
        ("stale_timestamp", "stale"),
        ("bad_signature", "bad_signature"),
    ):
        slack.post("/slack/_control/configure", json={"delivery_fault": fault})
        target.seen.clear()
        deliver(slack, TARGET)
        got = target.seen[0]
        with pytest.raises(SlackSignatureError) as caught:
            verify_slack_request(
                signing_secret(), got["headers"], got["body"], clock.now
            )
        assert caught.value.reason == reason


def test_a_fast_2xx_is_one_attempt_with_no_retries(
    slack: TestClient, clock: FakeClock
) -> None:
    target = Target(clock, (200, 2.9, {}))
    route_to(target)
    report = deliver(slack, TARGET)
    assert report["delivered"] is True
    assert report["attempts"][0]["ack_seconds"] == 2.9
    assert len(target.seen) == 1


def test_slower_than_three_seconds_is_a_timeout_and_retried_on_schedule(
    slack: TestClient, clock: FakeClock
) -> None:
    target = Target(clock, (200, 3.5, {}), (200, 0.1, {}))
    route_to(target)
    report = deliver(slack, TARGET)
    first, second = report["attempts"]
    assert (first["ok"], first["reason"]) == (False, "http_timeout")
    assert first["ack_seconds"] == 3.5
    assert second["ok"] is True
    assert second["sent_headers"]["x-slack-retry-num"] == "1"
    assert second["sent_headers"]["x-slack-retry-reason"] == "http_timeout"
    assert report["delivered"] is True
    assert clock.sleeps == [0]
    # each attempt is signed afresh with the then-current timestamp
    stamps = [t["headers"]["x-slack-request-timestamp"] for t in target.seen]
    assert stamps[0] != stamps[1]


def test_non_2xx_exhausts_all_retries_on_the_test_schedule(
    slack: TestClient, clock: FakeClock
) -> None:
    target = Target(clock, *[(500, 0.0, {})] * 4)
    route_to(target)
    report = deliver(slack, TARGET)
    assert report["delivered"] is False
    assert len(report["attempts"]) == 4
    assert [a["retry_num"] for a in report["attempts"]] == [0, 1, 2, 3]
    assert {a["reason"] for a in report["attempts"]} == {"http_error"}
    assert clock.sleeps == [0, 1, 5]
    assert [t["headers"].get("x-slack-retry-num") for t in target.seen] == [
        None,
        "1",
        "2",
        "3",
    ]
    assert {t["headers"].get("x-slack-retry-reason") for t in target.seen[1:]} == {
        "http_error"
    }
    # every retry carries the same body (same event_id)
    assert len({t["body"] for t in target.seen}) == 1


def test_real_schedule_is_configurable(slack: TestClient, clock: FakeClock) -> None:
    slack.post(
        "/slack/_control/configure", json={"retry_schedule_seconds": [0, 60, 300]}
    )
    target = Target(clock, *[(503, 0.0, {})] * 4)
    route_to(target)
    deliver(slack, TARGET)
    assert clock.sleeps == [0, 60, 300]


def test_no_retry_header_stops_retries(slack: TestClient, clock: FakeClock) -> None:
    target = Target(clock, (500, 0.0, {"x-slack-no-retry": "1"}))
    route_to(target)
    report = deliver(slack, TARGET)
    assert report["delivered"] is False
    assert len(report["attempts"]) == 1
    assert report["attempts"][0]["no_retry"] is True
    assert clock.sleeps == []


def test_unreachable_target_is_a_connection_failure_with_retries(
    slack: TestClient, clock: FakeClock
) -> None:
    class Refuse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

    STATE.transport = Refuse()
    report = deliver(slack, TARGET)
    assert [a["reason"] for a in report["attempts"]] == ["connection_failed"] * 4
    assert report["delivered"] is False


def test_recovery_on_the_third_attempt_stops_retrying(
    slack: TestClient, clock: FakeClock
) -> None:
    target = Target(clock, (500, 0.0, {}), (502, 0.0, {}), (200, 0.0, {}))
    route_to(target)
    report = deliver(slack, TARGET)
    assert report["delivered"] is True
    assert len(report["attempts"]) == 3
    assert len(target.seen) == 3


def test_interaction_is_form_encoded_signed_not_retried_and_gets_a_response_url(
    slack: TestClient, clock: FakeClock
) -> None:
    target = Target(clock, (500, 0.0, {}))
    route_to(target)
    payload = {
        "type": "block_actions",
        "actions": [{"action_id": "weaves.stop", "value": "r1"}],
    }
    response = slack.post(
        "/slack/_control/deliver_interaction", json={"url": TARGET, "payload": payload}
    )
    report = response.json()
    assert report["delivered"] is False
    assert len(report["attempts"]) == 1
    assert len(target.seen) == 1
    got = target.seen[0]
    assert got["headers"]["content-type"] == "application/x-www-form-urlencoded"
    verify_slack_request(signing_secret(), got["headers"], got["body"], clock.now)
    form = parse_qs(got["body"].decode())
    sent = json.loads(form["payload"][0])
    assert sent["actions"] == payload["actions"]
    assert sent["response_url"].startswith("http://testserver/slack/response/")


def test_interaction_keeps_a_supplied_response_url_and_applies_faults(
    slack: TestClient, clock: FakeClock
) -> None:
    target = Target(clock)
    route_to(target)
    slack.post("/slack/_control/configure", json={"delivery_fault": "stale_timestamp"})
    payload = {"type": "block_actions", "response_url": "https://hooks.example/x"}
    slack.post(
        "/slack/_control/deliver_interaction", json={"url": TARGET, "payload": payload}
    )
    got = target.seen[0]
    sent = json.loads(parse_qs(got["body"].decode())["payload"][0])
    assert sent["response_url"] == "https://hooks.example/x"
    with pytest.raises(SlackSignatureError) as caught:
        verify_slack_request(signing_secret(), got["headers"], got["body"], clock.now)
    assert caught.value.reason == "stale"


def test_slow_interaction_ack_is_flagged(slack: TestClient, clock: FakeClock) -> None:
    target = Target(clock, (200, 3.2, {}))
    route_to(target)
    report = slack.post(
        "/slack/_control/deliver_interaction",
        json={"url": TARGET, "payload": {"type": "block_actions"}},
    ).json()
    assert report["delivered"] is False
    assert report["attempts"][0]["reason"] == "http_timeout"
    assert len(target.seen) == 1


def test_deliveries_are_listed_in_state_without_signatures(
    slack: TestClient, clock: FakeClock
) -> None:
    deliver(slack, STUB)
    state = slack.get("/slack/_control/state").json()
    assert len(state["deliveries"]) == 1
    assert "x-slack-signature" not in json.dumps(state["deliveries"])
