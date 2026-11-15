"""Deploy shutdown finishes the current operation before closing its services."""

import signal
from unittest.mock import MagicMock

from apps.worker import main as worker


def test_sigterm_does_not_pick_up_another_task(monkeypatch):
    services = MagicMock()
    services.storage_mode = "postgres"
    services.workflow_automation.dispatch_due_workflow_schedules.return_value = []
    finished = []

    def current_operation(_worker_id):
        signal.raise_signal(signal.SIGTERM)
        finished.append(True)
        return None

    services.execution.process_next_execution_job.side_effect = current_operation
    monkeypatch.setattr(worker, "build_worker_services", lambda: services)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        worker.main()
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert finished == [True]
    services.tasks.process_next_turn.assert_not_called()
    services.close.assert_called_once()
