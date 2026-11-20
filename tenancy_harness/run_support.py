"""Stdlib-only host orchestration; no production configuration is imported."""

import base64
import json
import os
import re
import secrets
import socket
import subprocess


def execute(args, **kwargs):
    """Run argument arrays without shell expansion or inherited database secrets."""
    clean = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("WEAVES_", "DATABASE_", "PGPASSWORD", "POSTGRES_", "MODEL_")
        )
    }
    clean.update(kwargs.pop("env", {}))
    kwargs.setdefault("check", True)
    return subprocess.run(args, env=clean, **kwargs)


def revision(path):
    """Record the exact source revision and whether local worktree changes exist."""
    sha = execute(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    dirty = execute(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {"revision": sha, "dirty": bool(dirty)}


def validate_pair(backend, frontend, candidate, allow_dirty, pair):
    """Refuse floating counterparts and dirty CI inputs while allowing local work."""
    if os.environ.get("CI") and allow_dirty:
        raise RuntimeError("CI cannot bypass source reproducibility checks")
    sources = {"backend": revision(backend), "frontend": revision(frontend)}
    for component, source in sources.items():
        if component != candidate and source["revision"] != pair[component]["revision"]:
            raise RuntimeError(f"{component} counterpart must match pair.json exactly")
        if source["dirty"] and not allow_dirty:
            raise RuntimeError(
                f"{component} has unrecorded work; use --allow-dirty locally"
            )
    return sources


def setup_private(root, harness, project):
    """Create ephemeral secrets, local CA, and Compose's sole environment file."""
    private = root / "private"
    private.mkdir(parents=True, mode=0o700)
    tls = private / "tls"
    tls.mkdir(mode=0o700)
    (private / "mail").mkdir(mode=0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    execute(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=provider",
            "-addext",
            "subjectAltName=DNS:provider,DNS:models.tenancy.test,"
            "DNS:blocked-models.tenancy.test,"
            "DNS:api.anthropic.com,DNS:api.openai.com,"
            "DNS:generativelanguage.googleapis.com",
            "-keyout",
            str(tls / "key.pem"),
            "-out",
            str(tls / "cert.pem"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (tls / "key.pem").chmod(0o600)
    values = {
        "TEST_DB_PASSWORD": secrets.token_hex(24),
        "TEST_ENCRYPTION_KEY": base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode(),
        "TEST_EMAIL_KEY": secrets.token_hex(32),
        "TEST_ONBOARDING_TOKEN": secrets.token_hex(32),
        "TEST_HTTP_PORT": str(port),
        "TEST_UID": str(os.getuid()),
        "TEST_GID": str(os.getgid()),
        "TEST_PRIVATE_DIR": str(private),
        "TEST_HARNESS_DIR": str(harness),
        "TEST_BACKEND_IMAGE": project + "-backend",
        "TEST_FRONTEND_IMAGE": project + "-frontend",
    }
    envfile = private / "compose.env"
    envfile.write_text("\n".join(k + "=" + json.dumps(v) for k, v in values.items()))
    envfile.chmod(0o600)
    return private, envfile, port, values


class Compose:
    """Limit container and volume operations to a random disposable project."""

    def __init__(self, project, harness, envfile, live=False):
        """Reject names that could collide with the actual deployment."""
        if not re.fullmatch(r"weaves-tenancy-[a-f0-9]{12}", project):
            raise ValueError("Unsafe tenancy project name")
        self.base = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(envfile),
            "-f",
            str(harness / "compose.yml"),
        ]
        if live:
            self.base.extend(["-f", str(harness / "live.compose.yml")])

    def run(self, *args, **kwargs):
        """Run an isolated Compose command with bounded diagnostic output."""
        return execute([*self.base, *args], **kwargs)
