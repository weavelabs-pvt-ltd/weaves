"""The fake Slack Web API: methods, errors, rate limits, faults (no real Slack)."""

import pytest
from fastapi.testclient import TestClient

from clients.slack.client import SlackApiClient
from clients.slack.errors import SlackApiError, SlackRateLimited, SlackTokenError
from tenancy_harness.fake_slack_state import DEFAULT_CODE
from tests.tenancy.conftest import FakeClock

CHANNEL = "C0FAKE001"
PARENT = "1700000000.900001"
USER_MESSAGE = {"channel": CHANNEL, "ts": PARENT, "text": "@Weaves hello", "user": "U1"}


def call(slack: TestClient, method: str, token: str | None, **params: object):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return slack.post(f"/slack/api/{method}", data=params, headers=headers)


def seed(slack: TestClient, **extra: object) -> None:
    body = {"messages": [USER_MESSAGE], **extra}
    assert slack.post("/slack/_control/configure", json=body).status_code == 200


def test_oauth_v2_access_returns_bot_and_user_tokens(slack: TestClient) -> None:
    response = slack.post("/slack/api/oauth.v2.access", data={"code": DEFAULT_CODE})
    data = response.json()
    assert data["ok"] is True
    assert data["is_enterprise_install"] is False
    assert data["team"]["id"] == "T0FAKE001"
    assert data["access_token"].startswith("xoxb-fixture-")
    assert data["authed_user"]["access_token"].startswith("xoxp-fixture-")
    assert (
        call(slack, "auth.test", data["access_token"]).json()["team_id"] == "T0FAKE001"
    )


def test_oauth_rejects_unknown_codes_and_models_grid_installs(
    slack: TestClient,
) -> None:
    assert slack.post("/slack/api/oauth.v2.access", data={"code": "nope"}).json() == {
        "ok": False,
        "error": "invalid_code",
    }
    slack.post(
        "/slack/_control/configure",
        json={"codes": {"grid-code": {"team": "T0GRID", "enterprise": True}}},
    )
    grid = slack.post("/slack/api/oauth.v2.access", data={"code": "grid-code"}).json()
    assert grid["is_enterprise_install"] is True
    assert grid["team"] is None


def test_unknown_token_is_invalid_auth_and_revoke_kills_a_token(
    slack: TestClient, bot_token: str
) -> None:
    assert call(slack, "auth.test", "xoxb-unknown").json()["error"] == "invalid_auth"
    assert call(slack, "auth.test", None).json()["error"] == "invalid_auth"
    assert call(slack, "auth.revoke", bot_token).json()["revoked"] is True
    assert call(slack, "auth.test", bot_token).json()["error"] == "token_revoked"


def test_unknown_method(slack: TestClient, bot_token: str) -> None:
    assert call(slack, "files.upload", bot_token).json()["error"] == "unknown_method"


def test_post_message_in_thread_and_errors(slack: TestClient, bot_token: str) -> None:
    seed(slack)
    posted = call(
        slack,
        "chat.postMessage",
        bot_token,
        channel=CHANNEL,
        text="hi",
        thread_ts=PARENT,
    ).json()
    assert posted["ok"] is True
    assert posted["message"]["thread_ts"] == PARENT
    assert (
        call(slack, "chat.postMessage", bot_token, channel="C404", text="x").json()[
            "error"
        ]
        == "channel_not_found"
    )
    assert (
        call(slack, "chat.postMessage", bot_token, channel=CHANNEL).json()["error"]
        == "no_text"
    )
    missing = call(
        slack, "chat.postMessage", bot_token, channel=CHANNEL, text="x", thread_ts="9.9"
    ).json()
    assert missing["error"] == "thread_not_found"


def test_blocks_are_stored_and_json_bodies_work(
    slack: TestClient, bot_token: str
) -> None:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    response = slack.post(
        "/slack/api/chat.postMessage",
        json={"channel": CHANNEL, "text": "x", "blocks": blocks},
        headers={"Authorization": f"Bearer {bot_token}"},
    )
    assert response.json()["message"]["blocks"] == blocks


def test_update_and_delete_rules(slack: TestClient, bot_token: str) -> None:
    seed(slack)
    ts = call(slack, "chat.postMessage", bot_token, channel=CHANNEL, text="one").json()[
        "ts"
    ]
    assert call(
        slack, "chat.update", bot_token, channel=CHANNEL, ts=ts, text="two"
    ).json()["ok"]
    state = slack.get("/slack/_control/state").json()
    assert [m["text"] for m in state["messages"][CHANNEL] if m["ts"] == ts] == ["two"]
    human = call(
        slack, "chat.update", bot_token, channel=CHANNEL, ts=PARENT, text="x"
    ).json()
    assert human["error"] == "cant_update_message"
    assert (
        call(slack, "chat.delete", bot_token, channel=CHANNEL, ts=PARENT).json()[
            "error"
        ]
        == "cant_delete_message"
    )
    assert (
        call(
            slack, "chat.update", bot_token, channel=CHANNEL, ts="1.1", text="x"
        ).json()["error"]
        == "message_not_found"
    )
    assert (
        call(slack, "chat.delete", bot_token, channel=CHANNEL, ts=ts).json()["ok"]
        is True
    )
    after = slack.get("/slack/_control/state").json()["messages"][CHANNEL]
    assert all(m["ts"] != ts for m in after)


def test_post_ephemeral_is_recorded_privately(
    slack: TestClient, bot_token: str
) -> None:
    seed(slack)
    sent = call(
        slack, "chat.postEphemeral", bot_token, channel=CHANNEL, user="U1", text="psst"
    )
    assert sent.json()["ok"] is True
    state = slack.get("/slack/_control/state").json()
    assert state["ephemerals"][0]["user"] == "U1"
    assert all(m["text"] != "psst" for m in state["messages"][CHANNEL])
    assert (
        call(slack, "chat.postEphemeral", bot_token, channel=CHANNEL, text="x").json()[
            "ok"
        ]
        is False
    )


def test_reactions_add_and_already_reacted(slack: TestClient, bot_token: str) -> None:
    seed(slack)
    args = {"channel": CHANNEL, "timestamp": PARENT, "name": "eyes"}
    assert call(slack, "reactions.add", bot_token, **args).json()["ok"] is True
    assert (
        call(slack, "reactions.add", bot_token, **args).json()["error"]
        == "already_reacted"
    )
    missing = call(
        slack, "reactions.add", bot_token, channel=CHANNEL, timestamp="1.1", name="x"
    )
    assert missing.json()["error"] == "message_not_found"


def test_thread_status_supported_clears_on_reply_and_expires(
    slack: TestClient, bot_token: str, clock: FakeClock
) -> None:
    seed(slack)
    args = {"channel_id": CHANNEL, "thread_ts": PARENT, "status": "Reading Jira"}
    assert (
        call(slack, "assistant.threads.setStatus", bot_token, **args).json()["ok"]
        is True
    )
    key = f"{CHANNEL}|{PARENT}"
    assert slack.get("/slack/_control/state").json()["statuses"] == {
        key: "Reading Jira"
    }
    call(
        slack,
        "chat.postMessage",
        bot_token,
        channel=CHANNEL,
        text="done",
        thread_ts=PARENT,
    )
    assert slack.get("/slack/_control/state").json()["statuses"] == {}
    call(slack, "assistant.threads.setStatus", bot_token, **args)
    clock.advance(119)
    assert key in slack.get("/slack/_control/state").json()["statuses"]
    clock.advance(2)
    assert slack.get("/slack/_control/state").json()["statuses"] == {}


def test_thread_status_empty_clears_and_unsupported_mode(
    slack: TestClient, bot_token: str
) -> None:
    seed(slack)
    args = {"channel_id": CHANNEL, "thread_ts": PARENT}
    call(slack, "assistant.threads.setStatus", bot_token, status="x", **args)
    call(slack, "assistant.threads.setStatus", bot_token, status="", **args)
    assert slack.get("/slack/_control/state").json()["statuses"] == {}
    slack.post("/slack/_control/configure", json={"status_mode": "unsupported"})
    refused = call(slack, "assistant.threads.setStatus", bot_token, status="x", **args)
    assert refused.json()["error"] == "method_not_supported_for_channel_type"


def test_conversations_replies_is_one_request_per_minute_per_workspace(
    slack: TestClient, bot_token: str, clock: FakeClock
) -> None:
    seed(slack)
    first = call(slack, "conversations.replies", bot_token, channel=CHANNEL, ts=PARENT)
    assert first.status_code == 200
    assert first.json()["messages"][0]["ts"] == PARENT
    second = call(slack, "conversations.replies", bot_token, channel=CHANNEL, ts=PARENT)
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"
    assert second.json() == {"ok": False, "error": "ratelimited"}
    clock.advance(59)
    third = call(slack, "conversations.replies", bot_token, channel=CHANNEL, ts=PARENT)
    assert third.status_code == 429
    assert third.headers["Retry-After"] == "1"
    clock.advance(1)
    assert (
        call(
            slack, "conversations.replies", bot_token, channel=CHANNEL, ts=PARENT
        ).status_code
        == 200
    )


def test_conversations_replies_limit_is_clamped_to_fifteen_with_paging(
    slack: TestClient, bot_token: str, clock: FakeClock
) -> None:
    replies = [
        {
            "channel": CHANNEL,
            "ts": f"1700000001.{i:06d}",
            "thread_ts": PARENT,
            "text": str(i),
        }
        for i in range(20)
    ]
    seed(slack, messages=[USER_MESSAGE, *replies])
    page = call(
        slack,
        "conversations.replies",
        bot_token,
        channel=CHANNEL,
        ts=PARENT,
        limit="200",
    )
    data = page.json()
    assert len(data["messages"]) == 15
    assert data["has_more"] is True
    clock.advance(60)
    cursor = data["response_metadata"]["next_cursor"]
    rest = call(
        slack,
        "conversations.replies",
        bot_token,
        channel=CHANNEL,
        ts=PARENT,
        cursor=cursor,
    ).json()
    assert len(rest["messages"]) == 6
    assert rest["has_more"] is False


def test_other_workspaces_have_their_own_replies_budget(
    slack: TestClient, bot_token: str
) -> None:
    seed(slack)
    other = slack.post(
        "/slack/_control/configure", json={"codes": {"c2": {"team": "T0OTHER"}}}
    )
    assert other.status_code == 200
    token = slack.post("/slack/api/oauth.v2.access", data={"code": "c2"}).json()[
        "access_token"
    ]
    assert (
        call(
            slack, "conversations.replies", bot_token, channel=CHANNEL, ts=PARENT
        ).status_code
        == 200
    )
    assert (
        call(
            slack, "conversations.replies", token, channel=CHANNEL, ts=PARENT
        ).status_code
        == 200
    )


def test_conversations_list_filters_private_channels(
    slack: TestClient, bot_token: str
) -> None:
    both = call(
        slack, "conversations.list", bot_token, types="public_channel,private_channel"
    )
    assert [c["name"] for c in both.json()["channels"]] == ["general", "secret"]
    public = call(slack, "conversations.list", bot_token, types="public_channel")
    assert [c["name"] for c in public.json()["channels"]] == ["general"]
    paged = call(
        slack,
        "conversations.list",
        bot_token,
        types="public_channel,private_channel",
        limit="1",
    )
    assert paged.json()["response_metadata"]["next_cursor"] == "1"


def test_response_url_allows_five_uses_in_thirty_minutes(
    slack: TestClient, clock: FakeClock
) -> None:
    codes = [
        slack.post("/slack/response/R1", json={"text": "x"}).status_code
        for _ in range(6)
    ]
    assert codes == [200] * 5 + [404]
    clock.advance(31 * 60)
    assert slack.post("/slack/response/R2", json={}).status_code == 200
    clock.advance(31 * 60)
    assert slack.post("/slack/response/R2", json={}).status_code == 404


def test_calls_are_recorded_without_tokens_or_codes(
    slack: TestClient, bot_token: str
) -> None:
    seed(slack)
    call(slack, "chat.postMessage", bot_token, channel=CHANNEL, text="hi")
    slack.post(
        "/slack/api/oauth.v2.access", data={"code": DEFAULT_CODE, "client_secret": "s"}
    )
    calls = slack.get("/slack/_control/calls").json()["calls"]
    assert [c["method"] for c in calls] == ["chat.postMessage", "oauth.v2.access"]
    assert calls[0]["params"] == {"channel": CHANNEL, "text": "hi"}
    assert calls[0]["team"] == "T0FAKE001"
    assert DEFAULT_CODE not in str(calls) and bot_token not in str(calls)


@pytest.mark.parametrize(
    ("fault", "status"),
    [({"kind": "500"}, 500), ({"kind": "429", "retry_after": 7}, 429)],
)
def test_api_fault_injection_500_and_429(
    slack: TestClient, bot_token: str, fault: dict, status: int
) -> None:
    slack.post("/slack/_control/configure", json={"api_fault": {**fault, "count": 1}})
    broken = call(slack, "auth.test", bot_token)
    assert broken.status_code == status
    if status == 429:
        assert broken.headers["Retry-After"] == "7"
    assert call(slack, "auth.test", bot_token).json()["ok"] is True


def test_fault_can_target_one_method_and_stay_sticky(
    slack: TestClient, bot_token: str
) -> None:
    slack.post(
        "/slack/_control/configure",
        json={"api_fault": {"kind": "500", "method": "chat.postMessage"}},
    )
    assert call(slack, "auth.test", bot_token).status_code == 200
    for _ in range(3):
        assert (
            call(
                slack, "chat.postMessage", bot_token, channel=CHANNEL, text="x"
            ).status_code
            == 500
        )
    slack.post("/slack/_control/configure", json={"api_fault": None})
    assert (
        call(
            slack, "chat.postMessage", bot_token, channel=CHANNEL, text="x"
        ).status_code
        == 200
    )


def test_slow_fault_sleeps_on_the_injected_clock(
    slack: TestClient, bot_token: str, clock: FakeClock
) -> None:
    slack.post(
        "/slack/_control/configure",
        json={"api_fault": {"kind": "slow", "delay_seconds": 4, "count": 1}},
    )
    assert call(slack, "auth.test", bot_token).json()["ok"] is True
    assert clock.sleeps == [4.0]


def test_configure_rejects_unknown_keys_and_bad_values(slack: TestClient) -> None:
    for body in (
        {"mystery": 1},
        {"api_fault": {"kind": "teapot"}},
        {"delivery_fault": "bogus"},
        {"status_mode": "maybe"},
    ):
        assert slack.post("/slack/_control/configure", json=body).status_code == 422


def test_reset_restores_seeded_state(slack: TestClient, bot_token: str) -> None:
    seed(slack)
    call(slack, "auth.revoke", bot_token)
    assert slack.post("/slack/_control/reset").json() == {"reset": True}
    assert call(slack, "auth.test", bot_token).json()["ok"] is True
    assert (
        slack.get("/slack/_control/calls").json()["calls"][0]["method"] == "auth.test"
    )


def test_the_real_client_works_against_the_fake(
    slack: TestClient, bot_token: str
) -> None:
    seed(slack)
    client = SlackApiClient(
        slack, api_base_url="http://testserver/slack/api", sleep=lambda _: None
    )
    ref = client.post_message(bot_token, CHANNEL, text="hello", thread_ts=PARENT)
    assert client.update_message(bot_token, CHANNEL, ref.ts, text="edited") == ref
    assert client.add_reaction(bot_token, CHANNEL, PARENT, "eyes") is True
    assert client.add_reaction(bot_token, CHANNEL, PARENT, "eyes") is False
    assert (
        client.set_thread_status(bot_token, CHANNEL, PARENT, "Working").status == "ok"
    )
    page = client.conversations_replies(bot_token, CHANNEL, PARENT, limit=100)
    assert [m["text"] for m in page.messages] == ["@Weaves hello", "edited"]
    with pytest.raises(SlackRateLimited) as limited:
        client.conversations_replies(bot_token, CHANNEL, PARENT)
    assert limited.value.retry_after == 60.0
    assert [c["name"] for c in client.conversations_list(bot_token).channels] == [
        "general",
        "secret",
    ]
    client.delete_message(bot_token, CHANNEL, ref.ts)
    with pytest.raises(SlackApiError) as missing:
        client.delete_message(bot_token, CHANNEL, ref.ts)
    assert missing.value.code == "message_not_found"
    client.auth_revoke(bot_token)
    with pytest.raises(SlackTokenError):
        client.auth_test(bot_token)


def test_the_client_retries_a_fault_injected_429_then_succeeds(
    slack: TestClient, bot_token: str
) -> None:
    slack.post(
        "/slack/_control/configure",
        json={"api_fault": {"kind": "429", "retry_after": 2, "count": 1}},
    )
    slept: list[float] = []
    client = SlackApiClient(
        slack, api_base_url="http://testserver/slack/api", sleep=slept.append
    )
    assert client.auth_test(bot_token)["ok"] is True
    assert slept == [2.0]


def test_the_product_fake_app_serves_the_slack_routes() -> None:
    from tenancy_harness.fakes import app

    # No ``with``: the lifespan would bind the plain-HTTP port used by the stack.
    client = TestClient(app)
    assert client.post("/slack/_control/reset").json() == {"reset": True}
    assert client.get("/health").json() == {"fixture": "tenancy-phase-0"}
