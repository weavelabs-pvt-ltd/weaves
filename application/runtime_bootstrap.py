"""Seed and initialize runtime-owned platform defaults."""

from __future__ import annotations

from application.runtime_host import RuntimeCapabilityHost
from shared.clock import utc_now as _now


class RuntimeBootstrapMixin(RuntimeCapabilityHost):
    """Own runtime initialization and built-in seed records."""

    def bootstrap(self) -> None:
        """Create a deterministic local org, workspace, model, plugin, and agent."""
        if self._bootstrapped:
            return
        now = _now()
        self.identity_bootstrap_service.seed_local_organization(now)
        self.knowledge_bootstrap_service.seed_local_handbook(now)
        self.identity_bootstrap_service.seed_local_developer(now)
        self.model_bootstrap_service.seed(now)
        self.integration_bootstrap_service.seed(now)
        self.agent_bootstrap_service.seed_local_assistant(now)
        self.model_bootstrap_service.repair_profile_allowlists()
        self.recover_stale_runs()
        self.recover_stale_execution_jobs()
        self.recover_stale_action_invocations()
        self._bootstrapped = True
