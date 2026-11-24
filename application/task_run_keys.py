"""The run idempotency key a task turn gives its agent run.

Task turn recovery and the usage reconciler both need to get from a run back to
its turn, so the key format lives in one place.
"""

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
