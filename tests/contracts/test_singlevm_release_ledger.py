"""Release ledger: append-only history, legacy import and the rollback floor."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra/gcp/singlevm"))
import apply_release as release
import release_ledger as ledger
import rollback_release as cli
from release_ledger import RollbackRefused

PREFIX = "asia-south1-docker.pkg.dev/weaves-510819/weaves/"
SHA = "1" * 40
POLICY = {"min_rollback_schema_version": 28, "min_rollback_revision": None}
OLD = "BACKEND_IMAGE=old-backend\nFRONTEND_IMAGE=old-frontend\n"


def digest(character: str, component: str = "backend") -> str:
    return f"{PREFIX}{component}@sha256:{character * 64}"


def add(root, component, character, schema, result="healthy"):
    return ledger.record_release(
        root, component, digest(character, component), SHA, schema, result
    )


@pytest.fixture
def root(tmp_path):
    (tmp_path / "release_policy.json").write_text(json.dumps(POLICY))
    return tmp_path


def test_ledger_is_append_only_and_sequenced(root):
    add(root, "backend", "a", 27)
    first = (root / "releases.jsonl").read_text()
    add(root, "frontend", "b", None)
    add(root, "backend", "c", 28, "failed")
    text = (root / "releases.jsonl").read_text()
    assert text.startswith(first)
    entries = ledger.read_ledger(root)
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[2]["result"] == "failed" and entries[0]["schema_version"] == 27
    assert (root / "releases.jsonl").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        ledger.record_release(root, "backend", digest("d"), SHA, 28, "unknown")


def test_legacy_releases_json_is_imported_once_without_loss(root):
    latest = {
        "backend": {"image": digest("a"), "revision": SHA},
        "frontend": {"image": digest("b", "frontend"), "revision": "2" * 40},
    }
    (root / "releases.json").write_text(json.dumps(latest))
    view = ledger.read_ledger(root)
    assert not (root / "releases.jsonl").exists()
    assert {e["component"]: e["image"] for e in view} == {
        c: v["image"] for c, v in latest.items()
    }
    add(root, "backend", "c", 28)
    entries = ledger.read_ledger(root)
    assert [e["action"] for e in entries] == ["import", "import", "deploy"]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    ledger.ensure_ledger(root)
    assert len(ledger.read_ledger(root)) == 3
    assert json.loads((root / "releases.json").read_text()) == latest


def test_floor_is_first_healthy_backend_release_at_or_after_policy_schema(root):
    add(root, "backend", "a", None)
    add(root, "backend", "b", 27)
    add(root, "frontend", "f", None)
    add(root, "backend", "c", 28, "failed")
    add(root, "backend", "d", 28)
    add(root, "backend", "e", 29)
    entries = ledger.read_ledger(root)
    assert ledger.rollback_floor(entries, POLICY) == 5
    ledger.check_rollback(entries, POLICY, entries[4])
    ledger.check_rollback(entries, POLICY, entries[5])
    for index in (0, 1, 3):
        with pytest.raises(RollbackRefused):
            ledger.check_rollback(entries, POLICY, entries[index])
    ledger.check_rollback(entries, POLICY, entries[2])
    raised = {**POLICY, "min_rollback_schema_version": 29}
    with pytest.raises(RollbackRefused):
        ledger.check_rollback(entries, raised, entries[4])
    ledger.check_rollback(entries, raised, entries[5])


def test_no_floor_means_no_backend_rollback(root):
    add(root, "backend", "a", None)
    entries = ledger.read_ledger(root)
    with pytest.raises(RollbackRefused):
        ledger.check_rollback(entries, POLICY, entries[0])


def test_find_target_by_sequence_or_digest_prefix(root):
    add(root, "backend", "a", 28)
    add(root, "backend", "b", 28)
    entries = ledger.read_ledger(root)
    assert ledger.find_target(entries, "backend", "2")["image"] == digest("b")
    assert ledger.find_target(entries, "backend", "sha256:" + "a" * 12)["seq"] == 1
    for reference in ("9", "c", "sha256:"):
        with pytest.raises(RollbackRefused):
            ledger.find_target(entries, "backend", reference)
    with pytest.raises(RollbackRefused):
        ledger.find_target(entries, "frontend", "1")


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"min_rollback_schema_version": 0},
        {"min_rollback_schema_version": True},
        {"min_rollback_schema_version": 28, "min_rollback_revision": "abc"},
    ],
)
def test_invalid_policy_blocks_rollback(tmp_path, policy):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(RollbackRefused):
        ledger.load_policy(path)
    with pytest.raises(RollbackRefused):
        ledger.load_policy(tmp_path / "missing.json")


def test_repository_policy_is_valid():
    path = (
        Path(__file__).resolve().parents[2] / "infra/gcp/singlevm/release_policy.json"
    )
    assert ledger.load_policy(path)["min_rollback_schema_version"] >= 28


@pytest.fixture
def host(root, monkeypatch):
    (root / "runtime.env").write_text(OLD)
    for name in release.CONFIG_FILES:
        (root / name).write_text("original " + name)
    config = {
        "volumes": {
            key: {"name": "weaves-single_" + key}
            for key in ("database", "certificates", "gateway_config")
        },
        "services": {"postgres": {"volumes": [{"source": "database"}]}},
    }
    monkeypatch.setattr(
        release, "run", lambda cmd: json.dumps(config) if "config" in cmd else ""
    )
    monkeypatch.setattr(release, "activate", lambda *args: None)
    monkeypatch.setattr(release, "image_schema_version", lambda image: 28)
    return root


def test_deploy_records_healthy_and_keeps_latest_record(host):
    release.apply_locked(host, "backend", digest("a"), SHA, None)
    release.apply_locked(host, "frontend", digest("b", "frontend"), SHA, None)
    entries = ledger.read_ledger(host)
    assert [(e["component"], e["result"], e["schema_version"]) for e in entries] == [
        ("backend", "healthy", 28),
        ("frontend", "healthy", None),
    ]
    latest = json.loads((host / "releases.json").read_text())
    assert latest["backend"]["image"] == digest("a")


def test_failed_activation_is_recorded_honestly(host, monkeypatch):
    calls = []

    def activate(root, component, gateway_changed):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("readiness failed")

    monkeypatch.setattr(release, "activate", activate)
    with pytest.raises(RuntimeError):
        release.apply_locked(host, "backend", digest("a"), SHA, None)
    (entry,) = ledger.read_ledger(host)
    assert entry["result"] == "failed" and entry["image"] == digest("a")
    assert not (host / "releases.json").exists()


def test_manual_redeploy_below_floor_is_refused_before_any_change(host):
    add(host, "backend", "a", 27)
    add(host, "backend", "b", 28)
    with pytest.raises(RollbackRefused):
        release.apply_locked(host, "backend", digest("a"), SHA, None)
    assert (host / "runtime.env").read_text() == OLD
    assert len(ledger.read_ledger(host)) == 2
    release.apply_locked(host, "backend", digest("b"), SHA, None)
    release.apply_locked(host, "backend", digest("c"), SHA, None)


def test_rollback_command_applies_ledger_digest_and_refuses_below_floor(
    host, monkeypatch
):
    add(host, "backend", "a", 27)
    add(host, "backend", "b", 28)
    add(host, "backend", "c", 29)
    applied = []
    monkeypatch.setattr(cli, "deploy", lambda *args: applied.append(args))
    with pytest.raises(RollbackRefused):
        cli.rollback(host, "backend", "1", "token", False)
    with pytest.raises(RollbackRefused, match="already running"):
        cli.rollback(host, "backend", "3", "token", False)
    assert cli.rollback(host, "backend", "2", "", True).startswith("Dry run")
    assert applied == []
    cli.rollback(host, "backend", "sha256:" + "b" * 10, "token", False)
    _, component, image, _, token, bundle, action = applied[0]
    assert (component, image, token, bundle, action) == (
        "backend",
        digest("b"),
        "token",
        None,
        "rollback",
    )


def test_list_marks_the_rollback_floor(host):
    add(host, "backend", "a", 27)
    add(host, "backend", "b", 28)
    text = cli.list_releases(host, "backend")
    lines = text.splitlines()
    assert lines[2].startswith("2   *") and lines[1].startswith("1    ")
    assert digest("a") in text and digest("b") in text
