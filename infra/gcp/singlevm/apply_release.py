"""Apply one immutable image on the VM without resetting data or credentials."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from release_ledger import (
    POLICY,
    ensure_ledger,
    image_schema_version,
    record_release,
    refuse_unsafe_redeploy,
)
from release_runtime import (
    CONFIG_FILES,
    atomic_write,
    candidate_env,
    compose,
    health_check,
    prepare_tombstones,
    release_lock,
    run,
    validate_image,
)


def validate_config(command: list[str]) -> None:
    """Reject accidental changes to the persistent storage layout."""
    config = json.loads(run([*command, "config", "--format", "json"]))
    volumes = config["volumes"]
    expected = {"database", "certificates", "gateway_config"}
    if set(volumes) != expected:
        raise ValueError(
            "Persistent volume layout cannot change during an image release"
        )
    if any(volumes[key]["name"] != f"weaves-single_{key}" for key in expected):
        raise ValueError("Persistent volume names must remain unchanged")
    mounts = config["services"]["postgres"]["volumes"]
    if not any(m.get("source") == "database" for m in mounts):
        raise ValueError("PostgreSQL must keep its existing database volume")


def activate(root: Path, component: str, gateway_changed: bool = False) -> None:
    """Start the selected services with health checks and bounded worker shutdown."""
    command = compose(root, root / "runtime.env")
    if component == "backend":
        run([*command, "stop", "--timeout", "300", "product-worker"])
    services = (
        ["product-api", "product-worker"] if component == "backend" else ["frontend"]
    )
    run(
        [
            *command,
            "up",
            "-d",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "180",
            *services,
        ]
    )
    if gateway_changed:
        run([*command, "up", "-d", "--no-deps", "--force-recreate", "gateway"])
    health_check(component)


# Files installed on the VM from a backend release bundle after it is healthy.
TOOL_FILES = (
    "apply_release.py",
    "release_runtime.py",
    "release_ledger.py",
    "rollback_release.py",
    "restore_reconcile.py",
    "platform_control.py",
    POLICY,
)


def apply_locked(
    root: Path,
    component: str,
    image: str,
    revision: str,
    bundle: Path | None,
    action: str = "deploy",
) -> None:
    """Validate and pull first, then replace configuration with rollback on failure."""
    ensure_ledger(root)
    # Refused before any pull or activation, like an unsafe redeploy: nothing was
    # attempted, so no ledger row is written (the ledger records attempts).
    prepare_tombstones(root)
    refuse_unsafe_redeploy(root, component, image, (bundle or root) / POLICY)
    env_path = root / "runtime.env"
    previous = env_path.read_text()
    configs = {name: (root / name).read_text() for name in CONFIG_FILES}
    updated = candidate_env(previous, component, image)
    incoming = (
        {name: (bundle / name).read_text() for name in CONFIG_FILES}
        if bundle
        else configs
    )
    gateway_changed = configs["Caddyfile"] != incoming["Caddyfile"]
    with tempfile.TemporaryDirectory(prefix="release-", dir=root) as temporary:
        stage = Path(temporary)
        atomic_write(stage / "runtime.env", updated)
        atomic_write(stage / "compose.yaml", incoming["compose.yaml"])
        command = compose(root, stage / "runtime.env", stage / "compose.yaml")
        validate_config(command)
        services = (
            ["product-api", "product-worker"]
            if component == "backend"
            else ["frontend"]
        )
        run([*command, "pull", *services])
        if gateway_changed:
            atomic_write(stage / "Caddyfile", incoming["Caddyfile"])
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--volume",
                    f"{stage}/Caddyfile:/etc/caddy/Caddyfile:ro",
                    "caddy:2-alpine",
                    "caddy",
                    "validate",
                    "--config",
                    "/etc/caddy/Caddyfile",
                ]
            )
        schema = image_schema_version(image) if component == "backend" else None
        if component == "backend":
            run(["bash", str(root / "backup.sh")])
        try:
            for name, content in incoming.items():
                atomic_write(root / name, content)
            atomic_write(env_path, updated)
            activate(root, component, gateway_changed)
        except Exception:
            record_release(root, component, image, revision, schema, "failed", action)
            for name, content in configs.items():
                atomic_write(root / name, content)
            atomic_write(env_path, previous)
            activate(root, component, gateway_changed)
            raise
    if bundle:
        for name in TOOL_FILES:
            if (bundle / name).exists():
                atomic_write(root / name, (bundle / name).read_text())
    record_release(root, component, image, revision, schema, "healthy", action)
    record = root / "releases.json"
    releases = json.loads(record.read_text()) if record.exists() else {}
    releases[component] = {"image": image, "revision": revision}
    atomic_write(record, json.dumps(releases, indent=2) + "\n")
    print(f"Healthy {component} release: {revision}", flush=True)


def deploy(
    root: Path,
    component: str,
    image: str,
    revision: str,
    token: str,
    bundle: Path | None = None,
    action: str = "deploy",
) -> None:
    """Authenticate transiently and hold one cross-repository lock through readiness."""
    validate_image(component, image)
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not token:
        raise ValueError("A commit SHA and registry access token are required")
    if component != "backend" and bundle:
        raise ValueError("Only backend releases can update server configuration")
    with release_lock(root), tempfile.TemporaryDirectory(prefix="docker-auth-") as auth:
        previous_auth = os.environ.get("DOCKER_CONFIG")
        os.environ["DOCKER_CONFIG"] = auth
        try:
            run(
                [
                    "docker",
                    "login",
                    "asia-south1-docker.pkg.dev",
                    "--username",
                    "oauth2accesstoken",
                    "--password-stdin",
                ],
                token,
            )
            apply_locked(root, component, image, revision, bundle, action)
        finally:
            if previous_auth is None:
                os.environ.pop("DOCKER_CONFIG", None)
            else:
                os.environ["DOCKER_CONFIG"] = previous_auth


def main() -> None:
    """Read the registry token from stdin so it never appears in process arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", required=True, choices=("backend", "frontend"))
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    deploy(
        Path("/opt/weaves/singlevm"),
        args.component,
        args.image,
        args.revision,
        sys.stdin.read().strip(),
        args.bundle,
    )


if __name__ == "__main__":
    main()
