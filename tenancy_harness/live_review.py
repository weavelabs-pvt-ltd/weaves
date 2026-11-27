"""Host side of the opt-in live PR review control (never part of CI).

Runs ``phase7.slack_review_live`` in the seed container (one real model call
through the budget relay) and reconciles the relay's settled spend with the product
usage ledger, exactly as the existing live smoke does.
"""

import json
from pathlib import Path

from tenancy_harness.live_host import relay_matches_ledger

OUTPUT = "live-slack-review.json"


def review_flow(compose, private, directory):
    """Run the live review and return its evidence, with the relay reconciliation."""
    compose.run(
        "run",
        "--rm",
        "--no-deps",
        "seed",
        "python",
        "-m",
        "tenancy_harness.phase7.slack_review_live",
    )
    evidence = json.loads(Path(private / OUTPUT).read_text())
    evidence["usage_ledger"] = relay_matches_ledger(evidence, private, directory)
    if not evidence["passed"]:
        raise RuntimeError("Live review verification failed; inspect private evidence")
    return evidence
