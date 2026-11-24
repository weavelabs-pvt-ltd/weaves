"""The private-beta gate reports honestly, reads only, and never approves."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import gate_collect_production as collector
import private_beta_gate as gate
from beta_gate import production_checks, repo_checks
from beta_gate.model import BLOCKED, FAIL, PASS, UNVERIFIED, Context

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ROOT = SCRIPTS.parent
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
STUB = "x" * 1600
COMPOSE = """
      WEAVES_PUBLIC_REGISTRATION: "false"
      WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION: ${X:-false}
      WEAVES_WORKER_SLOTS: ${WEAVES_WORKER_SLOTS:-4}
      WEAVES_LIMITS_MODE: ${WEAVES_LIMITS_MODE:-observe}
      WEAVES_LIMIT_MAX_RUNNING_PER_ORG: ${WEAVES_LIMIT_MAX_RUNNING_PER_ORG:-2}
      WEAVES_TOMBSTONE_DIR: /var/lib/weaves/tombstones
"""
POLICY = {"min_rollback_schema_version": 30, "min_rollback_revision": "a" * 40}
DECLARATION = "worker_slots=4 limits_mode=observe max_running_per_org=2"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal repository that satisfies every repository check."""
    root = tmp_path / "repo"
    for name in repo_checks.RUNBOOKS:
        write(root / f"docs/runbooks/{name}.md", STUB)
    write(root / "docs/supported-capacity.md", f"<!-- gate: {DECLARATION} -->\n")
    write(root / "infra/gcp/singlevm/compose.yaml", COMPOSE)
    write(root / "infra/gcp/singlevm/release_policy.json", json.dumps(POLICY))
    write(
        root / "tenancy_harness/pair.json",
        json.dumps(
            {
                "required_controls": [
                    "T0-I01",
                    "T1-I01",
                    "T4-I01",
                    "T5-I01",
                    "T5-I05",
                    "T6-I03",
                ],
                "planned_controls": {},
            }
        ),
    )
    for path in (
        "infra/gcp/singlevm/restore_reconcile.py",
        "infra/gcp/singlevm/platform_control.py",
        "modules/recovery/tombstones.py",
        "modules/platform_controls/controls.py",
    ):
        write(root / path, "")
    return root


def context(root: Path, **overrides) -> Context:
    values = {
        "root": root,
        "evidence_dir": root / ".local/evidence",
        "production": None,
        "github_protected": None,
        "now": NOW,
    }
    values.update(overrides)
    return Context(**values)


def by_id(results):
    return {r.id: r for r in results}


def passing_rehearsal(days: int = 1) -> dict:
    return {
        "version": 1,
        "result": "passed",
        "started": (NOW - timedelta(days=days)).isoformat(),
        "candidate": {"revision": "b" * 40, "dirty": False, "schema_head": 31},
        "rollback_floor": {"revision": "a" * 40, "schema_head": 30},
        "database_head_after_upgrade": 31,
        "steps": [{"name": "upgrade", "ok": True}],
    }


def state(**changes) -> dict:
    document = {
        "version": 1,
        "collected_at": (NOW - timedelta(hours=1)).isoformat(),
        "env": {"WEAVES_LIMITS_MODE": "observe", "WEAVES_WORKER_SLOTS": "4"},
        "allowlist_set": True,
        "newest_backup_age_hours": 5.0,
        "platform_controls": {"effective": {"signup_enabled": True}},
    }
    document.update(changes)
    return document


# ---- repository checks ----------------------------------------------------
def test_a_complete_tree_passes_every_repository_check(tree):
    ctx = context(tree)
    ctx.evidence_dir.mkdir(parents=True)
    (ctx.evidence_dir / "release-compat.json").write_text(
        json.dumps(passing_rehearsal())
    )
    (ctx.evidence_dir / "dr-drill.json").write_text(
        json.dumps({"result": "passed", "started": NOW.isoformat()})
    )
    results = by_id(gate.evaluate(ctx))
    for item in ("R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08"):
        assert results[item].status == PASS, results[item]


def test_missing_or_stub_runbook_fails(tree):
    (tree / "docs/runbooks/secrets-rotation.md").unlink()
    write(tree / "docs/runbooks/tenant-recovery.md", "TODO")
    result = repo_checks.runbooks(context(tree))
    assert result.status == FAIL
    assert "secrets-rotation" in result.detail and "tenant-recovery" in result.detail


def test_capacity_doc_must_match_compose_defaults(tree):
    write(tree / "docs/supported-capacity.md", "<!-- gate: worker_slots=2 -->")
    assert repo_checks.capacity_doc(context(tree)).status == FAIL
    write(tree / "docs/supported-capacity.md", "no declaration")
    assert repo_checks.capacity_doc(context(tree)).status == FAIL


def test_open_signup_defaults_fail(tree):
    compose = (tree / "infra/gcp/singlevm/compose.yaml").read_text()
    write(
        tree / "infra/gcp/singlevm/compose.yaml",
        compose.replace('PUBLIC_REGISTRATION: "false"', 'PUBLIC_REGISTRATION: "true"'),
    )
    assert repo_checks.signup_defaults(context(tree)).status == FAIL


def test_required_controls_missing_the_kill_switch_fails(tree):
    pair = json.loads((tree / "tenancy_harness/pair.json").read_text())
    pair["required_controls"].remove("T6-I03")
    write(tree / "tenancy_harness/pair.json", json.dumps(pair))
    assert repo_checks.required_controls(context(tree)).status == FAIL


def test_evidence_must_exist_pass_and_be_recent(tree):
    ctx = context(tree)
    assert repo_checks.local_drill(ctx).status == FAIL  # no evidence at all
    ctx.evidence_dir.mkdir(parents=True)
    drill = ctx.evidence_dir / "dr-drill.json"
    drill.write_text(json.dumps({"result": "failed", "started": NOW.isoformat()}))
    assert repo_checks.local_drill(ctx).status == FAIL
    old = (NOW - timedelta(days=31)).isoformat()
    drill.write_text(json.dumps({"result": "passed", "started": old}))
    assert repo_checks.local_drill(ctx).status == FAIL
    drill.write_text(json.dumps({"result": "passed", "started": NOW.isoformat()}))
    assert repo_checks.local_drill(ctx).status == PASS


def test_rollback_rehearsal_rejects_dirty_wrong_floor_and_old(tree):
    ctx = context(tree)
    ctx.evidence_dir.mkdir(parents=True)
    path = ctx.evidence_dir / "release-compat.json"
    for change in (
        {"candidate": {"revision": "b" * 40, "dirty": True, "schema_head": 31}},
        {"rollback_floor": {"revision": "c" * 40, "schema_head": 30}},
        {"started": (NOW - timedelta(days=91)).isoformat()},
        {"steps": [{"name": "rollback", "ok": False}]},
    ):
        path.write_text(json.dumps({**passing_rehearsal(), **change}))
        assert repo_checks.rollback_rehearsal(ctx).status == FAIL, change
    path.write_text(json.dumps(passing_rehearsal()))
    assert repo_checks.rollback_rehearsal(ctx).status == PASS


def test_unbuilt_owner_slices_are_blocked_not_failed(tree):
    results = [check(context(tree)) for check in repo_checks.OWNER_ITEMS]
    assert {r.status for r in results} == {BLOCKED}
    (tree / "infra/gcp/alerts").mkdir(parents=True)
    assert repo_checks.OWNER_ITEMS[0](context(tree)).status == PASS


# ---- production checks ----------------------------------------------------
def test_without_production_state_every_production_item_is_unverified(tree):
    results = by_id(gate.evaluate(context(tree)))
    for item in ("P01", "P02", "P03", "P04", "P06", "G01"):
        assert results[item].status == UNVERIFIED, item
    assert results["P05"].status == BLOCKED  # off-host backup is an owner approval


def test_stale_production_state_is_unverified(tree):
    old = (NOW - timedelta(days=3)).isoformat()
    ctx = context(tree, production=state(collected_at=old))
    assert production_checks.limits_mode(ctx).status == UNVERIFIED


def test_limits_mode_observe_is_blocked_on_the_owner_enforce_passes(tree):
    observed = context(tree, production=state())
    assert production_checks.limits_mode(observed).status == BLOCKED
    enforced = state(env={"WEAVES_LIMITS_MODE": "enforce"})
    assert (
        production_checks.limits_mode(context(tree, production=enforced)).status == PASS
    )


def test_signup_checks_use_allowlist_registration_and_switch(tree):
    ok = context(tree, production=state())
    assert production_checks.signup_closed(ok).status == PASS
    open_ = context(tree, production=state(allowlist_set=False))
    assert production_checks.signup_closed(open_).status == FAIL
    public = state(env={"WEAVES_PUBLIC_REGISTRATION": "true"})
    assert (
        production_checks.signup_closed(context(tree, production=public)).status == FAIL
    )
    assert production_checks.signup_switch(ok).status == FAIL  # still true: open
    closed = state(platform_controls={"effective": {"signup_enabled": False}})
    assert (
        production_checks.signup_switch(context(tree, production=closed)).status == PASS
    )


def test_backup_freshness_and_capacity_agreement(tree):
    assert production_checks.backups(context(tree, production=state())).status == PASS
    stale = state(newest_backup_age_hours=40)
    assert production_checks.backups(context(tree, production=stale)).status == FAIL
    none = state(newest_backup_age_hours=None)
    assert production_checks.backups(context(tree, production=none)).status == FAIL
    assert (
        production_checks.capacity_matches_deployment(
            context(tree, production=state())
        ).status
        == PASS
    )
    drift = state(env={"WEAVES_WORKER_SLOTS": "2"})
    drifted = production_checks.capacity_matches_deployment(
        context(tree, production=drift)
    )
    assert drifted.status == FAIL


def test_offhost_backup_passes_only_with_a_fresh_copy(tree):
    fresh = state(offhost_backup_age_hours=3.0)
    assert (
        production_checks.offhost_backup(context(tree, production=fresh)).status == PASS
    )
    old = state(offhost_backup_age_hours=80.0)
    assert (
        production_checks.offhost_backup(context(tree, production=old)).status == FAIL
    )


def test_branch_protection_is_tri_state(tree):
    assert production_checks.branch_protection(context(tree)).status == UNVERIFIED
    off = context(tree, github_protected=False)
    assert production_checks.branch_protection(off).status == BLOCKED
    assert (
        production_checks.branch_protection(context(tree, github_protected=True)).status
        == PASS
    )


# ---- the report -----------------------------------------------------------
def test_exit_status_ranks_fail_over_open_over_pass():
    from beta_gate.model import Result

    def item(status):
        return Result("x", "t", status, "d")

    assert gate.exit_status([item(PASS)]) == 0
    assert gate.exit_status([item(PASS), item(BLOCKED)]) == 2
    assert gate.exit_status([item(UNVERIFIED), item(BLOCKED)]) == 2
    assert gate.exit_status([item(BLOCKED), item(FAIL)]) == 1


def test_a_crashing_check_is_reported_as_a_failure(tree, monkeypatch):
    def boom(ctx):
        raise RuntimeError("broken")

    monkeypatch.setattr(gate, "REPO_CHECKS", (boom,))
    results = gate.evaluate(context(tree))
    assert results[0].status == FAIL and "crashed" in results[0].detail


def digest_of(root: Path) -> str:
    sha = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        sha.update(path.relative_to(root).as_posix().encode())
        sha.update(path.read_bytes())
    return sha.hexdigest()


def test_the_gate_never_writes(tree, capsys):
    before = digest_of(tree)
    state_file = tree.parent / "state.json"
    state_file.write_text(json.dumps(state()))
    code = gate.main(
        ["--root", str(tree), "--production-state", str(state_file), "--json"]
    )
    assert code in (1, 2)
    assert digest_of(tree) == before
    assert not (tree / ".local").exists()
    report = json.loads(capsys.readouterr().out)
    assert {r["status"] for r in report} <= {PASS, FAIL, BLOCKED, UNVERIFIED}


def test_github_lookup_failure_is_unknown_not_a_pass(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no gh")

    monkeypatch.setattr(gate.subprocess, "run", broken)
    assert gate.github_protected() is None


def test_text_report_states_that_signup_stays_off(tree, capsys):
    gate.main(["--root", str(tree)])
    text = capsys.readouterr().out
    assert "Public signup stays off" in text and "approves nothing" in text


def test_unreadable_production_state_is_an_error(tree, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert gate.main(["--root", str(tree), "--production-state", str(bad)]) == 1


def test_this_repository_has_no_repository_side_failures():
    """The real checkout satisfies every check that needs no evidence or state."""
    ctx = context(ROOT, evidence_dir=ROOT / "nonexistent-evidence")
    results = by_id(gate.evaluate(ctx))
    for item in ("R01", "R02", "R03", "R04", "R07", "R08"):
        assert results[item].status == PASS, results[item]


# ---- the production collector ---------------------------------------------
def test_collector_reads_names_only_and_hides_the_allowlist(tmp_path):
    env = tmp_path / "runtime.env"
    env.write_text(
        "# comment\nWEAVES_LIMITS_MODE=observe\n"
        "WEAVES_SECRET_ENCRYPTION_KEY=SECRET-VALUE\n"
        "WEAVES_ACCOUNT_ALLOWED_EMAILS='owner@example.com'\n"
    )
    values = collector.read_env(env)
    assert values["WEAVES_ACCOUNT_ALLOWED_EMAILS"] == "owner@example.com"
    kept = {name: values[name] for name in collector.PLAIN if name in values}
    assert kept == {"WEAVES_LIMITS_MODE": "observe"}
    assert "WEAVES_ACCOUNT_ALLOWED_EMAILS" not in collector.PLAIN
    assert "WEAVES_SECRET_ENCRYPTION_KEY" not in collector.PLAIN


def test_collector_backup_age(tmp_path):
    assert collector.newest_backup_age_hours(tmp_path) is None
    dump = tmp_path / "weaves-20261124T000000Z.sql.gz"
    dump.write_bytes(b"x")
    assert collector.newest_backup_age_hours(tmp_path) < 0.1
