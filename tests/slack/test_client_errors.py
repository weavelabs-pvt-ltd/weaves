"""Slack client retries, 429 handling, error mapping and secret hygiene."""

import logging

import httpx
import pytest

from clients.slack.client import SlackApiClient
from clients.slack.errors import SlackApiError, SlackRateLimited, SlackTokenError

TOKEN = "xoxb-made-up-token-9f3a"
SECRET = "made-up-client-secret-77c1"


def limited(seconds: str = "2") -> httpx.Response:
    return httpx.Response(
        429,
        headers={"Retry-After": seconds},
        json={"ok": False, "error": "ratelimited"},
    )


def build(*replies: httpx.Response, **kwargs):
    queue = list(replies)
    seen: list[httpx.Request] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    client = SlackApiClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        api_base_url="https://slack.test/api",
        sleep=slept.append,
        **kwargs,
    )
    return client, seen, slept


def test_one_retry_after_ratelimited_honours_retry_after() -> None:
    client, seen, slept = build(
        limited("3"), httpx.Response(200, json={"ok": True, "team": "A"})
    )
    assert client.auth_test(TOKEN)["team"] == "A"
    assert len(seen) == 2
    assert slept == [3.0]


def test_second_429_raises_after_exactly_one_retry() -> None:
    client, seen, slept = build(limited("2"), limited("4"))
    with pytest.raises(SlackRateLimited) as caught:
        client.auth_test(TOKEN)
    assert caught.value.retry_after == 4.0
    assert caught.value.code == "ratelimited"
    assert len(seen) == 2
    assert slept == [2.0]


def test_long_retry_after_is_not_waited_out() -> None:
    client, seen, slept = build(limited("60"))
    with pytest.raises(SlackRateLimited) as caught:
        client.conversations_replies(TOKEN, "C1", "1.0")
    assert caught.value.retry_after == 60.0
    assert len(seen) == 1
    assert slept == []


def test_missing_or_garbled_retry_after_defaults_to_one_second() -> None:
    bare = httpx.Response(429, json={"ok": False, "error": "ratelimited"})
    client, _, slept = build(bare, httpx.Response(200, json={"ok": True}))
    client.auth_test(TOKEN)
    assert slept == [1.0]


@pytest.mark.parametrize("code", ["invalid_auth", "token_revoked", "account_inactive"])
def test_token_errors_map_to_slack_token_error(code: str) -> None:
    client, _, _ = build(httpx.Response(200, json={"ok": False, "error": code}))
    with pytest.raises(SlackTokenError) as caught:
        client.post_message(TOKEN, "C1", text="hi")
    assert caught.value.code == code
    assert isinstance(caught.value, SlackApiError)


def test_other_slack_errors_keep_their_code() -> None:
    client, _, _ = build(
        httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
    )
    with pytest.raises(SlackApiError) as caught:
        client.post_message(TOKEN, "C1", text="hi")
    assert type(caught.value) is SlackApiError
    assert caught.value.code == "channel_not_found"
    assert caught.value.method == "chat.postMessage"


def test_status_errors_that_are_not_unsupported_do_not_hide_token_problems() -> None:
    client, _, _ = build(
        httpx.Response(200, json={"ok": False, "error": "token_revoked"})
    )
    with pytest.raises(SlackTokenError):
        client.set_thread_status(TOKEN, "C1", "1.0", "x")


def test_rate_limit_on_status_propagates() -> None:
    client, _, _ = build(limited("90"))
    with pytest.raises(SlackRateLimited):
        client.set_thread_status(TOKEN, "C1", "1.0", "x")


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(500, text="oops"), "http_500"),
        (httpx.Response(503, json={"ok": False}), "http_503"),
        (httpx.Response(200, text="not json"), "invalid_response"),
        (httpx.Response(200, json=["list"]), "invalid_response"),
        (httpx.Response(200, json={"ok": False}), "unknown_error"),
        (httpx.Response(200, json={"ok": False, "error": ""}), "unknown_error"),
    ],
)
def test_unusable_responses_map_to_codes(response: httpx.Response, code: str) -> None:
    client, _, _ = build(response)
    with pytest.raises(SlackApiError) as caught:
        client.auth_test(TOKEN)
    assert caught.value.code == code


def test_post_message_without_channel_and_ts_in_reply_is_invalid() -> None:
    client, _, _ = build(httpx.Response(200, json={"ok": True}))
    with pytest.raises(SlackApiError) as caught:
        client.post_message(TOKEN, "C1", text="hi")
    assert caught.value.code == "invalid_response"


def test_transport_failure_maps_without_chaining_details() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url} with {TOKEN}")

    client = SlackApiClient(
        httpx.Client(transport=httpx.MockTransport(boom)),
        api_base_url="https://slack.test/api",
    )
    with pytest.raises(SlackApiError) as caught:
        client.auth_test(TOKEN)
    assert caught.value.code == "network_error"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert TOKEN not in str(caught.value)


def test_no_secret_in_logs_or_exception_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client, _, _ = build(
        limited("2"),
        httpx.Response(200, json={"ok": False, "error": "invalid_auth"}),
        limited("1"),
        limited("1"),
    )
    with pytest.raises(SlackTokenError) as token_error:
        client.post_message(TOKEN, "C1", text="a message body that must not be logged")
    with pytest.raises(SlackRateLimited) as limit_error:
        client.oauth_v2_access(client_id="id", client_secret=SECRET, code="c0de")
    everything = caplog.text + str(token_error.value) + repr(token_error.value)
    everything += str(limit_error.value) + repr(limit_error.value)
    assert TOKEN not in everything
    assert SECRET not in everything
    assert "must not be logged" not in everything
    assert "slack_rate_limited" in caplog.text
