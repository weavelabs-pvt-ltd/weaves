"""Provider success alone must not turn missing provenance into a passing test."""

import copy
import json

import pytest

from tenancy_harness.live_records import verify_records

LIVE = {"marker": "LIVE_CHECK", "provider_ids": ["anthropic-fixture"]}


def records():
    return [
        {
            "status": "succeeded",
            "model": "real-fixture-model",
            "provider_id": "anthropic-fixture",
            "input_tokens": 15,
            "output_tokens": 12,
            "skill_pins": ["pinned-skill"],
            "tool_statuses": ["succeeded"],
            "summary": {"response": "LIVE_CHECK", "citations": [{"source": "fixture"}]},
        }
        for _ in range(3)
    ]


def test_public_live_evidence_contains_usage_and_provenance_without_responses():
    evidence = verify_records(records(), LIVE, "pinned-skill")
    assert len(evidence["verified_model_calls"]) == 3
    assert "response" not in json.dumps(evidence)
    assert "LIVE_CHECK" not in json.dumps(evidence)


@pytest.mark.parametrize(
    "changes",
    [
        {"summary": {"response": "An unrelated reply", "citations": [{}]}},
        {"summary": {"response": "LIVE_CHECK", "citations": []}},
        {"tool_statuses": ["failed"]},
        {"skill_pins": []},
        {"provider_id": "unexpected"},
        {"input_tokens": 0},
        {"status": "failed"},
    ],
)
def test_live_evidence_rejects_wrong_response_usage_or_provenance(changes):
    data = copy.deepcopy(records())
    data[1].update(changes)
    with pytest.raises(AssertionError):
        verify_records(data, LIVE, "pinned-skill")


def test_missing_or_extra_paid_calls_do_not_pass_verification():
    for data in (records()[:2], records() + records()):
        with pytest.raises(AssertionError, match="three planned calls"):
            verify_records(data, LIVE, "pinned-skill")
