"""Slack request signature verification (made-up secret, no real Slack)."""

import hashlib
import hmac

import pytest

from clients.slack import signature as signature_module
from clients.slack.errors import SlackSignatureError
from clients.slack.signature import compute_signature, verify_slack_request

SECRET = b"made-up-signing-secret"
NOW = 1_700_000_000
BODY = b'{"type":"event_callback","event_id":"Ev0001"}'


def headers_for(body: bytes = BODY, ts: int = NOW, secret: bytes = SECRET) -> dict:
    stamp = str(ts)
    return {
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": compute_signature(secret, stamp, body),
    }


def reason_of(**kwargs) -> str:
    with pytest.raises(SlackSignatureError) as caught:
        verify_slack_request(**kwargs)
    return caught.value.reason


def test_signature_matches_slacks_documented_construction() -> None:
    base = b"v0:" + str(NOW).encode() + b":" + BODY
    expected = "v0=" + hmac.new(SECRET, base, hashlib.sha256).hexdigest()
    assert compute_signature(SECRET, str(NOW), BODY) == expected


def test_valid_request_passes() -> None:
    verify_slack_request(SECRET, headers_for(), BODY, NOW)


def test_header_names_are_case_insensitive() -> None:
    lowered = {k.lower(): v for k, v in headers_for().items()}
    verify_slack_request(SECRET, lowered, BODY, NOW)
    upper = {k.upper(): v for k, v in headers_for().items()}
    verify_slack_request(SECRET, upper, BODY, NOW)


def test_tampered_body_fails() -> None:
    assert (
        reason_of(secret=SECRET, headers=headers_for(), raw_body=BODY + b" ", now=NOW)
        == "bad_signature"
    )


def test_wrong_secret_fails() -> None:
    other = headers_for(secret=b"another-made-up-secret")
    assert (
        reason_of(secret=SECRET, headers=other, raw_body=BODY, now=NOW)
        == "bad_signature"
    )


def test_exactly_five_minutes_old_is_still_accepted() -> None:
    verify_slack_request(SECRET, headers_for(ts=NOW - 300), BODY, NOW)


def test_301_seconds_old_is_stale() -> None:
    assert (
        reason_of(
            secret=SECRET, headers=headers_for(ts=NOW - 301), raw_body=BODY, now=NOW
        )
        == "stale"
    )


def test_future_dated_beyond_the_window_is_stale() -> None:
    assert (
        reason_of(
            secret=SECRET, headers=headers_for(ts=NOW + 301), raw_body=BODY, now=NOW
        )
        == "stale"
    )


def test_replayed_body_is_accepted_inside_the_window_and_refused_after_it() -> None:
    # A captured request replays fine for five minutes (event_id dedupe, not the
    # signature, handles that) and never after: the timestamp is signed.
    captured = headers_for()
    verify_slack_request(SECRET, captured, BODY, NOW + 120)
    assert (
        reason_of(secret=SECRET, headers=captured, raw_body=BODY, now=NOW + 301)
        == "stale"
    )


def test_replay_with_a_refreshed_timestamp_but_old_signature_fails() -> None:
    captured = headers_for()
    forged = {**captured, "X-Slack-Request-Timestamp": str(NOW + 600)}
    assert (
        reason_of(secret=SECRET, headers=forged, raw_body=BODY, now=NOW + 600)
        == "bad_signature"
    )


@pytest.mark.parametrize("missing", ["X-Slack-Request-Timestamp", "X-Slack-Signature"])
def test_missing_header(missing: str) -> None:
    headers = headers_for()
    del headers[missing]
    assert (
        reason_of(secret=SECRET, headers=headers, raw_body=BODY, now=NOW)
        == "missing_header"
    )


def test_no_headers_at_all() -> None:
    assert (
        reason_of(secret=SECRET, headers={}, raw_body=BODY, now=NOW) == "missing_header"
    )


@pytest.mark.parametrize(
    ("timestamp", "signature"),
    [
        ("not-a-number", "v0=" + "a" * 64),
        ("-5", "v0=" + "a" * 64),
        ("1700000000.5", "v0=" + "a" * 64),
        (str(NOW), "v1=" + "a" * 64),
        (str(NOW), "v0=" + "a" * 63),
        (str(NOW), "v0=" + "A" * 64),
        (str(NOW), "v0=" + "g" * 64),
        (str(NOW), ""),
    ],
)
def test_malformed_headers(timestamp: str, signature: str) -> None:
    headers = {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }
    expected = "missing_header" if not signature else "malformed"
    assert reason_of(secret=SECRET, headers=headers, raw_body=BODY, now=NOW) == expected


def test_empty_secret_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="signing secret"):
        verify_slack_request(b"", headers_for(), BODY, NOW)


def test_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(signature_module.hmac, "compare_digest", spy)
    verify_slack_request(SECRET, headers_for(), BODY, NOW)
    assert len(calls) == 1
    assert calls[0][0] == calls[0][1]


def test_comparison_is_used_for_a_wrong_signature_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(signature_module.hmac, "compare_digest", spy)
    wrong = headers_for(secret=b"other")
    assert (
        reason_of(secret=SECRET, headers=wrong, raw_body=BODY, now=NOW)
        == "bad_signature"
    )
    assert calls == [1]


def test_error_message_is_the_reason_only() -> None:
    with pytest.raises(SlackSignatureError) as caught:
        verify_slack_request(SECRET, headers_for(secret=b"other"), BODY, NOW)
    assert str(caught.value) == "bad_signature"
    assert SECRET.decode() not in str(caught.value)
