"""Real-stack control TW-01: the public waitlist API and its operator script.

API-only (no browser). HTTP calls go through the real API exactly as the
website's relative URL would reach it, with no cookie and no sign-in. Operator
actions run the real ``waitlist.py`` (mounted from the backend checkout) against
the disposable database with the owner connection, exactly as it is streamed
into the container on the VM. Nothing here uses a testing endpoint.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path

import psycopg

from tenancy_harness.client import request, source_for
from tenancy_harness.operator import operator_bearer
from tenancy_harness.seed import require_disposable_database

SCRIPT = Path("/suite/operator/waitlist.py")
EMAIL = "Wait.Person@Waitlist-Company.test"
LOWER = EMAIL.lower()
OPERATOR = ["--operator", "harness@tenancy.test"]
BASE = "/waitlist"
CONSENT = {"consent": True, "consent_version": "v1"}


def operator(*argv):
    """Run the real operator script; return (status, stdout, JSON log lines)."""
    spec = importlib.util.spec_from_file_location("waitlist_operator", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = module.run(list(argv))
    logs = [json.loads(line) for line in err.getvalue().splitlines() if line.strip()]
    return status, out.getvalue(), logs


def join(email, visitor, **extra):
    """POST a join from one synthetic network source, with no cookie."""
    body = {"email": email, **CONSENT, **extra}
    return request("POST", BASE + "/submissions", source_for(visitor), body)


def with_token(method, path, token, body=None, source="wl-token-holder"):
    """Call a token endpoint with the private header only."""
    headers = {**source_for(source), "X-Waitlist-Token": token}
    return request(method, BASE + "/submissions/current" + path, headers, body)


def rows(query, *params):
    """Read the waitlist tables on the disposable owner connection."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(query, params).fetchall()


def answers_of(token):
    """The stored answers of the submission this token belongs to."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    found = rows(
        "SELECT payload->'answers' FROM product_records "
        "WHERE collection = 'waitlist_submissions' AND payload->>'token_hash' = %s",
        digest,
    )
    return found[0][0] if found else None


def stored_values(token):
    """The stored answer values (not the bookkeeping fields) of one submission."""
    return {
        question: answer["value"]
        for question, answer in (answers_of(token) or {}).items()
    }


def flow_checks():
    """Join, answer, change, clear, resume and a second join of a known email."""
    asked = request("GET", BASE + "/questions", source_for("wl-reader"))
    first = join(EMAIL, "wl-one", source={"page": "/", "utm_source": "harness"})
    token = first.json().get("token", "")
    saved = with_token("PUT", "/answers/company_location", token, {"value": "india"})
    with_token(
        "PUT", "/answers/tools", token, {"value": ["slack", "other"], "other": "Linear"}
    )
    changed = with_token("PUT", "/answers/company_location", token, {"value": "europe"})
    cleared = with_token("DELETE", "/answers/tools", token)
    resumed = request(
        "GET",
        BASE + "/submissions/current",
        {**source_for("wl-resume"), "X-Waitlist-Token": token},
    )
    second = join(EMAIL.upper(), "wl-two")
    other = second.json().get("token", "")
    return {
        "questions_served": asked.status_code == 200
        and asked.json()["version"] == 2
        and len(asked.json()["questions"]) == 6
        and bool(asked.headers.get("X-Request-Id")),
        "join_creates_submission": first.status_code == 201
        and set(first.json()) == {"token", "question_version", "answers"}
        and first.json()["answers"] == {},
        "answer_saved": saved.status_code == 200
        and saved.json()["answers"] == {"company_location": {"value": "india"}},
        "answer_changed": changed.status_code == 200
        and changed.json()["answers"]["company_location"] == {"value": "europe"},
        "answer_cleared": cleared.status_code == 200
        and "tools" not in cleared.json()["answers"],
        "resume_with_token_only": resumed.status_code == 200
        and resumed.json()["answers"] == {"company_location": {"value": "europe"}},
        "known_email_gets_a_new_submission": second.status_code == 201
        and bool(other)
        and other != token
        and set(second.json()) == set(first.json()),
        "known_email_leaks_no_answers": second.json().get("answers") == {}
        and "europe" not in second.text
        and with_token("GET", "", other).json()["answers"] == {},
        "first_token_still_has_its_answers": stored_values(token)
        == {"company_location": "europe"},
        "no_cookie_set": not any(
            r.headers.get("set-cookie")
            for r in (asked, first, saved, changed, cleared, resumed, second)
        ),
        "token": token,
    }


def refusal_checks():
    """Bad tokens, bad input and rate limits, each with a request reference."""
    bad = with_token("GET", "", "wlt_" + "A" * 43)
    missing = request("GET", BASE + "/submissions/current", source_for("wl-none"))
    invalid = join("not-an-email", "wl-invalid")
    statuses = [
        join(f"limit{index}@waitlist-company.test", "wl-limit").status_code
        for index in range(11)
    ]
    blocked = join("limit11@waitlist-company.test", "wl-limit")
    return {
        "invalid_token_is_401": bad.status_code == 401
        and missing.status_code == 401
        and bad.json() == missing.json()
        and bool(bad.headers.get("X-Request-Id")),
        "invalid_email_is_422": invalid.status_code == 422
        and bool(invalid.headers.get("X-Request-Id")),
        "rate_limit_returns_429_with_retry_after": statuses[:10] == [201] * 10
        and statuses[10] == 429
        and blocked.status_code == 429
        and int(blocked.headers.get("Retry-After", "0")) >= 1,
    }


def operator_checks(token):
    """Confirm with the real operator script that the stored rows match."""
    status, out, logs = operator("list", "--domain-type", "company")
    listed = {row["email"]: row for row in json.loads(out)} if status == 0 else {}
    mine = listed.get(LOWER, {})
    exported = operator("export", "--csv", *OPERATOR)
    stats = operator("stats")
    stored = rows(
        "SELECT count(*), min(org_id) FROM product_records "
        "WHERE collection = 'waitlist_entries' AND payload->>'email' = %s",
        LOWER,
    )[0]
    submissions = rows(
        "SELECT payload::text FROM product_records "
        "WHERE collection = 'waitlist_submissions'"
    )
    changed = operator(
        "set-status", "--email", EMAIL, "--status", "reviewed", *OPERATOR
    )
    history = rows(
        "SELECT payload->'history'->0->>'actor_kind' FROM product_records "
        "WHERE collection = 'waitlist_entries' AND payload->>'email' = %s",
        LOWER,
    )
    return {
        "operator_list_matches_rows": status == 0
        and mine.get("submission_count") == 2
        and mine.get("domain_type") == "company"
        and token not in out,
        "operator_export_has_the_entry": exported[0] == 0
        and LOWER in exported[1]
        and token not in exported[1],
        "operator_stats_count_entries": stats[0] == 0
        and json.loads(stats[1])["entries"] >= 11,
        "rows_are_global_and_hold_only_a_digest": stored == (1, None)
        and not any(token in payload[0] for payload in submissions),
        "operator_actions_are_audited": changed[0] == 0
        and history == [("operator",)]
        and all(
            line.get("actor_kind") == "operator"
            for line in logs + changed[2]
            if line["event"] == "operator.waitlist"
        ),
    }


def other_checks():
    """Other needs its text on role and location; operators see that text."""
    address = "other.person@waitlist-company.test"
    token = join(address, "wl-other").json().get("token", "")
    asked = request("GET", BASE + "/questions", source_for("wl-reader")).json()
    asking = {q["id"] for q in asked["questions"] if q.get("allow_other")}
    refused = [
        with_token("PUT", f"/answers/{question}", token, body, "wl-other")
        for question in ("role", "company_location")
        for body in (
            {"value": "other"},
            {"value": "other", "other": "   "},
            {"value": "other", "other": "x" * 61},
        )
    ]
    nothing = answers_of(token) == {}
    saved = [
        with_token(
            "PUT",
            f"/answers/{question}",
            token,
            {"value": "other", "other": text},
            "wl-other",
        )
        for question, text in (
            ("role", " Chief of Staff "),
            ("company_location", "Peru"),
        )
    ]
    resumed = with_token("GET", "", token, source="wl-other").json().get("answers", {})
    status, out, _ = operator("list", "--domain-type", "company")
    listed = {row["email"]: row for row in json.loads(out)} if status == 0 else {}
    exported = operator("export", "--csv", *OPERATOR)
    header, *lines = exported[1].splitlines()
    mine = next((line for line in lines if address in line), "")
    stats = json.loads(operator("stats")[1])["questions"]
    erased = operator("delete", "--email", address, *OPERATOR)
    return {
        "other_text_is_asked_on_role_and_location": {"role", "company_location"}
        <= asking,
        "other_without_text_is_422_and_stores_nothing": all(
            r.status_code == 422 for r in refused
        )
        and nothing,
        "other_text_is_trimmed_and_resumes": all(r.status_code == 200 for r in saved)
        and resumed.get("role") == {"value": "other", "other": "Chief of Staff"}
        and resumed.get("company_location") == {"value": "other", "other": "Peru"},
        "operator_shows_other_text": status == 0
        and listed.get(address, {}).get("answers", {}).get("role")
        == "Other (Chief of Staff)"
        and "role_other" in header
        and "Chief of Staff" in mine
        and "Peru" in mine
        and stats["role"]["other_texts"].get("Chief of Staff") == 1
        and stats["company_location"]["other_texts"].get("Peru") == 1
        and erased[0] == 0,
    }


def erasure_checks(token):
    """No account was created; erasing removes the entry, rows and the token."""
    status, out, _ = operator("delete", "--email", LOWER, *OPERATOR)
    left = rows(
        "SELECT count(*) FROM product_records WHERE collection IN "
        "('waitlist_entries', 'waitlist_submissions') AND payload::text LIKE %s",
        "%" + LOWER + "%",
    )[0][0]
    return {
        "no_account_or_session_created": operator_bearer(LOWER) is None,
        "operator_delete_erases_entry": status == 0
        and json.loads(out)["submissions_deleted"] == 2
        and left == 0,
        "erased_token_stops_working": with_token("GET", "", token).status_code == 401,
    }


def waitlist_control():
    """Run the whole TW-01 scenario and return its named boolean results."""
    flow = flow_checks()
    token = flow.pop("token")
    result = {**flow, **refusal_checks()}
    result.update(other_checks())
    result.update(operator_checks(token))
    result.update(erasure_checks(token))
    return result


def main():
    """Record the control in the private fixture file the browser suite reads."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    fixtures["waitlist"] = waitlist_control()
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print("Waitlist control recorded.")


if __name__ == "__main__":
    main()
