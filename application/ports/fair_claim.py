"""Result of a fair (round-robin, capped) claim of queued work."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from shared.limits_config import LimitsMode

logger = logging.getLogger("product.worker")


@dataclass(frozen=True)
class FairClaim[TContract]:
    """A claimed record plus what the claim saw for the owning organization.

    ``running_before`` counts the organization's running work (jobs and task
    turns together) before this claim; ``cap`` is its running limit. Under
    ``observe`` the claim is never refused, so ``over_cap`` means the claim
    would have been refused under ``enforce``.
    """

    record: TContract
    running_before: int
    cap: int
    mode: LimitsMode

    @property
    def over_cap(self) -> bool:
        """True when this claim exceeds the organization's running cap."""
        return self.running_before >= self.cap


def log_over_cap[TContract](
    kind: str, record_id: str, claim: FairClaim[TContract]
) -> None:
    """In ``observe`` mode, log a claim that ``enforce`` would have deferred."""
    if claim.mode == "observe" and claim.over_cap:
        logger.warning(
            "would defer %s over the running cap job_id=%s running=%d cap=%d",
            kind,
            record_id,
            claim.running_before,
            claim.cap,
        )
