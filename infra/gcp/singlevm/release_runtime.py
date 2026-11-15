"""Host-side utilities for locked, reversible single-VM image releases."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

CONFIG_FILES = ("compose.yaml", "Caddyfile", "backup.sh")


def run(command: list[str], payload: str | None = None) -> str:
    """Capture output privately; deployment failures never echo runtime secrets."""
    result = subprocess.run(command, input=payload, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            f"Deployment command failed: {command[0]} ({result.returncode})"
        )
    return result.stdout


def atomic_write(path: Path, content: str) -> None:
    """Replace a root-only file without exposing partially written credentials."""
    temporary = path.with_suffix(path.suffix + ".next")
    temporary.write_text(content)
    temporary.chmod(0o600)
    os.replace(temporary, path)


@contextmanager
def release_lock(root: Path):
    """Serialize both repositories on the VM, including rollback and readiness."""
    with (root / "deploy.lock").open("a") as lock:
        deadline = time.monotonic() + 600
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Another release still holds the VM lock"
                    ) from None
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def candidate_env(previous: str, component: str, image: str) -> str:
    """Change only this release's image reference, preserving all other bytes."""
    variable = "BACKEND_IMAGE" if component == "backend" else "FRONTEND_IMAGE"
    updated, count = re.subn(
        rf"^{variable}=.*$", f"{variable}={image}", previous, flags=re.M
    )
    if count != 1:
        raise ValueError(f"Expected exactly one {variable} in runtime.env")
    return updated


def validate_image(component: str, image: str) -> None:
    """Limit releases to immutable images in this installation's registry."""
    prefix = "asia-south1-docker.pkg.dev/weaves-510819/weaves/"
    if not re.fullmatch(re.escape(prefix + component) + r"@sha256:[0-9a-f]{64}", image):
        raise ValueError("Expected the component's Artifact Registry image digest")


def health_check(component: str) -> None:
    """Wait for local HTTP readiness without accessing or creating user data."""
    port, path = (
        (8001, "/api/v0/health") if component == "backend" else (8080, "/healthz")
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(2)
    raise RuntimeError(f"{component} did not become healthy")


def compose(root: Path, env: Path, config: Path | None = None) -> list[str]:
    """Always use the existing project and its persistent database volume."""
    return [
        "docker",
        "compose",
        "--project-name",
        "weaves-single",
        "--project-directory",
        str(root),
        "--env-file",
        str(env),
        "--file",
        str(config or root / "compose.yaml"),
    ]
