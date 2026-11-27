"""The in-memory purge guard: try-only, per organization, released on error."""

import pytest

from modules.auth.organization_purge_lock import MemoryPurgeLock


def test_second_holder_is_refused_without_waiting():
    lock = MemoryPurgeLock()
    with lock.hold("a") as first:
        assert first is True
        with lock.hold("a") as second:
            assert second is False
        with lock.hold("b") as other:
            assert other is True
    with lock.hold("a") as again:
        assert again is True


def test_released_when_the_body_raises():
    lock = MemoryPurgeLock()
    with pytest.raises(RuntimeError), lock.hold("a"):
        raise RuntimeError("boom")
    with lock.hold("a") as acquired:
        assert acquired is True


def test_a_refused_holder_does_not_release_the_owners_lock():
    lock = MemoryPurgeLock()
    with lock.hold("a"):
        with lock.hold("a"):
            pass
        with lock.hold("a") as still_refused:
            assert still_refused is False
