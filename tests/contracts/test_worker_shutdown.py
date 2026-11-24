"""Deploy shutdown finishes the current operation before closing its services."""

import signal
from unittest.mock import MagicMock

from apps.worker import main as worker


def test_sigterm_does_not_pick_up_another_task(monkeypatch):
    # The legacy single loop (fair claiming off, one slot) is unchanged.
    monkeypatch.setenv("WEAVES_FAIR_CLAIM", "off")
    monkeypatch.setenv("WEAVES_WORKER_SLOTS", "1")
    services = MagicMock()
    services.storage_mode = "postgres"
    services.workflow_automation.dispatch_due_workflow_schedules.return_value = []
    finished = []

    def current_operation(_worker_id):
        signal.raise_signal(signal.SIGTERM)
        finished.append(True)
        return None

    services.execution.process_next_execution_job.side_effect = current_operation
    monkeypatch.setattr(worker, "build_worker_services", lambda *_: services)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        worker.main()
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert finished == [True]
    services.tasks.process_next_turn.assert_not_called()
    services.close.assert_called_once()


def test_sigterm_lets_every_slot_finish_and_closes_services(monkeypatch):
    """Supervised slots: in-flight work completes, nothing new is claimed."""
    import threading

    monkeypatch.setenv("WEAVES_WORKER_SLOTS", "3")
    monkeypatch.setenv("WEAVES_DB_POOL_MAX_SIZE", "12")
    services = MagicMock()
    services.storage_mode = "postgres"
    services.workflow_automation.dispatch_due_workflow_schedules.return_value = []
    lock = threading.Lock()
    claims = []
    finished = []

    def current_operation(worker_id, limits=None):
        with lock:
            claims.append(worker_id)
            first = len(claims) == 1
        if first:
            signal.raise_signal(signal.SIGTERM)
            import time

            time.sleep(0.3)
            with lock:
                finished.append(worker_id)
        return None

    services.execution.process_next_execution_job.side_effect = current_operation
    services.tasks.process_next_turn.return_value = None
    monkeypatch.setattr(worker, "build_worker_services", lambda *_: services)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        worker.main()
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert len(finished) == 1  # the in-flight operation was not cut short
    services.close.assert_called_once()
