"""Run a blocking call with a hard deadline, keeping the caller's tenant context.

A call that misses its deadline is abandoned, not killed: its thread finishes on
its own (and settles its own usage row), but the pipeline has already moved on.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextvars import copy_context

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="slack-pipeline")


class DeadlineExceeded(Exception):
    """The call did not finish inside its deadline."""


def run_with_deadline[T](call: Callable[[], T], seconds: float) -> T:
    """Return ``call()`` or raise ``DeadlineExceeded`` after ``seconds``."""
    future = _POOL.submit(copy_context().run, call)
    try:
        return future.result(timeout=seconds)
    except FutureTimeout:
        raise DeadlineExceeded from None
