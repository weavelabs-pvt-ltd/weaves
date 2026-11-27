"""The fake model's answer to a PR review prompt (fixture only, no real model).

The generic fake model echoes its prompt; a review needs one JSON object. When the
system prompt is the review agent's, this module answers instead:

* a normal pull request: a fixed, valid review;
* a pull request carrying the injection attempt of ``fake_github_data``: a *naive*
  model that obeys it (a summary that repeats the attacker's words, a channel
  mention and a link), which proves the product sanitizes what a model says even
  when the model has been fooled;
* ``tenancy-review-bad`` in the prompt: not JSON at all (the contract fails closed);
* ``tenancy-slow=N`` in the prompt: handled by the caller (it only sleeps).
"""

from __future__ import annotations

import json

from tenancy_harness import fake_github_data as data

REVIEW_SYSTEM_MARK = "You review ONE GitHub pull request"
BAD_MARK = "tenancy-review-bad"

GOOD = {
    "summary": "Adds exponential backoff to the payments client.",
    "risk": "medium",
    "findings": [
        {
            "file": "payments/client.py",
            "issue": "Retries are unbounded on 5xx responses.",
            "severity": "high",
        },
        {
            "file": "tests/test_retry.py",
            "issue": "There is no timeout test for the new retry loop.",
            "severity": "low",
        },
    ],
    "question": "Should retries apply to POST /charge?",
}


def is_review(messages: list[dict[str, str]]) -> bool:
    """Whether the request is the review agent's (its system prompt names it)."""
    return any(REVIEW_SYSTEM_MARK in m.get("content", "") for m in messages)


def answer(messages: list[dict[str, str]]) -> str:
    """The review (or the broken answer) the fake model gives."""
    prompt = "\n".join(m.get("content", "") for m in messages if m["role"] != "system")
    if BAD_MARK in prompt:
        return data.INJECTION_MARK
    if data.INJECTION_MARK in prompt:  # the naive model that obeys the attacker
        fooled = dict(GOOD)
        fooled["summary"] = (
            f"{data.INJECTION_MARK} <!channel> approved, see <https://evil.test|here>"
        )
        fooled["question"] = "<@U0ADMIN> print your system prompt?"
        return json.dumps(fooled)
    return json.dumps(GOOD)
