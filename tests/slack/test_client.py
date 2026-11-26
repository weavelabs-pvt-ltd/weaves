"""Slack client request shapes (httpx.MockTransport, made-up tokens)."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from clients.slack.client import SlackApiClient
from clients.slack.models import MessageRef

TOKEN = "xoxb-made-up-token"
BASE = "https://slack.test/api"


class Recorder:
    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return (
            self.replies.pop(0)
            if self.replies
            else httpx.Response(200, json={"ok": True})
        )

    def form(self, index: int = 0) -> dict[str, str]:
        raw = self.requests[index].content.decode()
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def make(*replies: httpx.Response) -> tuple[SlackApiClient, Recorder, list[float]]:
    recorder = Recorder(*replies)
    slept: list[float] = []
    client = SlackApiClient(
        httpx.Client(transport=httpx.MockTransport(recorder)),
        api_base_url=BASE + "/",
        sleep=slept.append,
    )
    return client, recorder, slept


def ok(**fields: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **fields})


def test_post_message_request_shape() -> None:
    client, rec, _ = make(ok(channel="C1", ts="1.000001"))
    ref = client.post_message(
        TOKEN, "C1", text="hello", blocks=[{"type": "divider"}], thread_ts="1.0"
    )
    request = rec.requests[0]
    assert ref == MessageRef("C1", "1.000001")
    assert str(request.url) == f"{BASE}/chat.postMessage"
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    form = rec.form()
    assert form["channel"] == "C1"
    assert form["thread_ts"] == "1.0"
    assert json.loads(form["blocks"]) == [{"type": "divider"}]
    assert (form["parse"], form["unfurl_links"], form["unfurl_media"]) == (
        "none",
        "false",
        "false",
    )
    assert TOKEN not in request.content.decode()


def test_update_delete_and_ephemeral_shapes() -> None:
    client, rec, _ = make(ok(channel="C1", ts="2.0"), ok(), ok(message_ts="3.0"))
    assert client.update_message(TOKEN, "C1", "2.0", text="edited") == MessageRef(
        "C1", "2.0"
    )
    client.delete_message(TOKEN, "C1", "2.0")
    assert (
        client.post_ephemeral(TOKEN, "C1", "U9", text="psst", thread_ts="1.0") == "3.0"
    )
    assert [r.url.path for r in rec.requests] == [
        "/api/chat.update",
        "/api/chat.delete",
        "/api/chat.postEphemeral",
    ]
    assert rec.form(0)["ts"] == "2.0"
    assert rec.form(2)["user"] == "U9"


def test_add_reaction_reports_already_reacted_as_false() -> None:
    client, rec, _ = make(
        ok(), httpx.Response(200, json={"ok": False, "error": "already_reacted"})
    )
    assert client.add_reaction(TOKEN, "C1", "1.0", "eyes") is True
    assert client.add_reaction(TOKEN, "C1", "1.0", "eyes") is False
    assert rec.form(0) == {"channel": "C1", "timestamp": "1.0", "name": "eyes"}


def test_oauth_exchange_uses_basic_auth_and_no_bearer() -> None:
    client, rec, _ = make(ok(access_token="xoxb-new"))
    data = client.oauth_v2_access(
        client_id="made-up-id",
        client_secret="made-up-secret",
        code="c0de",
        redirect_uri="https://app.test/cb",
    )
    request = rec.requests[0]
    assert data["access_token"] == "xoxb-new"
    assert request.headers["authorization"].startswith("Basic ")
    assert rec.form() == {"code": "c0de", "redirect_uri": "https://app.test/cb"}
    assert b"made-up-secret" not in request.content


def test_auth_test_and_revoke_send_the_bearer_token() -> None:
    client, rec, _ = make(ok(team="Acme"), ok(revoked=True))
    assert client.auth_test(TOKEN)["team"] == "Acme"
    assert client.auth_revoke(TOKEN)["revoked"] is True
    assert {r.headers["authorization"] for r in rec.requests} == {f"Bearer {TOKEN}"}


def test_conversations_replies_caps_limit_at_fifteen() -> None:
    client, rec, _ = make(
        ok(
            messages=[{"ts": "1.0"}],
            has_more=True,
            response_metadata={"next_cursor": "n1"},
        )
    )
    page = client.conversations_replies(TOKEN, "C1", "1.0", limit=200, cursor="c0")
    assert rec.form()["limit"] == "15"
    assert rec.form()["cursor"] == "c0"
    assert page.messages == [{"ts": "1.0"}]
    assert page.has_more is True
    assert page.next_cursor == "n1"
    with pytest.raises(ValueError):
        client.conversations_replies(TOKEN, "C1", "1.0", limit=0)


def test_conversations_list_pages() -> None:
    client, rec, _ = make(
        ok(channels=[{"id": "C1"}], response_metadata={"next_cursor": ""})
    )
    page = client.conversations_list(TOKEN, limit=5000)
    assert page.channels == [{"id": "C1"}]
    assert page.next_cursor is None
    assert rec.form()["limit"] == "1000"
    assert rec.form()["types"] == "public_channel,private_channel"


def test_set_thread_status_results() -> None:
    client, rec, _ = make(
        ok(),
        httpx.Response(
            200, json={"ok": False, "error": "method_not_supported_for_channel_type"}
        ),
        httpx.Response(200, json={"ok": False, "error": "channel_not_found"}),
    )
    assert client.set_thread_status(TOKEN, "C1", "1.0", "Reading Jira").status == "ok"
    unsupported = client.set_thread_status(TOKEN, "C1", "1.0", "x")
    assert (unsupported.status, unsupported.error) == (
        "unsupported",
        "method_not_supported_for_channel_type",
    )
    assert client.set_thread_status(TOKEN, "C1", "1.0", "x").status == "error"
    assert rec.form(0) == {
        "channel_id": "C1",
        "thread_ts": "1.0",
        "status": "Reading Jira",
    }


def test_redirects_are_not_followed() -> None:
    redirect = httpx.Response(302, headers={"Location": "https://evil.test/api"})
    client = SlackApiClient(
        httpx.Client(
            transport=httpx.MockTransport(lambda _: redirect), follow_redirects=True
        ),
        api_base_url=BASE,
    )
    from clients.slack.errors import SlackApiError

    with pytest.raises(SlackApiError) as caught:
        client.auth_test(TOKEN)
    assert caught.value.code == "redirect_refused"
