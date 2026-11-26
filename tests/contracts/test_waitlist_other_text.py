"""Waitlist 'Other' text: required, trimmed and kept for role and location."""

from __future__ import annotations

import pytest

from contracts.v1 import WaitlistAnswer
from tests.support.waitlist_world import (
    URL,
    answer,
    client_for,
    join,
    memory_runtime,
    token_headers,
)

EMAIL = "Founder@Acme-Corp.test"


@pytest.fixture
def runtime():
    active = memory_runtime()
    yield active
    active.close()


@pytest.fixture
def client(runtime):
    yield client_for(runtime)


def _service(runtime):
    return runtime.waitlist_service


def test_other_text_is_required_trimmed_and_saved_for_role_and_location(client):
    token = join(client, EMAIL).json()["token"]
    for question in ("role", "company_location"):
        saved = answer(client, token, question, "other", other="  Chief of Staff  ")
        assert saved.status_code == 200
        assert saved.json()["answers"][question] == {
            "value": "other",
            "other": "Chief of Staff",
        }
        assert (
            answer(client, token, question, "other", other="y" * 60).status_code == 200
        )
        # Changing to a plain option drops the text; a failed change keeps the old.
        assert answer(client, token, question, "other", other=" ").status_code == 422
        kept = client.get(f"{URL}/submissions/current", headers=token_headers(token))
        assert kept.json()["answers"][question]["other"] == "y" * 60
        plain = answer(
            client, token, question, "finance" if question == "role" else "india"
        )
        assert plain.json()["answers"][question] == {
            "value": "finance" if question == "role" else "india"
        }
        again = answer(client, token, question, "other", other="Z")
        assert again.status_code == 200
        gone = client.delete(
            f"{URL}/submissions/current/answers/{question}",
            headers=token_headers(token),
        )
        assert gone.status_code == 200 and question not in gone.json()["answers"]


def test_answers_stored_before_other_text_was_required_stay_valid(runtime, client):
    """A version 1 submission keeps its own rules and its bare 'other'."""
    token = join(client, EMAIL).json()["token"]
    service = _service(runtime)
    (submission,) = service.submissions.list()
    legacy = {
        "role": WaitlistAnswer(value="other"),
        "company_location": WaitlistAnswer(value="other"),
    }
    service.submissions.put(
        submission.model_copy(update={"question_version": 1, "answers": legacy})
    )
    read = client.get(f"{URL}/submissions/current", headers=token_headers(token))
    assert read.status_code == 200 and read.json()["question_version"] == 1
    assert read.json()["answers"]["role"] == {"value": "other"}
    # Version 1 never required the text on these two.
    assert answer(client, token, "role", "other").status_code == 200
    # The current questions ask for the text, so a v1 session may send it too.
    kept = answer(client, token, "role", "other", other=" Chief ")
    assert kept.status_code == 200
    assert kept.json()["answers"]["role"] == {"value": "other", "other": "Chief"}
    assert answer(client, token, "role", "other", other="x" * 61).status_code == 422
    assert answer(client, token, "role", "finance", other="x").status_code == 422
    cleared = client.delete(
        f"{URL}/submissions/current/answers/company_location",
        headers=token_headers(token),
    )
    assert cleared.status_code == 200
    # Untouched stored answers are never re-validated by reads or other PUTs.
    assert answer(client, token, "company_size", "1_10").status_code == 200
