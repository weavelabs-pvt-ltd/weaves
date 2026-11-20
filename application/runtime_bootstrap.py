"""Seed and initialize runtime-owned platform defaults."""

from __future__ import annotations

from application.runtime_host import RuntimeCapabilityHost
from shared.clock import utc_now as _now
from shared.request_context import system_scope


class RuntimeBootstrapMixin(RuntimeCapabilityHost):
    """Own runtime initialization and built-in seed records."""

    def bootstrap(self) -> None:
        """Initialize catalogs and optionally create local development examples."""
        if self._bootstrapped:
            return
        with system_scope("bootstrap"):
            self._bootstrap_platform()

    def _bootstrap_platform(self) -> None:
        """Run startup seeding and recovery as the named bootstrap system scope."""
        now = _now()
        if self.seed_local_defaults:
            self.identity_bootstrap_service.seed_local_organization(now)
            self.identity_bootstrap_service.seed_local_developer(now)
        self.identity_bootstrap_service.seed_standard_roles()
        if self.seed_local_defaults:
            self.model_bootstrap_service.seed(now)
        self.integration_bootstrap_service.seed(
            now, seed_local_policy=self.seed_local_defaults
        )
        if self.seed_local_defaults:
            self.agent_bootstrap_service.seed_local_assistant(now)
        self.model_bootstrap_service.repair_profile_allowlists()
        self.recover_stale_runs()
        self.recover_stale_execution_jobs()
        self.recover_stale_action_invocations()
        self._bootstrapped = True
