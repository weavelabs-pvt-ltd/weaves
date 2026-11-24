"""Release rehearsal: the manifest gate, the policy pin and the CI wiring."""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import release_rehearsal as cli
from rehearsal import manifest as gate
from rehearsal.run import Rehearsal
from rehearsal.stack import RehearsalError

ROOT = Path(__file__).resolve().parents[2]
POLICY = {
    "min_rollback_schema_version": 28,
    "min_rollback_revision": "a" * 40,
}
REVISION = "b" * 40
GOOD = {
    "version": 1,
    "result": "passed",
    "candidate": {"revision": REVISION, "dirty": False, "schema_head": 29},
    "rollback_floor": {"revision": "a" * 40, "schema_head": 28},
    "database_head_after_upgrade": 29,
    "steps": [{"name": "upgrade", "ok": True}, {"name": "rollback", "ok": True}],
}


def broken(**changes):
    manifest = copy.deepcopy(GOOD)
    for dotted, value in changes.items():
        target = manifest
        *path, last = dotted.split("__")
        for key in path:
            target = target[key]
        target[last] = value
    return manifest


def test_a_complete_passing_manifest_opens_the_gate():
    assert gate.gate_problems(GOOD, POLICY, REVISION) == []


@pytest.mark.parametrize(
    "manifest",
    [
        broken(result="failed"),
        broken(candidate__revision="c" * 40),
        broken(candidate__dirty=True),
        broken(rollback_floor__revision="d" * 40),
        broken(rollback_floor__schema_head=27),
        broken(database_head_after_upgrade=28),
        broken(steps=[]),
        broken(steps=[{"name": "rollback", "ok": False}]),
        broken(version=2),
    ],
)
def test_the_gate_blocks_every_unproven_release(manifest):
    assert gate.gate_problems(manifest, POLICY, REVISION)


def test_verification_fails_closed_without_a_manifest_or_policy(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY))
    assert gate.verify_manifest(tmp_path / "absent.json", policy, REVISION)
    present = tmp_path / "release-compat.json"
    present.write_text(json.dumps(GOOD))
    assert gate.verify_manifest(present, policy, REVISION) == []
    assert gate.verify_manifest(present, tmp_path / "missing.json", REVISION)
    present.write_text("{not json")
    assert gate.verify_manifest(present, policy, REVISION)


def test_the_policy_must_pin_a_commit_and_a_schema_floor(tmp_path):
    for bad in (
        {"min_rollback_schema_version": 28, "min_rollback_revision": None},
        {"min_rollback_schema_version": 28, "min_rollback_revision": "abc"},
        {"min_rollback_schema_version": True, "min_rollback_revision": "a" * 40},
    ):
        path = tmp_path / "p.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            gate.load_policy(path)


def test_the_repository_policy_is_a_pinned_known_commit():
    policy = gate.load_policy(cli.POLICY)
    assert re.fullmatch(r"[0-9a-f]{40}", policy["min_rollback_revision"])


def test_verify_command_reports_the_gate(tmp_path, capsys):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(broken(result="failed")))
    status = cli.main(["verify", str(manifest), "--revision", REVISION])
    assert status == 1
    assert "release gate: blocked" in capsys.readouterr().out


def test_a_failing_step_is_recorded_and_the_run_continues():
    run = Rehearsal.__new__(Rehearsal)
    run.steps = []

    def explode():
        raise RehearsalError("old image cannot read the new schema")

    run.step("rollback", explode)
    run.step("forward", lambda: [])
    assert [s["ok"] for s in run.steps] == [False, True]
    assert "cannot read" in run.steps[0]["detail"]


def test_ci_gates_deploys_on_the_rehearsal_when_the_schema_changes():
    workflow = (ROOT / ".github/workflows/ci-deploy.yml").read_text()
    assert "infrastructure/database/" in workflow
    deploy = workflow[workflow.index("\n  deploy:") :]
    assert "needs: [changes, verify, tenancy, rehearsal]" in deploy
    assert "needs.rehearsal.result == 'success'" in deploy
    tenancy = workflow[
        workflow.index("\n  tenancy:") : workflow.index("\n  rehearsal:")
    ]
    assert "needs.changes.outputs.migrations == 'true'" in tenancy
    assert "retention-days: 7" not in workflow
    assert "scripts/release_rehearsal.py verify" in workflow


def test_the_rehearsal_touches_no_cloud_or_registry_resource():
    text = "".join(
        p.read_text()
        for p in [
            ROOT / "scripts/release_rehearsal.py",
            *(ROOT / "scripts/rehearsal").glob("*.py"),
        ]
    )
    for forbidden in ("gcloud", "pkg.dev", "gsutil", "docker push", "docker pull"):
        assert forbidden not in text
