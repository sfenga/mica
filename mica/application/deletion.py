from __future__ import annotations

import logging
from contextlib import nullcontext

from .services import NotFoundError


class IssueDeletionService:
    """Cancel best-effort, then remove platform history without waiting for providers."""

    def __init__(self, issues, tasks, events, channels, transactions=None):
        self.issues = issues
        self.tasks = tasks
        self.events = events
        self.channels = channels
        self.transactions = transactions

    def delete(self, issue_id: str) -> None:
        if self.issues.get(issue_id) is None:
            raise NotFoundError(f"issue not found: {issue_id}")
        # Cancel outside the delete transaction: a cancellation error must not
        # poison the PostgreSQL transaction or prevent the subsequent deletion.
        for task in self.tasks.list(issue_id):
            try:
                self.tasks.request_cancel(task.id)
            except Exception:
                logging.getLogger(__name__).exception(
                    "failed to cancel task before issue deletion", extra={"task_id": task.id}
                )
        transaction = self.transactions.transaction() if self.transactions else nullcontext()
        with transaction:
            if self.issues.get_for_update(issue_id) is None:
                raise NotFoundError(f"issue not found: {issue_id}")
            task_ids = [task.id for task in self.tasks.list(issue_id)]
            event_ids = [event.id for task_id in task_ids for event in self.events.list(task_id)]
            self.channels.delete_issue_data(issue_id, event_ids)
            self.events.delete_for_tasks(task_ids)
            self.tasks.delete_for_issue(issue_id)
            self.issues.delete(issue_id)
