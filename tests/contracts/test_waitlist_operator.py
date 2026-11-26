"""The operator script lists, exports, reports on, changes and erases sign-ups."""

from __future__ import annotations

import csv
import io
import json

import pytest

from contracts.v1 import WaitlistAnswer
from tests.support.operator_scripts import run_script, waitlist
from tests.support.waitlist_world import answer, client_for, join, memory_runtime

OP = ("--operator", "founder@weaves.test")


@pytest.fixture
def world():
    runtime = memory_runtime()
    one = client_for(runtime, "198.51.100.1")
    two = client_for(runtime, "198.51.100.2")
    first = join(one, "Ada@Acme-Corp.test", source={"page": "/", "utm_source": "li"})
    answer(one, first.json()["token"], "company_location", "europe")
    answer(one, first.json()["token"], "tools", ["slack", "other"], other="=cmd()")
    second = join(two, "bob@gmail.com").json()
    answer(two, second["token"], "company_location", "india")
    join(two, "ada@acme-corp.test")  # a second submission for Ada, no answers
    yield runtime
    runtime.close()


def _run(capsys, runtime, *args):
    status, out, logs = run_script(waitlist, capsys, runtime, *args)
    return status, out, logs


def test_list_filters_by_status_domain_date_and_answer(world, capsys):
    status, out, logs = _run(capsys, world, "list")
    rows = json.loads(out)
    assert status == 0 and [r["email"] for r in rows] == [
        "ada@acme-corp.test",
        "bob@gmail.com",
    ]
    assert rows[0]["submission_count"] == 2 and rows[0]["answers"] == {}
    assert (
        logs[-1]["event"] == "operator.waitlist"
        and logs[-1]["actor_kind"] == "operator"
    )
    _, out, _ = _run(capsys, world, "list", "--domain-type", "public")
    assert [r["email"] for r in json.loads(out)] == ["bob@gmail.com"]
    _, out, _ = _run(capsys, world, "list", "--answer", "company_location=india")
    rows = json.loads(out)
    assert [r["email"] for r in rows] == ["bob@gmail.com"]
    assert rows[0]["answers"] == {"company_location": "India"}
    _, out, _ = _run(capsys, world, "list", "--since", "2999-01-01")
    assert json.loads(out) == []
    _, out, _ = _run(capsys, world, "list", "--status", "contacted")
    assert json.loads(out) == []


def test_list_shows_the_latest_submission_of_each_email(world, capsys):
    service = world.waitlist_service
    ada = next(e for e in service.entries.list() if e.email.startswith("ada"))
    latest = max(
        service.submissions.list_by_field("entry_id", ada.id),
        key=lambda s: s.created_at,
    )
    assert latest.answers == {}  # the newest join has no answers; the report uses it
    _, out, _ = _run(capsys, world, "list", "--answer", "company_location=europe")
    assert json.loads(out) == []


def test_export_csv_has_one_column_per_question_and_neutralizes_formulas(world, capsys):
    status, out, logs = _run(capsys, world, "export", "--csv", *OP)
    assert status == 0
    rows = list(csv.DictReader(io.StringIO(out)))
    assert [r["email"] for r in rows] == ["ada@acme-corp.test", "bob@gmail.com"]
    assert (
        "company_location" in rows[0] and "tools" in rows[0] and "utm_source" in rows[0]
    )
    assert rows[1]["company_location"] == "India" and rows[1]["domain_type"] == "public"
    assert "token" not in out.lower().replace("tokens", "")
    assert logs[-1]["command"] == "export" and logs[-1]["rows"] == 2


def test_export_cells_starting_with_formula_characters_are_quoted(capsys):
    runtime = memory_runtime()
    client = client_for(runtime)
    join(client, "=cmd@acme-corp.test")
    _run(
        capsys,
        runtime,
        "note",
        "--email",
        "=cmd@acme-corp.test",
        "--text",
        "@SUM(1)",
        *OP,
    )
    _, out, _ = _run(capsys, runtime, "export", "--csv", *OP)
    row = next(csv.DictReader(io.StringIO(out)))
    assert row["email"] == "'=cmd@acme-corp.test" and row["notes"] == "'@SUM(1)"
    runtime.close()


def test_stats_reports_signups_completion_distribution_and_domain_share(world, capsys):
    status, out, _ = _run(capsys, world, "stats")
    stats = json.loads(out)
    assert status == 0 and stats["entries"] == 2 and stats["submissions"] == 3
    assert sum(stats["signups_per_day"].values()) == 2
    assert stats["company_vs_public"] == {
        "company": 1,
        "public": 1,
        "company_share": 0.5,
    }
    location = stats["questions"]["company_location"]
    assert location["answered"] == 1 and location["completion"] == 0.5
    assert location["distribution"] == {"india": 1}
    assert set(stats["questions"]) == {
        "company_location",
        "company_size",
        "role",
        "waiting_on",
        "ai_today",
        "tools",
    }


def test_status_and_notes_are_changed_and_audited(world, capsys):
    status, out, logs = _run(
        capsys,
        world,
        "set-status",
        "--email",
        "BOB@gmail.com",
        "--status",
        "contacted",
        *OP,
    )
    assert status == 0 and json.loads(out)["status"] == "contacted"
    assert logs[-1]["operator"] == "founder@weaves.test"
    assert logs[-1]["actor_kind"] == "operator" and logs[-1]["entry_id"].startswith(
        "wle_"
    )
    _run(
        capsys,
        world,
        "note",
        "--email",
        "bob@gmail.com",
        "--text",
        "met at a fair",
        *OP,
    )
    entry = next(e for e in world.waitlist_service.entries.list() if "bob" in e.email)
    assert entry.status.value == "contacted" and entry.notes == "met at a fair"
    assert [h.action for h in entry.history] == ["status:contacted", "note"]
    assert all(h.actor_kind == "operator" for h in entry.history)
    _, out, _ = _run(capsys, world, "list", "--status", "contacted")
    assert [r["email"] for r in json.loads(out)] == ["bob@gmail.com"]


def test_bad_input_is_refused_without_a_change(world, capsys):
    for args in (
        ("set-status", "--email", "bob@gmail.com", "--status", "vip", *OP),
        ("set-status", "--email", "nobody@acme-corp.test", "--status", "new", *OP),
        ("note", "--email", "nobody@acme-corp.test", "--text", "x", *OP),
        ("delete", "--email", "nobody@acme-corp.test", *OP),
        ("list", "--since", "yesterday"),
        ("list", "--answer", "broken"),
    ):
        status, out, logs = _run(capsys, world, *args)
        assert status == 2 and out == "", args
        assert logs[-1]["event"] == "operator.waitlist.refused"
    with pytest.raises(SystemExit):
        waitlist.run(["delete", "--email", "bob@gmail.com"], world)  # no operator


def test_delete_removes_the_entry_and_every_submission(world, capsys):
    status, out, logs = _run(
        capsys, world, "delete", "--email", "Ada@acme-corp.test", *OP
    )
    result = json.loads(out)
    assert status == 0 and result["submissions_deleted"] == 2
    service = world.waitlist_service
    assert [e.email for e in service.entries.list()] == ["bob@gmail.com"]
    assert len(service.submissions.list()) == 1
    assert "ada@acme-corp.test" not in json.dumps(logs)
    assert "ada@acme-corp.test" not in out


def test_the_script_refuses_to_run_with_demo_seeding(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("WEAVES_SEED_LOCAL_DEFAULTS", "true")
    with pytest.raises(RuntimeError, match="WEAVES_SEED_LOCAL_DEFAULTS=false"):
        waitlist.run(["list"])


def test_other_text_shows_in_list_export_and_stats_and_legacy_rows_still_read(capsys):
    runtime = memory_runtime()
    client = client_for(runtime, "198.51.100.5")
    new = join(client, "new@acme-corp.test").json()["token"]
    answer(client, new, "role", "other", other="Chief of Staff")
    answer(client, new, "company_location", "other", other="Peru")
    answer(client, new, "tools", ["other"], other="Linear")
    old = join(client, "old@acme-corp.test").json()["token"]
    service = runtime.waitlist_service
    entry = next(e for e in service.entries.list() if e.email.startswith("old"))
    (submission,) = service.submissions.list_by_field("entry_id", entry.id)
    del old
    service.submissions.put(
        submission.model_copy(
            update={
                "question_version": 1,
                "answers": {
                    "role": WaitlistAnswer(value="other"),
                    "company_location": WaitlistAnswer(value="other"),
                },
            }
        )
    )
    _, out, _ = _run(capsys, runtime, "list")
    rows = {r["email"]: r for r in json.loads(out)}
    assert rows["new@acme-corp.test"]["answers"]["role"] == "Other (Chief of Staff)"
    assert rows["old@acme-corp.test"]["answers"]["role"] == "Other"
    status, out, _ = _run(capsys, runtime, "export", "--csv", *OP)
    exported = {r["email"]: r for r in csv.DictReader(io.StringIO(out))}
    assert status == 0
    new_row, old_row = exported["new@acme-corp.test"], exported["old@acme-corp.test"]
    assert new_row["role_other"] == "Chief of Staff"
    assert new_row["company_location_other"] == "Peru"
    assert new_row["tools_other"] == "Linear" and new_row["waiting_on_other"] == ""
    assert old_row["role"] == "Other" and old_row["role_other"] == ""
    assert "company_size_other" not in new_row
    status, out, _ = _run(capsys, runtime, "stats")
    stats = json.loads(out)["questions"]
    assert status == 0
    assert stats["role"]["distribution"] == {"other": 2}
    assert stats["role"]["other_texts"] == {"Chief of Staff": 1}
    assert stats["company_location"]["other_texts"] == {"Peru": 1}
    assert stats["tools"]["other_texts"] == {"Linear": 1}
    assert "other_texts" not in stats["company_size"]
    runtime.close()
