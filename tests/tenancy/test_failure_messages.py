"""A failing browser control must publish its real message, scrubbed and bounded."""

import json
from pathlib import Path

from tenancy_harness.report import fixture_secrets, scrub, summarize

PAIR = json.loads(
    (Path(__file__).resolve().parents[2] / "tenancy_harness/pair.json").read_text()
)
ADMIN_KEY = "wk_live_0123456789abcdefghijklmnop"
MESSAGE = (
    "\x1b[31mError: expect(locator).toBeVisible() failed\x1b[39m\n"
    "Locator: weaves-dialog >> [data-schedule-error]\n"
    "Authorization: Bearer abcdef0123456789 cookie=session-value-123\n"
    "header x-api-key=" + ADMIN_KEY + " token: eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl\n"
)


def failing_raw(target="T4-I04"):
    cases = PAIR["required_controls"] + list(PAIR["known_defects"])
    specs = []
    for case in cases:
        if case == target:
            result = {
                "status": "failed",
                "errors": [
                    {"message": MESSAGE, "location": {"file": "/x/y.cjs", "line": 73}}
                ],
            }
            test = {
                "results": [result],
                "annotations": [
                    {
                        "type": "browser-summary",
                        "description": "http 500 POST /api/v0/workflow-schedules",
                    }
                ],
            }
        else:
            test = {"results": [{"status": "passed"}]}
        specs.append({"title": case + " fixture", "tests": [test]})
    return {"suites": [{"specs": specs}]}


def test_failing_browser_control_publishes_message_and_redacts_secrets():
    fixture = {
        "diagnostics": dict.fromkeys(PAIR["known_defects"], True),
        "tenants": {"a": {"admin": {"headers": {"X-Key": ADMIN_KEY}}}},
    }
    report = summarize(failing_raw(), PAIR, {}, "digest", fixture)
    published = report["failure_messages"]["T4-I04"]
    text = json.dumps(report)
    assert not report["foundation_passed"]
    assert "toBeVisible() failed" in published["errors"][0]
    assert "data-schedule-error" in published["errors"][0]
    assert published["browser"] == ["http 500 POST /api/v0/workflow-schedules"]
    for secret in (ADMIN_KEY, "abcdef0123456789", "session-value-123", "c2lnbmF0dXJl"):
        assert secret not in text
    assert "\x1b" not in text
    assert report["failure_locations"]["T4-I04"][0]["line"] == 73
    assert set(report["failure_messages"]) == {"T4-I04"}


def test_scrub_truncates_and_redacts_exact_fixture_secrets():
    assert scrub("word " * 2000).endswith("[truncated]")
    assert len(scrub("x " * 5000)) < 800
    assert "hunter2hunter2" not in scrub(
        "password is hunter2hunter2", {"hunter2hunter2"}
    )
    found = fixture_secrets(
        {"admin": {"headers": {"k": "abcdefgh12"}}, "id": "plain-id-1"}
    )
    assert found == {"abcdefgh12"}
