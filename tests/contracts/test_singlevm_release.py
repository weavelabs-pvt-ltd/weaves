"""Release invariants: shared locking, preserved credentials and image rollback."""

from __future__ import annotations

import json
import multiprocessing
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra/gcp/singlevm"))
import apply_release as release
from apply_release import activate as activate_services
from release_runtime import release_lock, validate_image

OLD = (
    "BACKEND_IMAGE=old-backend\nFRONTEND_IMAGE=old-frontend\n"
    "PASSWORD='keep these bytes'\n"
)
PREFIX = "asia-south1-docker.pkg.dev/weaves-510819/weaves/"
SHA = "1" * 40


@pytest.fixture
def host(tmp_path, monkeypatch):
    (tmp_path / "runtime.env").write_text(OLD)
    for name in release.CONFIG_FILES:
        (tmp_path / name).write_text("original " + name)
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
    return tmp_path


def _concurrent_release(root, component):
    with release_lock(root):
        time.sleep(0.05)
        release.apply_locked(
            root, component, PREFIX + component + "@sha256:" + "a" * 64, SHA, None
        )


def test_simultaneous_repositories_preserve_both_images_and_credentials(host):
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_concurrent_release, args=(host, component))
        for component in ("backend", "frontend")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    current = (host / "runtime.env").read_text()
    assert f"BACKEND_IMAGE={PREFIX}backend@sha256:" in current
    assert f"FRONTEND_IMAGE={PREFIX}frontend@sha256:" in current
    assert "PASSWORD='keep these bytes'\n" in current
    assert set(json.loads((host / "releases.json").read_text())) == {
        "backend",
        "frontend",
    }
    assert (host / "runtime.env").stat().st_mode & 0o777 == 0o600


def test_pull_failure_never_changes_running_configuration(host, monkeypatch):
    original_run = release.run

    def fail_pull(command):
        if "pull" in command:
            raise RuntimeError("registry unavailable")
        return original_run(command)

    monkeypatch.setattr(release, "run", fail_pull)
    with pytest.raises(RuntimeError, match="registry unavailable"):
        release.apply_locked(host, "frontend", "new", SHA, None)
    assert (host / "runtime.env").read_text() == OLD
    assert not (host / "releases.json").exists()


def test_unhealthy_release_rolls_back_images_and_server_config(
    host, monkeypatch, tmp_path
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in release.CONFIG_FILES:
        (bundle / name).write_text("new " + name)
    attempts = []

    def activate(root, component, gateway_changed):
        attempts.append((root / "runtime.env").read_text())
        assert component == "backend" and gateway_changed
        if len(attempts) == 1:
            raise RuntimeError("readiness failed")

    monkeypatch.setattr(release, "activate", activate)
    with pytest.raises(RuntimeError, match="readiness failed"):
        release.apply_locked(host, "backend", "new", SHA, bundle)
    assert len(attempts) == 2 and attempts[1] == OLD
    assert (host / "runtime.env").read_text() == OLD
    assert (host / "compose.yaml").read_text() == "original compose.yaml"
    assert not (host / "releases.json").exists()


def test_frontend_release_never_stops_backend_or_database(host, monkeypatch):
    commands = []
    monkeypatch.setattr(release, "run", lambda command: commands.append(command))
    monkeypatch.setattr(release, "health_check", lambda component: None)
    activate_services(host, "frontend")
    assert len(commands) == 1
    assert commands[0][-1] == "frontend"
    assert "--no-deps" in commands[0]
    assert not {"stop", "down", "postgres", "product-api", "product-worker"} & set(
        commands[0]
    )


@pytest.mark.parametrize(
    "image",
    [
        PREFIX + "frontend:latest",
        PREFIX + "backend@sha256:" + "a" * 64,
        "other/frontend@sha256:" + "a" * 64,
    ],
)
def test_release_rejects_mutable_or_wrong_component_images(image):
    with pytest.raises(ValueError):
        validate_image("frontend", image)


def test_storage_layout_changes_are_rejected(host, monkeypatch):
    monkeypatch.setattr(release, "run", lambda command: json.dumps({"volumes": {}}))
    with pytest.raises(ValueError, match="volume layout"):
        release.apply_locked(host, "backend", "new", SHA, None)
    assert (host / "runtime.env").read_text() == OLD
