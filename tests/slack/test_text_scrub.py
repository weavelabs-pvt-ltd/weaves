"""The egress scrubber: secrets and personal data never leave, and it fails closed."""

from __future__ import annotations

import pytest

from modules.slack.reply.poster import PostError, finalize
from modules.slack.text.scrub import Scrubbed, ScrubberUnavailable, scrub

SECRETS = {
    "slack_token": ("xoxb-1234567890-abcdefghijkl", "1234567890-abcdefghijkl"),
    "slack_app_token": ("xapp-1-A0123456789-abcdefghijkl", "A0123456789-abcdef"),
    "slack_webhook": (
        "https://hooks.slack.com/services/T0AAAAAAA/B0BBBBBBB/abcdefghijklmnop",
        "B0BBBBBBB/abcdef",
    ),
    "github_token": ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnop"),
    "anthropic_key": ("sk-ant-api03-abcdefghijklmnop", "api03-abcdefghijklmnop"),
    "api_key": ("sk-proj-abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
    "aws_key": ("AKIAABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOP"),
    "google_key": ("AIzaSyA-abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnop"),
    "jwt": (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdEFGHijkl",
        "abcdEFGH",
    ),
    "bearer": ("Bearer abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnop"),
    "credential": ("password = hunter2hunter2", "hunter2hunter2"),
    "url_credentials": ("postgres://admin:s3cretpass@db.internal/app", "s3cretpass"),
    "private_key": (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----",
        "MIIEabc",
    ),
}


@pytest.mark.parametrize(("category", "case"), SECRETS.items())
def test_each_secret_shape_is_removed(category: str, case: tuple[str, str]) -> None:
    text, fragment = case
    result = scrub(f"before {text} after")
    assert category in result.categories
    assert result.redactions >= 1
    assert fragment not in result.text
    assert result.text.startswith("before") and result.text.endswith("after")


def test_personal_data_is_removed_and_cards_need_a_valid_checksum() -> None:
    result = scrub(
        "mail jane.doe@example.com ssn 123-45-6789 card 4111 1111 1111 1111 "
        "order 1234 5678 9012 3456"
    )
    assert "jane.doe" not in result.text and "123-45-6789" not in result.text
    assert "4111" not in result.text
    assert "1234 5678 9012 3456" in result.text  # fails the Luhn check: kept
    assert set(result.categories) >= {"email", "ssn", "card"}


def test_ordinary_text_is_unchanged() -> None:
    text = "WEAVES-12 moved to Done on 2026-11-27 (build 4455, 3 sources)."
    assert scrub(text) == Scrubbed(text, 0, ())


def test_the_result_never_carries_the_matched_values() -> None:
    result = scrub("key AKIAABCDEFGHIJKLMNOP")
    assert "AKIA" not in repr(result)


def test_email_redaction_can_be_switched_off() -> None:
    assert scrub("a@b.com", redact_email=False).text == "a@b.com"


def test_an_internal_failure_raises_unavailable() -> None:
    with pytest.raises(ScrubberUnavailable):
        scrub("x" * 200_000)


def test_finalize_fails_closed_when_the_scrubber_errors() -> None:
    def broken(_: str) -> Scrubbed:
        raise RuntimeError("regex engine exploded with secret-in-message")

    with pytest.raises(PostError) as caught:
        finalize("a perfectly fine message", broken)
    assert caught.value.kind == "scrubber"
    assert "secret-in-message" not in str(caught.value)


def test_finalize_refuses_text_that_still_has_raw_markup() -> None:
    with pytest.raises(PostError) as caught:
        finalize("<!channel> look", lambda text: Scrubbed(text))
    assert caught.value.kind == "scrubber"


def test_finalize_scrubs_and_counts() -> None:
    final = finalize("token xoxb-1234567890-abcdefghijkl")
    assert "xoxb" not in final.text and final.redactions == 1
