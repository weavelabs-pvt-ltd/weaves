"""The deploy script retries only the SSH key propagation rejection."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "infra/gcp/singlevm/deploy.py"
_SPEC = importlib.util.spec_from_file_location("singlevm_deploy", _PATH)
deploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deploy)


def _result(code, stderr="", stdout=""):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


def test_key_propagation_rejection_is_retried_until_it_succeeds(monkeypatch):
    results = iter(
        [
            _result(255, "x: Permission denied (publickey)."),
            _result(255, "x: Permission denied (publickey)."),
            _result(0, stdout="ok\n"),
        ]
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: next(results))
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)

    assert deploy.invoke(["gcloud"]) == "ok"


def test_other_failures_are_not_retried(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        return _result(1, "disk full")

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="disk full"):
        deploy.invoke(["gcloud"])
    assert len(calls) == 1


def test_persistent_key_rejection_eventually_fails(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _result(255, "Permission denied (publickey)")
    )
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="publickey"):
        deploy.invoke(["gcloud"], attempts=3)
