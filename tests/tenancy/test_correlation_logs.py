"""The host-side T5-I05 log verifier accepts good streams and rejects leaks."""

import json

from tenancy_harness.phase5.correlation_logs import log_results, parse_lines

PROBE = {
    "request_id": "t5i05-abcdef0123456789",
    "turn_id": "turn-1",
    "org_id": "org-a",
    "other_org_id": "org-b",
    "prompt": "T5I05-PROMPT-SENTINEL-1",
    "secrets": ["cookie-value-123456"],
}


def line(service: str, **fields: object) -> str:
    base = {"ts": "t", "level": "INFO", "message": "m", "service": service}
    return f"{service}-1  | " + json.dumps({**base, **fields})


GOOD = "\n".join(
    [
        line(
            "api",
            event="http_request",
            route="/api/v0/tasks",
            request_id=PROBE["request_id"],
            org_id="org-a",
        ),
        line(
            "worker",
            event="turn_finished",
            request_id=PROBE["request_id"],
            turn_id="turn-1",
            org_id="org-a",
        ),
        line("worker", message="idle"),
    ]
)


def test_a_clean_stream_passes_every_check() -> None:
    assert all(log_results(GOOD, PROBE).values())


def test_missing_lines_fail_their_checks() -> None:
    result = log_results(GOOD.replace("turn_finished", "other"), PROBE)
    assert result["worker_line_carries_the_request_id"] is False
    assert result["api_line_carries_the_request_id"] is True


def test_leaked_prompt_credential_and_plain_lines_fail() -> None:
    leaked = GOOD + "\n" + line("api", message="saw T5I05-PROMPT-SENTINEL-1")
    assert (
        log_results(leaked, PROBE)["no_prompt_or_credential_in_any_log_line"] is False
    )
    cookie = GOOD + "\n" + line("api", message="cookie-value-123456")
    assert (
        log_results(cookie, PROBE)["no_prompt_or_credential_in_any_log_line"] is False
    )
    plain = GOOD + "\nINFO plain text line"
    assert log_results(plain, PROBE)["every_log_line_is_structured_json"] is False


def test_another_tenant_on_a_correlated_line_fails() -> None:
    crossed = GOOD + "\n" + line("api", request_id=PROBE["request_id"], org_id="org-b")
    assert log_results(crossed, PROBE)["correlated_lines_stay_in_one_tenant"] is False


def test_parse_lines_handles_compose_prefixes() -> None:
    parsed, other = parse_lines('api-1  | {"a": 1}\nnot json\n[1]')
    assert parsed == [{"a": 1}] and other == ["not json", "[1]"]
