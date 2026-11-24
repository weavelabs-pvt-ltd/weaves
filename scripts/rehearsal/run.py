"""Upgrade to the candidate, roll back to the minimum revision, roll forward again."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rehearsal.manifest import VERSION
from rehearsal.stack import RehearsalError, Stack, docker
from rehearsal.tenants import (
    Tenant,
    check_isolation,
    issue_session,
    provision,
    verify_records,
    write_records,
)

HEAD_CODE = (
    "from infrastructure.database.store import SCHEMA_HEAD_VERSION as v;print(v)"
)
SETTLE_SECONDS = 6


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    if done.returncode:
        raise RehearsalError(f"git {args[0]} failed")
    return done.stdout.strip()


def build_from_revision(root: Path, revision: str, tag: str) -> None:
    """Build the image the old revision would have produced, from its own files."""
    archive = subprocess.Popen(
        ["git", "-C", str(root), "archive", revision], stdout=subprocess.PIPE
    )
    build = subprocess.run(
        ["docker", "build", "-q", "-t", tag, "-"],
        stdin=archive.stdout,
        capture_output=True,
    )
    archive.wait()
    if archive.returncode or build.returncode:
        raise RehearsalError(f"could not build an image from revision {revision[:12]}")


def describe_image(image: str, revision: str, source: str) -> dict[str, Any]:
    """Identity and migration head of one image; the head comes from the image."""
    meta = docker("image", "inspect", "-f", "{{.Id}} {{json .RepoDigests}}", image)
    image_id, _, digests = meta.stdout.strip().partition(" ")
    head = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "python",
        image,
        "-c",
        HEAD_CODE,
    )
    return {
        "revision": revision,
        "source": source,
        "image_id": image_id,
        "repo_digests": digests,
        "schema_head": int(head.stdout.strip()),
    }


class Rehearsal:
    """Runs the phases and records one pass/fail step per assertion."""

    def __init__(self, stack: Stack, old: str, new: str, scale: int) -> None:
        """Bind the stack, the two image tags and the data volume per phase."""
        self.stack, self.old, self.new, self.scale = stack, old, new, scale
        self.steps: list[dict[str, Any]] = []
        self.tenants = [
            Tenant("alpha", "owner@alpha-rehearsal.test"),
            Tenant("beta", "owner@beta-rehearsal.test"),
        ]

    def step(self, name: str, check: Callable[[], list[str] | None]) -> None:
        """Record a step; a failure is kept (and later phases still run to report)."""
        started = time.monotonic()
        try:
            problems = check() or []
        except RehearsalError as error:
            problems = [str(error)]
        self.steps.append(
            {
                "name": name,
                "ok": not problems,
                "detail": "; ".join(problems),
                "seconds": round(time.monotonic() - started, 1),
            }
        )

    def _check_all(self) -> list[str]:
        problems: list[str] = []
        for tenant in self.tenants:
            problems += verify_records(self.stack, "api", tenant)
            problems += check_isolation(
                self.stack, tenant, [t for t in self.tenants if t is not tenant]
            )
        return problems

    def _boot(self, image: str) -> None:
        self.stack.start("api", image)
        self.stack.start("worker", image)

    def _settled(self) -> list[str]:
        time.sleep(SETTLE_SECONDS)
        return [
            f"{role} exited: {self.stack.tail(role)}"
            for role in ("api", "worker")
            if not self.stack.is_running(role)
        ]

    def _retire(self) -> None:
        self.stack.stop("worker")
        self.stack.stop("api")

    def _write(self, prefix: str) -> list[str]:
        for tenant in self.tenants:
            write_records(self.stack, "api", tenant, prefix, self.scale)
        return []

    def phase_old_seed(self) -> None:
        """Run one phase of the rehearsal."""
        self.stack.start("api", self.old)
        for tenant in self.tenants:
            provision(self.stack, "api", tenant)
        self.step("old image seeds production-shaped data", lambda: self._write("seed"))
        self.stack.start("worker", self.old)
        self.step("old image is stable with data", self._settled)
        self._retire()

    def phase_upgrade(self, expected_head: int) -> None:
        """Run one phase of the rehearsal."""
        self._boot(self.new)
        self.step("upgrade: candidate migrates and reads old data", self._check_all)
        self.step("upgrade: candidate writes new data", lambda: self._write("upgraded"))
        self.step("upgrade: candidate stack is stable", self._settled)
        self.step(
            "upgrade: database is at the candidate head",
            lambda: (
                [] if self.stack.schema_head() == expected_head else ["head mismatch"]
            ),
        )
        self._retire()

    def phase_rollback(self) -> None:
        """Run one phase of the rehearsal."""
        self._boot(self.old)
        self.step("rollback: old image reads old and new data", self._check_all)
        self.step(
            "rollback: old image writes on the newer schema",
            lambda: self._write("rolled-back"),
        )
        self.step("rollback: old image issues sessions", self._fresh_sessions)
        self.step("rollback: old image is stable", self._settled)
        self.step("rollback: still readable after writes", self._check_all)
        self._retire()

    def _fresh_sessions(self) -> list[str]:
        for tenant in self.tenants:
            tenant.token = issue_session(self.stack, "api", tenant)
        return []

    def phase_roll_forward(self) -> None:
        """Run one phase of the rehearsal."""
        self._boot(self.new)
        self.step(
            "roll forward: candidate reads data the old image wrote", self._check_all
        )
        self.step("roll forward: candidate is stable", self._settled)
        self._retire()


def rehearse(
    root: Path,
    old: dict[str, Any],
    new: dict[str, Any],
    old_tag: str,
    new_tag: str,
    scale: int,
) -> dict[str, Any]:
    """Run all phases on a fresh disposable stack and return the manifest body."""
    started = datetime.now(UTC)
    clock = time.monotonic()
    with Stack() as stack:
        run = Rehearsal(stack, old_tag, new_tag, scale)
        try:
            run.phase_old_seed()
            run.phase_upgrade(new["schema_head"])
            run.phase_rollback()
            run.phase_roll_forward()
            head_after = stack.schema_head()
        except RehearsalError as error:  # a phase could not even start
            run.steps.append(
                {"name": "rehearsal aborted", "ok": False, "detail": str(error)}
            )
            head_after = -1
    return {
        "version": VERSION,
        "result": "passed" if all(s["ok"] for s in run.steps) else "failed",
        "started": started.isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - clock),
        "candidate": new,
        "rollback_floor": old,
        "database_head_after_upgrade": head_after,
        "scale": {"tenants": len(run.tenants), "agents_per_phase": scale},
        "steps": run.steps,
    }
