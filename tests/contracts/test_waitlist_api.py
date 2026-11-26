"""The public waitlist API: join, resume, answer, errors, abuse limits."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from modules.waitlist.questions import QuestionType
from shared.clock import utc_now
from tests.support.waitlist_world import (
    URL,
    answer,
    client_for,
    extended_bank,
    join,
    memory_runtime,
    stored_json,
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


def test_questions_are_served_with_cache_request_id_and_consent(client):
    response = client.get(f"{URL}/questions")
    assert response.status_code == 200
    assert response.headers["X-Request-Id"]
    assert "max-age" in response.headers["Cache-Control"]
    body = response.json()
    assert body["version"] == 2 and len(body["questions"]) == 6
    other_caps = {q["id"] for q in body["questions"] if q.get("allow_other")}
    assert other_caps == {"company_location", "role", "waiting_on", "tools"}
    assert all(
        q["other_max_length"] == 60 for q in body["questions"] if q.get("allow_other")
    )
    assert (
        body["consent"]["version"] == "v1"
        and "store your email" in body["consent"]["text"]
    )
    types = {q["id"]: q["type"] for q in body["questions"]}
    assert types["company_size"] == "size_band" and types["tools"] == "multiple_choice"
    assert all(q["optional"] is True and q["summary_label"] for q in body["questions"])
    old = client.get(f"{URL}/questions?version=1").json()
    assert old["version"] == 1  # published sets stay served for older answers
    assert not {q["id"] for q in old["questions"] if q.get("allow_other")} & {"role"}
    assert client.get(f"{URL}/questions?version=9").status_code == 404


def test_join_creates_one_entry_and_stores_only_a_token_digest(runtime, client):
    response = join(client, f"  {EMAIL} ", source={"page": "/", "utm_source": "x"})
    assert response.status_code == 201
    assert response.headers["X-Request-Id"] and "set-cookie" not in response.headers
    body = response.json()
    assert set(body) == {"token", "question_version", "answers"}
    assert body["question_version"] == 2 and body["answers"] == {}
    (entry,) = _service(runtime).entries.list()
    (submission,) = _service(runtime).submissions.list()
    assert entry.email == "founder@acme-corp.test"
    assert (
        entry.email_domain == "acme-corp.test" and entry.domain_type.value == "company"
    )
    assert entry.status.value == "new" and entry.submission_count == 1
    assert submission.entry_id == entry.id and submission.consent_version == 1
    assert submission.token_hash == hashlib.sha256(body["token"].encode()).hexdigest()
    assert submission.source.utm_source == "x"
    assert submission.token_expires_at - submission.created_at == timedelta(days=30)
    assert body["token"] not in stored_json(runtime)


def test_public_mailbox_domains_are_typed_public(runtime, client):
    join(client, "someone@gmail.com")
    join(client, "other@example.test")  # WEAVES_PUBLIC_EMAIL_DOMAINS in conftest
    types = {e.email: e.domain_type.value for e in _service(runtime).entries.list()}
    assert types == {"someone@gmail.com": "public", "other@example.test": "public"}


def test_known_email_gets_a_new_submission_and_an_identical_response(runtime):
    first = client_for(runtime, "198.51.100.1")
    second = client_for(runtime, "198.51.100.2")
    one = join(first, EMAIL).json()
    assert answer(first, one["token"], "role", "finance").status_code == 200
    two_response = join(second, EMAIL.upper())
    two = two_response.json()
    assert two_response.status_code == 201
    assert set(two) == set(one) and two["answers"] == {}
    assert two["token"] != one["token"]
    assert "finance" not in two_response.text
    assert len(_service(runtime).entries.list()) == 1
    assert len(_service(runtime).submissions.list()) == 2
    (entry,) = _service(runtime).entries.list()
    assert entry.submission_count == 2
    mine = second.get(f"{URL}/submissions/current", headers=token_headers(two["token"]))
    assert mine.json()["answers"] == {}
    old = first.get(f"{URL}/submissions/current", headers=token_headers(one["token"]))
    assert old.json()["answers"] == {"role": {"value": "finance"}}


def test_idempotent_retry_does_not_add_a_submission(runtime, client):
    one = join(client, EMAIL, key="retry-1").json()
    assert answer(client, one["token"], "company_size", "11_50").status_code == 200
    again = join(client, EMAIL, key="retry-1")
    assert again.status_code == 201 and again.json()["token"]
    assert len(_service(runtime).submissions.list()) == 1
    assert _service(runtime).entries.list()[0].submission_count == 1
    other = join(client, EMAIL, key="retry-2")
    assert other.status_code == 201 and other.json()["answers"] == {}
    assert len(_service(runtime).submissions.list()) == 2


def test_honeypot_returns_the_same_shape_and_stores_nothing(runtime, client):
    response = join(client, "bot@acme-corp.test", company_website="http://spam")
    assert response.status_code == 201
    assert set(response.json()) == {"token", "question_version", "answers"}
    assert not _service(runtime).entries.list()
    assert not _service(runtime).submissions.list()
    unusable = client.get(
        f"{URL}/submissions/current", headers=token_headers(response.json()["token"])
    )
    assert unusable.status_code == 401
    assert join(client, "not an email", company_website="x").status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {"email": "nobody"},
        {"email": "a@b"},
        {"email": "a@@b.test"},
        {"email": "a b@c.test"},
        {"email": ".a@c.test"},
        {"email": "a@c..test"},
        {"email": "a@-c.test"},
        {"email": "a@c.t"},
        {"email": "a@c.123"},
        {"email": "x" * 250 + "@c.test"},
        {"email": "ünï@c.test"},
        {"consent": False},
        {"consent_version": "v99"},
        {"unexpected": 1},
        {"source": {"unknown": "x"}},
        {"source": {"page": "p" * 600}},
    ],
)
def test_join_validation_refuses_with_422_and_stores_nothing(runtime, client, body):
    payload = {"email": "ok@acme-corp.test", "consent": True, "consent_version": 1}
    response = client.post(f"{URL}/submissions", json={**payload, **body})
    assert response.status_code == 422
    assert response.headers["X-Request-Id"] and response.json()["code"]
    assert not _service(runtime).entries.list()


def test_join_without_consent_or_json_is_refused(client):
    assert (
        client.post(f"{URL}/submissions", json={"email": "a@c.test"}).status_code == 422
    )
    assert client.post(f"{URL}/submissions", content=b"not json").status_code == 422
    assert client.post(f"{URL}/submissions", content=b"[]").status_code == 422


def test_oversized_bodies_are_refused_with_413(runtime, client):
    big = "x" * 9000
    too_big = join(client, "ok@acme-corp.test", source={"page": big})
    assert too_big.status_code == 413 and too_big.headers["X-Request-Id"]
    assert too_big.json()["code"] == "payload_too_large"
    token = join(client, "ok@acme-corp.test").json()["token"]
    put = answer(client, token, "role", "finance", other=big)
    assert put.status_code == 413
    assert len(_service(runtime).entries.list()) == 1


def test_token_failures_are_one_generic_401(runtime, client):
    token = join(client, EMAIL).json()["token"]
    (submission,) = _service(runtime).submissions.list()
    _service(runtime).submissions.put(
        submission.model_copy(update={"token_expires_at": utc_now() - timedelta(1)})
    )
    bodies = []
    for headers in (
        {},
        token_headers("short"),
        token_headers("wlt_" + "A" * 43),
        token_headers(token),
    ):
        response = client.get(f"{URL}/submissions/current", headers=headers)
        assert response.status_code == 401 and response.headers["X-Request-Id"]
        bodies.append(response.json())
    assert all(body == bodies[0] for body in bodies)
    assert (
        client.put(
            f"{URL}/submissions/current/answers/role",
            json={"value": "finance"},
            headers=token_headers(token),
        ).status_code
        == 401
    )


def test_answers_can_be_saved_changed_cleared_and_resumed(client):
    token = join(client, EMAIL).json()["token"]
    assert answer(client, token, "company_location", "india").json()["answers"] == {
        "company_location": {"value": "india"}
    }
    assert answer(client, token, "company_location", "europe").status_code == 200
    saved = answer(client, token, "tools", ["slack", "other"], other=" Linear ")
    assert saved.json()["answers"]["tools"] == {
        "value": ["slack", "other"],
        "other": "Linear",
    }
    assert (
        answer(client, token, "waiting_on", "other", other="Legal").status_code == 200
    )
    cleared = client.delete(
        f"{URL}/submissions/current/answers/company_location",
        headers=token_headers(token),
    )
    assert (
        cleared.status_code == 200
        and "company_location" not in cleared.json()["answers"]
    )
    again = client.delete(
        f"{URL}/submissions/current/answers/company_location",
        headers=token_headers(token),
    )
    assert again.status_code == 200
    resumed = client.get(f"{URL}/submissions/current", headers=token_headers(token))
    assert set(resumed.json()["answers"]) == {"tools", "waiting_on"}


@pytest.mark.parametrize(
    ("question", "value", "extra"),
    [
        ("role", "not_an_option", {}),
        ("role", ["finance"], {}),
        ("role", 5, {}),
        ("role", "finance", {"other": "text"}),
        ("role", "other", {}),
        ("role", "other", {"other": ""}),
        ("role", "other", {"other": "   "}),
        ("role", "other", {"other": "x" * 61}),
        ("role", "other", {"other": "bad\x00text"}),
        ("role", "other", {"other": 5}),
        ("company_location", "other", {}),
        ("company_location", "other", {"other": "  "}),
        ("company_location", "india", {"other": "Peru"}),
        ("waiting_on", "other", {}),
        ("tools", ["slack", "other"], {}),
        ("tools", ["slack", "other"], {"other": " "}),
        ("tools", ["slack"], {"other": "Linear"}),
        ("company_size", "1_10", {"other": "text"}),
        ("waiting_on", "numbers_reports", {"other": "text"}),
        ("waiting_on", "other", {"other": "x" * 61}),
        ("tools", "slack", {}),
        ("tools", [], {}),
        ("tools", ["slack", "slack"], {}),
        ("tools", ["slack", "nope"], {}),
        ("tools", ["other"], {"other": "y" * 61}),
        ("not_a_question", "x", {}),
    ],
)
def test_answer_validation_refuses_with_422(client, question, value, extra):
    token = join(client, EMAIL).json()["token"]
    response = answer(client, token, question, value, **extra)
    assert response.status_code == 422
    assert response.headers["X-Request-Id"] and response.json()["code"]
    resumed = client.get(f"{URL}/submissions/current", headers=token_headers(token))
    assert resumed.json()["answers"] == {}


def test_short_text_questions_enforce_type_and_length(runtime, tmp_path):
    bank = extended_bank(tmp_path / "content")
    text_question = {
        "id": "wish",
        "type": QuestionType.SHORT_TEXT,
        "prompt": "Anything else?",
        "summary_label": "Wish",
        "optional": True,
        "max_length": 280,
    }
    base = bank.sets[3]
    patched = base.model_copy(
        update={
            "questions": (*base.questions[:5], type(base.questions[0])(**text_question))
        }
    )
    _service(runtime).bank = type(bank)(sets={3: patched}, consents=bank.consents)
    client = client_for(runtime)
    token = join(client, EMAIL).json()["token"]
    assert answer(client, token, "wish", "z" * 280).status_code == 200
    assert answer(client, token, "wish", "z" * 281).status_code == 422
    assert answer(client, token, "wish", ["a"]).status_code == 422
    assert answer(client, token, "wish", "  ").status_code == 422
    assert answer(client, token, "wish", "ok", other="x").status_code == 422


def test_a_changed_question_set_returns_409(runtime, tmp_path):
    client = client_for(runtime)
    token = join(client, EMAIL).json()["token"]
    _service(runtime).bank = extended_bank(tmp_path / "content")
    stale = answer(client, token, "role", "finance", question_version=3)
    assert stale.status_code == 409 and stale.headers["X-Request-Id"]
    assert stale.json()["code"] == "question_version_changed"
    assert (
        stale.json()["current_version"] == 3 and stale.json()["question_version"] == 2
    )
    cleared = client.delete(
        f"{URL}/submissions/current/answers/role?question_version=3",
        headers=token_headers(token),
    )
    assert cleared.status_code == 409
    assert join(client, "late@acme-corp.test", question_version=2).status_code == 409
    fresh = join(client, "late@acme-corp.test", question_version=3)
    assert fresh.status_code == 201 and fresh.json()["question_version"] == 3
    assert answer(client, token, "role", "finance").status_code == 200  # own version


def test_join_is_limited_per_source_with_retry_after(runtime):
    client = client_for(runtime, "192.0.2.10")
    for index in range(10):
        assert join(client, f"user{index}@acme-corp.test").status_code == 201
    blocked = join(client, "user11@acme-corp.test")
    assert blocked.status_code == 429 and blocked.headers["X-Request-Id"]
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.json()["code"] == "rate_limited"
    other_source = client_for(runtime, "192.0.2.11")
    assert join(other_source, "user12@acme-corp.test").status_code == 201


def test_join_is_limited_per_email(runtime):
    for index in range(5):
        client = client_for(runtime, f"192.0.2.{20 + index}")
        assert join(client, EMAIL).status_code == 201
    blocked = join(client_for(runtime, "192.0.2.40"), EMAIL)
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) >= 1


def test_answers_are_limited_per_token(client):
    token = join(client, EMAIL).json()["token"]
    codes = [answer(client, token, "role", "finance").status_code for _ in range(61)]
    assert codes[:60] == [200] * 60
    assert codes[60] == 429
    last = answer(client, token, "role", "finance")
    assert last.headers["Retry-After"]


def test_questions_have_a_light_per_source_limit(client):
    codes = [client.get(f"{URL}/questions").status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_waitlist_never_creates_accounts_sessions_or_cookies(runtime):
    client = client_for(runtime)
    client.cookies.set("__Host-weaves_account_session", "bogus-session-cookie")
    response = join(client, EMAIL)
    assert response.status_code == 201  # no CSRF or origin check on this prefix
    token = response.json()["token"]
    put = client.put(
        f"{URL}/submissions/current/answers/role",
        json={"value": "finance"},
        headers={**token_headers(token), "Origin": "https://weave-studio.in"},
    )
    assert put.status_code == 200
    assert "set-cookie" not in put.headers and "set-cookie" not in response.headers
    for name in (
        "accounts",
        "account_identities",
        "account_sessions",
        "memberships",
        "organizations",
        "users",
        "principals",
        "api_credentials",
    ):
        assert not getattr(runtime, name).list(), name


def test_other_routes_do_not_accept_a_waitlist_token(client):
    token = join(client, EMAIL).json()["token"]
    for path in ("/api/v0/agents", "/api/v0/account/me"):
        response = client.get(path, headers=token_headers(token))
        assert response.status_code in (401, 403)


def test_concurrent_joins_of_one_email_make_one_entry(runtime):
    clients = [client_for(runtime, f"198.18.0.{index}") for index in range(4)]

    def attempt(index: int) -> int:
        return join(clients[index], EMAIL, key=f"race-{index}").status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(attempt, range(4)))
    assert statuses == [201] * 4
    (entry,) = _service(runtime).entries.list()
    assert entry.submission_count == 4
    assert len(_service(runtime).submissions.list()) == 4


def test_retention_purges_stale_submissions_and_empty_entries(runtime, client):
    stale = join(client, "old@acme-corp.test").json()["token"]
    keep = join(client, "mixed@acme-corp.test").json()["token"]
    join(client_for(runtime, "198.51.100.7"), "mixed@acme-corp.test")
    service = _service(runtime)
    old_time = utc_now() - timedelta(days=400)
    for item in service.submissions.list():
        owner = service.entries.get(item.entry_id).email
        if (
            owner == "old@acme-corp.test"
            or item.token_hash == hashlib.sha256(keep.encode()).hexdigest()
        ):
            service.submissions.put(item.model_copy(update={"updated_at": old_time}))
    for entry in service.entries.list():
        if entry.email == "old@acme-corp.test":
            service.entries.put(entry.model_copy(update={"last_joined_at": old_time}))
    assert service.purge_stale(days=365) == (2, 1)
    assert [e.email for e in service.entries.list()] == ["mixed@acme-corp.test"]
    assert (
        client.get(
            f"{URL}/submissions/current", headers=token_headers(stale)
        ).status_code
        == 401
    )
    assert service.purge_stale(days=365) == (0, 0)
