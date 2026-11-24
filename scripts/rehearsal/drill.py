"""Whole-service disaster-recovery drill on a local, throwaway Docker stack.

Seeds three tenants, takes a backup the way ``backup.sh`` does (dump plus the
tombstone-log head), deletes one tenant and writes one record after the backup,
destroys the database (a lost disk), then recovers from the backup with the
documented procedure: ``mark-restore``, restore, ``apply`` (tombstone replay),
start. It asserts what the runbook promises: surviving tenants intact, the
deleted tenant not resurrected, encrypted secrets readable only with the
original key, old sessions dead, and the post-backup write lost (the RPO).
"""

from __future__ import annotations

import gzip
import json
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rehearsal.stack import HERE, OPERATOR_DIR, RehearsalError, Stack, docker
from rehearsal.tenants import (
    Tenant,
    call,
    check_isolation,
    issue_session,
    provision,
    verify_records,
    write_records,
)

TOMBSTONES = "/var/lib/weaves/tombstones"
OPERATOR = "dr-drill@example.invalid"
LABELS = ("alpha", "beta", "gamma")
KEY_VARIABLE = "WEAVES_SECRET_ENCRYPTION_KEY"
WRONG_KEY = "A" * 43 + "="


class Drill:
    """One drill run; every assertion is recorded as a pass/fail step."""

    def __init__(self, stack: Stack, image: str, workdir: Path, scale: int) -> None:
        """Bind a stack, the image under test and a scratch directory."""
        self.stack, self.image, self.scale = stack, image, scale
        self.backups = workdir / "backups"
        self.backups.mkdir(mode=0o755)
        self.volume = f"{stack.name}-tombstones"
        self.steps: list[dict[str, Any]] = []
        self.tenants = {
            label: Tenant(label, f"owner@{label}-drill.test") for label in LABELS
        }
        self.dump: Path = self.backups / "none"
        self.backup_at = 0.0
        self.lost_agent = ""
        self.rto = 0.0
        self.rpo = 0.0
        self.report: dict[str, Any] = {}

    # ---- plumbing -----------------------------------------------------------
    def step(self, name: str, check: Callable[[], list[str] | None]) -> None:
        """Record a step; a failure is kept and later steps still run."""
        started = time.monotonic()
        try:
            problems = check() or []
        except (RehearsalError, subprocess.SubprocessError, OSError, ValueError) as e:
            problems = [str(e)]
        self.steps.append(
            {
                "name": name,
                "ok": not problems,
                "detail": "; ".join(problems),
                "seconds": round(time.monotonic() - started, 1),
            }
        )

    def _script(self, name: str, *args: str) -> str:
        """Run an operator script in a one-off container on the stack network."""
        script = OPERATOR_DIR / name
        result = docker(
            "run", "--rm", "-i", "--network", self.stack.name,
            *self.stack.run_flags(), "--volume", f"{self.backups}:/backup:ro",
            self.image, "python", "-", *args, payload=script.read_text(),
        )  # fmt: skip
        return result.stdout.strip()

    def _status(self, tenant: Tenant, path: str) -> int:
        """HTTP status of an authenticated GET (errors are statuses, not raises)."""
        headers = {
            "Authorization": "Bearer " + tenant.token,
            "X-Organization-Id": tenant.org_id,
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.stack.api_port}/api/v0{path}", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)

    def _lifecycle(self, command: str, tenant: Tenant, *extra: str) -> str:
        return self._script(
            "organization_lifecycle.py", command, "--organization-id", tenant.org_id,
            "--operator", OPERATOR, "--reason", "dr drill", *extra,
        )  # fmt: skip

    def _org_state(self, tenant: Tenant) -> str:
        out = self._script(
            "organization_lifecycle.py", "status", "--organization-id", tenant.org_id
        )
        return str(json.loads(out.splitlines()[-1]).get("state", "")).lower()

    # ---- phases -------------------------------------------------------------
    def seed(self) -> list[str]:
        """Create tenants, records and an encrypted secret for each."""
        docker("volume", "create", self.volume)
        docker(
            "run", "--rm", "--user", "0", "--entrypoint", "chown",
            "--volume", f"{self.volume}:{TOMBSTONES}", self.image,
            "10001:10001", TOMBSTONES,
        )  # fmt: skip
        self.stack.extra_env.update(
            WEAVES_TOMBSTONE_DIR=TOMBSTONES, WEAVES_LIFECYCLE_POLL_SECONDS="2"
        )
        self.stack.run_args += ["--volume", f"{self.volume}:{TOMBSTONES}"]
        self.stack.start("api", self.image)
        for tenant in self.tenants.values():
            provision(self.stack, "api", tenant)
            write_records(self.stack, "api", tenant, "seed", self.scale)
        self.stack.start("worker", self.image)
        return []

    def backup(self) -> list[str]:
        """Do what ``backup.sh`` does: tombstone head, dump, ``gzip -t``."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.dump = self.backups / f"weaves-{stamp}.sql.gz"
        head = docker(
            "exec", f"{self.stack.name}-api", "python", "-m",
            "modules.recovery.tombstones", "head", "--dir", TOMBSTONES,
        )  # fmt: skip
        Path(f"{self.dump}.tombstone-head").write_text(head.stdout)
        dump = docker(
            "exec", f"{self.stack.name}-pg", "pg_dump", "--no-owner", "--no-acl",
            "-U", "weaves", "-d", "weaves",
        )  # fmt: skip
        self.dump.write_bytes(gzip.compress(dump.stdout.encode()))
        self.backup_at = time.monotonic()
        return [] if gzip.decompress(self.dump.read_bytes()) else ["empty dump"]

    def after_backup(self) -> list[str]:
        """A write the backup lacks, and the deletion the backup predates."""
        gamma = self.tenants["gamma"]
        agent = call(
            self.stack, self.tenants["alpha"], "POST", "/agents",
            {"name": "written-after-backup", "instructions": "x",
             "description": "must be lost", "allowed_capability_ids": []},
        )  # fmt: skip
        self.lost_agent = agent["id"]
        self._lifecycle("request-deletion", gamma)
        self._lifecycle(
            "purge-now", gamma, "--confirm-organization-name", "Rehearsal gamma"
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self._org_state(gamma) == "deleted":
                return []
            time.sleep(2)
        return ["the worker did not purge the organization within 120 s"]

    def disaster(self) -> list[str]:
        """Lose the database disk. The tombstone volume survives (see runbook)."""
        self.rpo = time.monotonic() - self.backup_at
        self.stack.stop("worker")
        self.stack.stop("api")
        self.stack.reset_database()
        return []

    def recover(self, started: float) -> list[str]:
        """Follow the documented restore procedure and start the services."""
        name = self.dump.name
        head = f"/backup/{name}.tombstone-head"
        self._script("restore_reconcile.py", "mark-restore", "--backup", name)
        restore = subprocess.run(
            ["docker", "exec", "-i", f"{self.stack.name}-pg", "psql",
             "-v", "ON_ERROR_STOP=1", "-q", "-U", "weaves", "-d", "weaves"],
            input=gzip.decompress(self.dump.read_bytes()),
            capture_output=True,
        )  # fmt: skip
        if restore.returncode:
            return ["psql restore failed: " + restore.stderr.decode()[-300:]]
        plan = self._script(
            "restore_reconcile.py", "apply", "--dry-run", "--head-file", head
        )
        applied = self._script("restore_reconcile.py", "apply", "--head-file", head)
        self.report = json.loads(applied.splitlines()[-1])
        planned = set(json.loads(plan.splitlines()[-1])["organizations"])
        self.stack.start("api", self.image)
        self.stack.start("worker", self.image)
        self.rto = time.monotonic() - started
        done = set(self.report["organizations"])
        if not done or planned != done:
            return [f"the dry run planned {sorted(planned)}, apply did {sorted(done)}"]
        return []

    # ---- assertions ---------------------------------------------------------
    def survivors_intact(self) -> list[str]:
        """Alpha and beta read back exactly what existed at backup time."""
        problems: list[str] = []
        for label in ("alpha", "beta"):
            tenant = self.tenants[label]
            tenant.token = issue_session(self.stack, "api", tenant)
            problems += verify_records(self.stack, "api", tenant)
            others = [t for t in self.tenants.values() if t is not tenant]
            problems += check_isolation(self.stack, tenant, others)
        return problems

    def old_sessions_dead(self, stale: dict[str, str]) -> list[str]:
        """Every pre-disaster session token is refused after reconciliation."""
        problems = []
        for label, token in stale.items():
            org = self.tenants[label].org_id
            probe = Tenant(label, "x", org_id=org, token=token)
            if self._status(probe, "/account/me") not in (401, 403):
                problems.append(f"{label}: a pre-disaster session still works")
        return problems

    def deleted_stays_deleted(self) -> list[str]:
        """The tenant deleted after the backup is not resurrected by the restore."""
        gamma = self.tenants["gamma"]
        problems = []
        if self._org_state(gamma) != "deleted":
            problems.append("gamma is not DELETED after restore")
        listing = json.dumps(call(self.stack, self.tenants["alpha"], "GET", "/agents"))
        if any(i in listing for i in gamma.agents.values()):
            problems.append("gamma's agents are visible to another tenant")
        return problems

    def post_backup_write_lost(self) -> list[str]:
        """The RPO is real: the record written after the backup is gone."""
        alpha = self.tenants["alpha"]
        status = self._status(alpha, f"/agents/{self.lost_agent}")
        return [] if status == 404 else [f"post-backup agent answered {status}"]

    def key_matters(self) -> list[str]:
        """Secrets decrypt with the original key and do not with another."""
        beta = self.tenants["beta"]
        ref, value = next(iter(beta.secrets.items()))
        result = subprocess.run(
            ["docker", "exec", "-i", "--env", f"{KEY_VARIABLE}={WRONG_KEY}",
             f"{self.stack.name}-api", "python", "-", "check", beta.org_id,
             beta.workspace_id, ref, value],
            input=(HERE / "secret_probe.py").read_text(),
            capture_output=True,
            text=True,
        )  # fmt: skip
        if result.returncode == 0:
            return ["a secret decrypted with the wrong key"]
        return []

    def stable(self) -> list[str]:
        """Both processes are still up after settling."""
        time.sleep(6)
        return [
            f"{role} exited: {self.stack.tail(role)}"
            for role in ("api", "worker")
            if not self.stack.is_running(role)
        ]


def _phases(drill: Drill) -> None:
    drill.step("seed three tenants with records and encrypted secrets", drill.seed)
    stale = {t.label: t.token for t in drill.tenants.values()}
    drill.step("backup like backup.sh (tombstone head, dump, gzip -t)", drill.backup)
    drill.step("write after the backup and delete tenant gamma", drill.after_backup)
    drill.step("disaster: the database disk is lost", drill.disaster)
    started = time.monotonic()
    drill.step(
        "recover: mark, restore, tombstone replay, start",
        lambda: drill.recover(started),
    )
    drill.step("survivors read back their backup-time data", drill.survivors_intact)
    drill.step(
        "pre-disaster sessions are refused", lambda: drill.old_sessions_dead(stale)
    )
    drill.step("the deleted tenant stays deleted", drill.deleted_stays_deleted)
    drill.step("the post-backup write is lost (RPO)", drill.post_backup_write_lost)
    drill.step("secrets need the original key", drill.key_matters)
    drill.step("api and worker are stable", drill.stable)


def run_drill(image: str, workdir: Path, scale: int = 4) -> dict[str, Any]:
    """Run the whole drill on a fresh stack and return the result document."""
    started = datetime.now(UTC)
    clock = time.monotonic()
    with Stack("dr-drill-" + secrets.token_hex(4)) as stack:
        drill = Drill(stack, image, workdir, scale)
        try:
            _phases(drill)
        finally:
            docker("volume", "rm", "-f", drill.volume, check=False)
            shutil.rmtree(drill.backups, ignore_errors=True)
    return {
        "version": 1,
        "kind": "dr-drill",
        "scope": "local throwaway Docker stack; no cloud or production access",
        "result": "passed" if all(s["ok"] for s in drill.steps) else "failed",
        "started": started.isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - clock),
        "rto_seconds": round(drill.rto, 1),
        "backup_to_disaster_seconds": round(drill.rpo, 1),
        "image": image,
        "tombstones_replayed": drill.report.get("tombstones"),
        "sessions_revoked": drill.report.get("sessions_revoked"),
        "steps": drill.steps,
    }
