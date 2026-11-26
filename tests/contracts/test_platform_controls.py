"""Slice 6.4 unit tests: switches, cache latency, hooks and the audit helper."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.ports.limits import StaticLimitPolicy
from apps.api.platform_gate import install_platform_gate
from apps.worker.loop import WorkerLoop
from infrastructure.database.later_migrations import LATER_MIGRATIONS
from infrastructure.database.store import SCHEMA_HEAD_VERSION
from modules.limits.errors import LimitExceeded
from modules.limits.service import AdmissionService
from modules.platform_controls.admin import ControlsAdmin
from modules.platform_controls.audit import OperatorAudit
from modules.platform_controls.controls import PlatformControls, StoredControls
from modules.platform_controls.store import MemoryControlsStore
from shared.limits_config import LimitsConfig

SINGLEVM = Path(__file__).resolve().parents[2] / "infra/gcp/singlevm"
sys.path.insert(0, str(SINGLEVM))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Counts:
    def count(self, org_id: str, resource: str) -> int:
        return 0

    def global_queued(self) -> int:
        return 0


def controls_over(
    store: MemoryControlsStore, clock: Clock, *, admissions_default: bool = False
) -> PlatformControls:
    return PlatformControls(
        store.load, admissions_default=admissions_default, clock=clock
    )


def admin_for(store: MemoryControlsStore, controls: PlatformControls) -> ControlsAdmin:
    return ControlsAdmin(store, OperatorAudit(store), controls)


def test_migration_30_is_registered_once_and_is_the_head() -> None:
    versions = [version for version, _ in LATER_MIGRATIONS]
    assert versions == sorted(set(versions))
    assert 30 in versions and SCHEMA_HEAD_VERSION == 32


def test_defaults_change_nothing_until_a_switch_is_set() -> None:
    store, clock = MemoryControlsStore(), Clock()
    snapshot = controls_over(store, clock).snapshot()
    assert (snapshot.admissions_paused, snapshot.worker_paused) == (False, False)
    assert (snapshot.read_only, snapshot.signup_enabled) == (False, True)
    assert snapshot.admissions_forced is False


def test_environment_default_applies_until_overridden_either_way() -> None:
    store, clock = MemoryControlsStore(), Clock()
    controls = controls_over(store, clock, admissions_default=True)
    admin = admin_for(store, controls)
    assert controls.snapshot().admissions_paused is True
    assert controls.snapshot().admissions_forced is False
    admin.set("op@example.test", "resume", {"admissions_paused": False})
    assert controls.snapshot().admissions_paused is False
    admin.clear("op@example.test", "back to default", ("admissions_paused",))
    assert controls.snapshot().admissions_paused is True


def test_a_change_is_visible_within_the_cache_lifetime_without_restart() -> None:
    store, clock = MemoryControlsStore(), Clock()
    controls = controls_over(store, clock)
    reader = PlatformControls(store.load, clock=clock)  # another process
    assert reader.snapshot().read_only is False
    admin_for(store, controls).set("op@example.test", "drill", {"read_only": True})
    clock.now += 4.9
    assert reader.snapshot().read_only is False  # still inside the 5 s cache
    clock.now += 0.2
    assert reader.snapshot().read_only is True  # live after at most ~5 s


def test_cache_does_not_read_the_store_per_call() -> None:
    clock, loads = Clock(), []

    def load() -> StoredControls:
        loads.append(1)
        return StoredControls()

    controls = PlatformControls(load, clock=clock)
    for _ in range(50):
        controls.snapshot()
    assert len(loads) == 1


def test_a_failed_read_keeps_the_last_value_and_never_raises() -> None:
    clock, state = Clock(), {"fail": False}

    def load() -> StoredControls:
        if state["fail"]:
            raise RuntimeError("database down")
        return StoredControls(read_only=True)

    controls = PlatformControls(load, clock=clock)
    assert controls.snapshot().read_only is True
    state["fail"] = True
    clock.now += 10
    assert controls.snapshot().read_only is True  # fail closed for explicit read-only
    never_read = PlatformControls(load, clock=clock)
    assert never_read.snapshot().read_only is False  # defaults before any read


def test_forced_pause_refuses_work_in_every_limits_mode() -> None:
    for mode in ("off", "observe", "enforce"):
        store, clock = MemoryControlsStore(), Clock()
        controls = controls_over(store, clock)
        admission = AdmissionService(
            StaticLimitPolicy(LimitsConfig(mode=mode)),  # type: ignore[arg-type]
            Counts(),
            None,
            controls,
        )
        admission.admit_work("org-a")  # nothing set: admitted
        admin_for(store, controls).set(
            "op@example.test", "x", {"admissions_paused": True}
        )
        with pytest.raises(LimitExceeded) as caught:
            admission.admit_work("org-a")
        assert caught.value.resource == "admissions_paused"
        assert caught.value.retry_after_seconds == 30
        assert admission.usage("org-a").admissions_paused is True


def test_worker_claiming_stops_while_paused_and_resumes() -> None:
    store, clock = MemoryControlsStore(), Clock()
    controls = controls_over(store, clock)
    calls: list[str] = []
    services = SimpleNamespace(
        execution=SimpleNamespace(
            process_next_execution_job=lambda worker, **kw: calls.append("job")
        ),
        tasks=SimpleNamespace(
            process_next_turn=lambda worker, **kw: calls.append("turn")
        ),
        worker_paused=controls.worker_paused,
    )
    from threading import Event

    loop = WorkerLoop(
        services,  # type: ignore[arg-type]
        SimpleNamespace(ticks=[]),  # type: ignore[arg-type]
        worker_id="w",
        poll_seconds=0.01,
        stopping=Event(),
    )
    admin = admin_for(store, controls)
    admin.set("op@example.test", "stop claims", {"worker_paused": True})
    assert loop.claim_and_run() is False and calls == []
    assert loop.claim_once("w-s1") is False and calls == []
    admin.clear("op@example.test", "resume", ("worker_paused",))
    loop.claim_and_run()
    assert calls == ["job", "turn"]


def gated_app(controls: PlatformControls) -> FastAPI:
    app = FastAPI()
    app.state.platform_controls = controls
    install_platform_gate(app)

    @app.get("/api/v0/things")
    def read() -> dict[str, str]:
        return {"ok": "read"}

    @app.post("/api/v0/things")
    def write() -> dict[str, str]:
        return {"ok": "write"}

    @app.post("/other")
    def other() -> dict[str, str]:
        return {"ok": "other"}

    return app


def test_read_only_refuses_writes_with_a_clear_503_but_serves_reads() -> None:
    store, clock = MemoryControlsStore(), Clock()
    controls = controls_over(store, clock)
    client = TestClient(gated_app(controls))
    assert client.post("/api/v0/things").status_code == 200
    admin = admin_for(store, controls)
    admin.set("op@example.test", "restore", {"read_only": True}, message="Back soon.")
    refused = client.post("/api/v0/things")
    assert refused.status_code == 503
    assert refused.json() == {"detail": "Back soon.", "code": "platform_read_only"}
    assert refused.headers["Retry-After"] == "30"
    assert client.get("/api/v0/things").status_code == 200
    assert client.post("/other").status_code == 200  # outside /api/v0/
    admin.clear("op@example.test", "done", ())
    assert client.post("/api/v0/things").status_code == 200


def test_read_only_default_message_is_neutral() -> None:
    store, clock = MemoryControlsStore(), Clock()
    controls = controls_over(store, clock)
    admin_for(store, controls).set("op@example.test", "x", {"read_only": True})
    body = TestClient(gated_app(controls)).delete("/api/v0/things").json()
    assert body["code"] == "platform_read_only"
    assert "read-only maintenance" in body["detail"]


def test_audit_helper_validates_and_orders_newest_first() -> None:
    store = MemoryControlsStore()
    audit = OperatorAudit(store)
    with pytest.raises(ValueError):
        audit.record(operator="", command="x", reason="r")
    with pytest.raises(ValueError):
        audit.record(operator="a@b.c", command="x", reason="  ")
    first = audit.record(operator="a@b.c", command="one", reason="r", org_id="o1")
    second = audit.record(
        operator="a@b.c", command="two", reason="r", ticket="T-1", org_id="o2"
    )
    assert first.correlation_id.startswith("op-")
    assert [a.command for a in audit.recent()] == ["two", "one"]
    assert [a.command for a in audit.recent(org_id="o1")] == ["one"]
    assert second.ticket == "T-1"


def test_set_requires_a_known_switch_and_a_reason() -> None:
    store, clock = MemoryControlsStore(), Clock()
    admin = admin_for(store, controls_over(store, clock))
    with pytest.raises(ValueError):
        admin.set("op@example.test", "x", {})
    with pytest.raises(ValueError):
        admin.set("op@example.test", "x", {"nonsense": True})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        admin.set("op@example.test", " ", {"read_only": True})
    assert store.list_actions(10) == []


def test_set_and_clear_each_append_one_audit_row() -> None:
    store, clock = MemoryControlsStore(), Clock()
    admin = admin_for(store, controls_over(store, clock))
    admin.set("op@example.test", "incident", {"read_only": True}, ticket="T-9")
    admin.clear("op@example.test", "over", ("read_only",))
    rows = store.list_actions(10)
    assert [r.command for r in rows] == ["platform.clear", "platform.set"]
    assert rows[1].details["changes"] == {"read_only": True}
    assert rows[1].ticket == "T-9" and rows[1].result == "ok"


def test_platform_control_script_is_bundled_and_installed() -> None:
    import apply_release
    import deploy

    assert "platform_control.py" in deploy.BUNDLE_FILES
    assert "platform_control.py" in apply_release.TOOL_FILES


def test_platform_control_script_round_trip(capsys: Any) -> None:
    import json

    import platform_control

    store, clock = MemoryControlsStore(), Clock()
    controls = controls_over(store, clock)
    runtime = SimpleNamespace(
        controls_store=store,
        platform_controls=controls,
        operator_audit=OperatorAudit(store),
    )
    base = ["--operator", "op@example.test", "--reason", "drill"]
    assert platform_control.run(["set", *base, "--read-only", "true"], runtime) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["overrides"]["read_only"] is True and out["version"] == 1
    assert platform_control.run(["status"], runtime) == 0
    capsys.readouterr()
    assert platform_control.run(["clear", *base], runtime) == 0
    assert json.loads(capsys.readouterr().out)["overrides"]["read_only"] is None
    assert platform_control.run(["set", *base], runtime) == 2  # nothing chosen
    refused = store.list_actions(1)[0]
    assert refused.result == "refused" and refused.command == "platform.set"
    assert platform_control.run(["actions", "--limit", "2"], runtime) == 0
