"""A disposable PostgreSQL plus API and worker, swappable between image versions.

Everything is local Docker: a private network, a throwaway database with a
generated password and a generated encryption key. Nothing is published except
the API on a random loopback port, and no cloud or registry resource is touched.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Self

HERE = Path(__file__).resolve().parent
OPERATOR_DIR = HERE.parents[1] / "infra/gcp/singlevm"
POSTGRES_IMAGE = "postgres:16-alpine"
CUSTOM_BASE_URL = "https://models.rehearsal.test/v1"


class RehearsalError(RuntimeError):
    """A rehearsal step failed; the message never contains secrets."""


def docker(
    *args: str, payload: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run one Docker CLI command, capturing output and hiding the command line."""
    result = subprocess.run(
        ["docker", *args], input=payload, text=True, capture_output=True
    )
    if check and result.returncode:
        detail = result.stderr.strip().splitlines()[-1:] or [""]
        raise RehearsalError(f"docker {args[0]} failed ({result.returncode}): {detail}")
    return result


class Stack:
    """One rehearsal environment; ``with`` removes every container and network."""

    def __init__(self, scale_name: str | None = None) -> None:
        """Generate a private name, database password and encryption key."""
        self.name = scale_name or "rehearsal-" + secrets.token_hex(4)
        self.password = secrets.token_hex(16)
        self.key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        self.api_port = 0
        self._running: dict[str, str] = {}
        self.extra_env: dict[str, str] = {}
        self.run_args: list[str] = []

    def __enter__(self) -> Self:
        """Create the network and database."""
        docker("network", "create", self.name)
        self._run_postgres()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Remove every container and the network."""
        for role in list(self._running):
            docker("rm", "-f", f"{self.name}-{role}", check=False)
        docker("rm", "-f", "-v", f"{self.name}-pg", check=False)
        docker("network", "rm", self.name, check=False)

    def _environment(self) -> Mapping[str, str]:
        return {
            "DATABASE_URL": f"postgresql://weaves:{self.password}@postgres:5432/weaves",
            "WEAVES_SECRET_ENCRYPTION_KEY": self.key,
            "WEAVES_SEED_LOCAL_DEFAULTS": "false",
            "WEAVES_DB_POOL_MAX_SIZE": "4",
            "WEAVES_MODEL_CUSTOM_BASE_URLS": json.dumps([CUSTOM_BASE_URL]),
            "WEAVES_WORKER_POLL_SECONDS": "1",
            **self.extra_env,
        }

    def run_flags(self) -> list[str]:
        """``docker run`` flags (environment and extra mounts) for any one-off."""
        env = [f"--env={k}={v}" for k, v in self._environment().items()]
        return [*env, *self.run_args]

    def reset_database(self) -> None:
        """Destroy the database container and start an empty one (a lost disk)."""
        docker("rm", "-f", "-v", f"{self.name}-pg", check=False)
        self._run_postgres()

    def _run_postgres(self) -> None:
        env = {
            "POSTGRES_DB": "weaves",
            "POSTGRES_USER": "weaves",
            "POSTGRES_PASSWORD": self.password,
        }
        flags = [f"--env={k}={v}" for k, v in env.items()]
        docker(
            "run", "-d", "--name", f"{self.name}-pg", "--network", self.name,
            "--network-alias", "postgres", *flags, POSTGRES_IMAGE,
        )  # fmt: skip
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = docker(
                "exec", f"{self.name}-pg", "pg_isready", "-U", "weaves", check=False
            )
            if ready.returncode == 0 and self._sql("select 1", check=False) == "1":
                return
            time.sleep(1)
        raise RehearsalError("PostgreSQL did not become ready")

    def _sql(self, statement: str, check: bool = True) -> str:
        result = docker(
            "exec", f"{self.name}-pg", "psql", "-U", "weaves", "-d", "weaves",
            "-tAc", statement, check=check,
        )  # fmt: skip
        return result.stdout.strip()

    def schema_head(self) -> int:
        """The newest migration the database has recorded."""
        return int(self._sql("select max(version) from product_schema_migrations"))

    def start(self, role: str, image: str) -> None:
        """Start ``api`` or ``worker`` from ``image`` (migrations run on API start)."""
        command = {
            "api": [
                "uvicorn",
                "apps.api.app:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
            "worker": ["python", "-m", "apps.worker.main"],
        }[role]
        publish = ["--publish", "127.0.0.1::8000"] if role == "api" else []
        docker(
            "run", "-d", "--name", f"{self.name}-{role}", "--network", self.name,
            *self.run_flags(), *publish, image, *command,
        )  # fmt: skip
        self._running[role] = image
        if role == "api":
            self._wait_healthy()

    def stop(self, role: str) -> None:
        """Stop and remove one process, keeping the database."""
        docker("rm", "-f", f"{self.name}-{role}", check=False)
        self._running.pop(role, None)

    def _wait_healthy(self) -> None:
        mapping = docker("port", f"{self.name}-api", "8000/tcp").stdout.split()[-1]
        self.api_port = int(mapping.rsplit(":", 1)[1])
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                url = f"http://127.0.0.1:{self.api_port}/api/v0/health"
                with urllib.request.urlopen(url, timeout=3) as response:
                    if json.load(response) == {"status": "ok", "storage": "postgres"}:
                        return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            if (
                docker(
                    "inspect", "-f", "{{.State.Running}}", f"{self.name}-api"
                ).stdout.strip()
                != "true"
            ):
                raise RehearsalError("The API container exited: " + self.tail("api"))
            time.sleep(1)
        raise RehearsalError("The API did not become healthy: " + self.tail("api"))

    def tail(self, role: str, lines: int = 5) -> str:
        """The last log lines, for failure messages (the stack carries no secrets)."""
        out = docker("logs", "--tail", str(lines), f"{self.name}-{role}", check=False)
        return " | ".join((out.stdout + out.stderr).strip().splitlines()[-lines:])[:600]

    def is_running(self, role: str) -> bool:
        """Whether the process is still up (a crash-looping worker exits)."""
        state = docker(
            "inspect", "-f", "{{.State.Running}}", f"{self.name}-{role}", check=False
        )
        return state.stdout.strip() == "true"

    def exec_script(self, role: str, script: Path, *args: str) -> str:
        """Run a local script inside a running container's image (stdin, stdout)."""
        result = docker(
            "exec", "-i", f"{self.name}-{role}", "python", "-", *args,
            payload=script.read_text(),
        )  # fmt: skip
        return result.stdout.strip()
