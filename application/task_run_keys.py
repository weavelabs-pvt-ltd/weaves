"""The run idempotency key a task turn gives its agent run.

Task turn recovery and the usage reconciler both need to get from a run back to
its turn, so the key format lives in one place.
"""

import hashlib

_PREFIX = "task:"
_MIDDLE = ":run:"


def task_run_key(turn_id: str, attempts: int) -> str:
    """Return the run idempotency key for one attempt of a task turn."""
    return f"{_PREFIX}{turn_id}{_MIDDLE}{attempts}"


def task_turn_id_from_run_key(key: str | None) -> str | None:
    """Return the turn a run key belongs to, or None for any other run."""
    if key is None or not key.startswith(_PREFIX) or _MIDDLE not in key:
        return None
    turn_id = key[len(_PREFIX) : key.rindex(_MIDDLE)]
    return turn_id or None


PLANNER_KEY_SUFFIX = ":plan"
_MAX_KEY = 128


def planner_usage_key(run_key: str) -> str:
    """The usage ledger key of a turn's Ask planner call: ``{run key}:plan``.

    The planner runs before the agent run exists, so the turn's run key (which
    the agent run's workflow run later carries as its idempotency key) is the
    join. A key too long for the ledger is shortened with a digest, never cut.
    """
    key = run_key + PLANNER_KEY_SUFFIX
    if len(key) <= _MAX_KEY:
        return key
    digest = hashlib.sha256(run_key.encode()).hexdigest()[:16]
    head = run_key[: _MAX_KEY - len(PLANNER_KEY_SUFFIX) - 17]
    return f"{head}~{digest}{PLANNER_KEY_SUFFIX}"
