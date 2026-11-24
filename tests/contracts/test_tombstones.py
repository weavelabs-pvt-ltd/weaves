"""Tombstone log, restore gate and the credential/secret/purge hooks."""

import json
import threading

import pytest

from infrastructure.secrets.store import InMemorySecretStore
from modules.recovery import tombstones as ts
from modules.recovery.reconcile import ReconcileRefused, build_plan, verify_head
from modules.recovery.restore_gate import (
    RestoreNotReconciled,
    assert_from_env,
    assert_services_may_start,
    mark_reconciled,
    mark_restore,
    pending_restores,
)
from modules.recovery.secret_tombstones import TombstonedSecretStore
from shared.request_context import reset_request_principal, set_request_identity


def log_in(tmp_path):
    return ts.FileTombstoneLog(tmp_path / "t")


def test_records_are_chained_and_idempotent(tmp_path):
    log = log_in(tmp_path)
    log.record(ts.CREDENTIAL_REVOKED, "o1", subject_id="c1", subject_digest="d")
    log.record(ts.CREDENTIAL_REVOKED, "o1", subject_id="c1", subject_digest="d")
    log.record(ts.ORGANIZATION_DELETED, "o1", erased=("secrets",))
    entries = log.read()
    assert [e.seq for e in entries] == [1, 2]
    assert entries[1].erased == ("secrets",)
    assert log.head()["seq"] == 2 and log.head()["hash"] == entries[1].hash
    assert (oct(log.path.stat().st_mode & 0o777)) == "0o640"


def test_tombstones_hold_only_ids_and_timestamps(tmp_path):
    log = log_in(tmp_path)
    log.record(ts.CREDENTIAL_REVOKED, "o1", subject_id="c1", subject_digest="d")
    line = json.loads(log.path.read_text())
    assert set(line) == {
        "v", "seq", "id", "kind", "org_id", "subject_kind", "subject_id",
        "subject_digest", "at", "erased", "prev", "hash",
    }  # fmt: skip


def test_editing_or_truncating_is_detected(tmp_path):
    log = log_in(tmp_path)
    for index in range(3):
        log.record(ts.SECRET_DELETED, "o", subject_id=f"s{index}")
    lines = log.path.read_text().splitlines()
    log.path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # dropped one
    with pytest.raises(ts.TombstoneLogCorrupt):
        log.read()
    log.path.write_text(lines[0].replace('"o"', '"x"') + "\n")  # edited
    with pytest.raises(ts.TombstoneLogCorrupt):
        log.read()
    with pytest.raises(ts.TombstoneWriteError):  # refuses to extend a bad chain
        log.record(ts.SECRET_DELETED, "o", subject_id="new")


def test_write_failure_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(ts.TombstoneWriteError):
        ts.FileTombstoneLog(blocker / "sub").record(ts.SECRET_DELETED, "o")


def test_concurrent_writers_keep_one_valid_chain(tmp_path):
    log = log_in(tmp_path)
    threads = [
        threading.Thread(
            target=log.record,
            args=(ts.SECRET_DELETED, "o"),
            kwargs={"subject_id": str(i)},
        )
        for i in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [e.seq for e in log.read()] == list(range(1, 13))


def test_env_factory_and_cli_head(tmp_path, capsys):
    assert isinstance(ts.tombstones_from_env({}), ts.NullTombstones)
    sink = ts.tombstones_from_env({"WEAVES_TOMBSTONE_DIR": str(tmp_path)})
    assert isinstance(sink, ts.FileTombstoneLog)
    assert ts.main(["head", "--dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["seq"] == 0
    assert ts.main(["nope"]) == 2


def test_head_verification_and_plan(tmp_path):
    log = log_in(tmp_path)
    log.record(ts.SECRET_REPLACED, "o", subject_id="old")
    head = log.head()
    log.record(ts.SECRET_REPLACED, "o", subject_id="new")
    log.record(ts.SECRET_DELETED, "o", subject_id="gone")
    log.record(ts.ORGANIZATION_PURGE_STARTED, "o2")
    log.record(ts.ORGANIZATION_DELETED, "o2")
    entries = log.read()
    seq = verify_head(entries, head)
    plan = build_plan(entries, seq)
    assert plan.organizations == ["o2"]
    assert plan.secrets == [("o", "new"), ("o", "gone")]
    assert plan.skipped_replacements == ["old"]
    assert build_plan(entries, None).skipped_replacements == ["old", "new"]
    with pytest.raises(ReconcileRefused):
        verify_head(entries[:1], {"seq": 3, "hash": "x"})
    with pytest.raises(ReconcileRefused):
        verify_head(entries, {"seq": 1, "hash": "not-it"})


def test_restore_gate(tmp_path, monkeypatch):
    assert_services_may_start(None)
    assert_services_may_start(tmp_path / "missing")
    restore_id = mark_restore(tmp_path, "weaves-1.sql.gz")
    assert pending_restores(tmp_path) == [restore_id]
    with pytest.raises(RestoreNotReconciled):
        assert_services_may_start(tmp_path)
    monkeypatch.setenv("WEAVES_TOMBSTONE_DIR", str(tmp_path))
    with pytest.raises(RestoreNotReconciled):
        assert_from_env()
    mark_reconciled(tmp_path, restore_id, {"ok": True})
    assert pending_restores(tmp_path) == []
    assert_from_env()


def test_secret_store_tombstones_before_changing(tmp_path):
    log = log_in(tmp_path)
    store = TombstonedSecretStore(InMemorySecretStore(), log)
    token = set_request_identity("p", "org-1", "ws-1")
    try:
        ref = store.put("v")
        store.replace(ref, "v2")
        assert store.get(ref) == "v2"
        store.delete(ref)
        broken = TombstonedSecretStore(
            InMemorySecretStore(), ts.FileTombstoneLog(tmp_path / "f" / "x")
        )
        (tmp_path / "f").write_text("not a dir")
        keep = broken._inner.put("keep")
        with pytest.raises(ts.TombstoneWriteError):
            broken.delete(keep)
        assert broken.get(keep) == "keep"  # fail closed: still stored
    finally:
        reset_request_principal(token)
    assert [(e.kind, e.org_id) for e in log.read()] == [
        (ts.SECRET_REPLACED, "org-1"),
        (ts.SECRET_DELETED, "org-1"),
    ]
