"""Connector health snapshots (``slack_connector_health``) and the refresh tick.

A snapshot is a cache of the last connector check: a status and a reason code,
never a token or data. The ``slack_health`` worker tick rewrites every
organization's snapshots every ten minutes; the pipeline reads them and only
re-probes live (3 s) a tool the planner actually needs whose snapshot is stale or
errored. The probe is the connector's own health check, run as a service identity
(``probe_plugin_installation``), so it needs no admin permission and leaves no
installation or audit side effect.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from application.ports.repository import Repository
from application.ports.slack import SlackInstallation, SlackStore
from contracts.v1 import PluginInstallation, PluginInstallationStatus
from contracts.v1.slack_access import SlackConnectorHealth
from modules.slack.pipeline.deadline import DeadlineExceeded, run_with_deadline
from shared.clock import utc_now
from shared.metrics import SLACK_HEALTH_REFRESH
from shared.request_context import reset_request_principal, set_request_identity

logger = logging.getLogger("product.slack.health")

SNAPSHOT_TTL = timedelta(minutes=15)
LIVE_PROBE_SECONDS = 3.0
TICK_PROBE_SECONDS = 10.0
TICK_BUDGET_SECONDS = 60.0
MAX_PROBES_PER_ORG = 50
HEALTH_PRINCIPAL = "slack-health"

Probe = Callable[[str], tuple[str, str]]


class HealthSnapshots:
    """Read, write and refresh the snapshot of one connector installation."""

    def __init__(
        self,
        repository: Repository[SlackConnectorHealth],
        probe: Probe,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the snapshot collection, the connector probe and the clock."""
        self._repo = repository
        self._probe = probe
        self._clock = clock

    def read(self, org_id: str, installation_id: str) -> SlackConnectorHealth | None:
        """The stored snapshot of an installation, if any."""
        try:
            return self._repo.get_scoped(installation_id, org_id)
        except KeyError:
            return None

    def write(
        self, org_id: str, installation_id: str, status: str, reason: str
    ) -> SlackConnectorHealth:
        """Store the latest status (one row per installation)."""
        now = self._clock()
        current = self.read(org_id, installation_id)
        record = SlackConnectorHealth(
            id=installation_id,
            org_id=org_id,
            installation_id=installation_id,
            status=status,  # type: ignore[arg-type]
            reason_code=reason[:64],
            checked_at=now,
            created_at=current.created_at if current else now,
            updated_at=now,
        )
        SLACK_HEALTH_REFRESH.inc(status=status)
        return self._repo.put(record)

    def probe_and_store(
        self, org_id: str, installation_id: str, *, timeout: float
    ) -> tuple[str, str]:
        """Check the connector now (within ``timeout``) and store the result."""
        try:
            status, reason = run_with_deadline(
                lambda: self._probe(installation_id), timeout
            )
        except DeadlineExceeded:
            status, reason = "error", "probe_timeout"
        self.write(org_id, installation_id, status, reason)
        return status, reason


@contextmanager
def _tenant(installation: SlackInstallation) -> Iterator[None]:
    token = set_request_identity(
        HEALTH_PRINCIPAL, installation.org_id, installation.workspace_id
    )
    try:
        yield
    finally:
        reset_request_principal(token)


class SlackHealthRefresher:
    """The ``slack_health`` tick: refresh the oldest snapshots within a budget."""

    def __init__(
        self,
        *,
        store: SlackStore,
        installations: Repository[PluginInstallation],
        snapshots: HealthSnapshots,
        enabled: Callable[[], bool],
        budget_seconds: float = TICK_BUDGET_SECONDS,
    ) -> None:
        """Bind the Slack store (to find organizations) and the snapshot cache."""
        self._store = store
        self._installations = installations
        self._snapshots = snapshots
        self._enabled = enabled
        self._budget = budget_seconds

    def refresh_all(self) -> int:
        """Refresh snapshots across organizations; returns how many were written."""
        if not self._enabled():
            return 0
        deadline = time.monotonic() + self._budget
        queue: list[tuple[datetime | None, SlackInstallation, str]] = []
        for held in self._store.list_held():
            with _tenant(held):
                queue.extend(self._work_of(held))
        queue.sort(key=lambda item: (item[0] is not None, item[0] or utc_now()))
        written = 0
        for _, held, installation_id in queue:
            if time.monotonic() >= deadline:
                break
            try:
                with _tenant(held):
                    self._refresh_one(held.org_id, held.workspace_id, installation_id)
                written += 1
            except Exception:
                logger.exception("connector health refresh failed")
        return written

    def _work_of(
        self, held: SlackInstallation
    ) -> list[tuple[datetime | None, SlackInstallation, str]]:
        items = []
        rows = self._installations.list_scoped(held.org_id, held.workspace_id)
        for row in rows[:MAX_PROBES_PER_ORG]:
            snapshot = self._snapshots.read(held.org_id, row.plugin_installation_id)
            items.append(
                (
                    snapshot.checked_at if snapshot else None,
                    held,
                    row.plugin_installation_id,
                )
            )
        return items

    def _refresh_one(
        self, org_id: str, workspace_id: str, installation_id: str
    ) -> None:
        row = self._installations.get_scoped(installation_id, org_id, workspace_id)
        if row.status is PluginInstallationStatus.DISABLED:
            self._snapshots.write(org_id, installation_id, "disabled", "")
            return
        self._snapshots.probe_and_store(
            org_id, installation_id, timeout=TICK_PROBE_SECONDS
        )
