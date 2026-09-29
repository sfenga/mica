from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Optional

from mica.domain import AssigneeType, ExternalEvent, Issue, IssueOrigin, OutboxMessage

from .ports import ChannelRepository, TransactionManager
from .services import IssueService, TriggerService


class IdempotencyConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class IngressCommand:
    channel: str
    tenant_key: str
    external_event_id: str
    external_user_id: str
    display_name: str
    conversation_id: str
    title: str
    body: str
    workspace_id: str
    assignee_id: str
    assignee_type: AssigneeType = AssigneeType.AGENT
    project_path: Optional[str] = None
    project_execution_mode: Optional[str] = None
    event_type: str = "issue.created"
    external_message_id: str = ""
    related_issue_id: Optional[str] = None

    def payload_hash(self) -> str:
        canonical = json.dumps(
            {
                "title": self.title,
                "body": self.body,
                "workspace_id": self.workspace_id,
                "assignee_id": self.assignee_id,
                "assignee_type": self.assignee_type.value,
                "project_path": self.project_path,
                "project_execution_mode": self.project_execution_mode,
                "related_issue_id": self.related_issue_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IngressService:
    """Creates durable work for every UI/channel through one idempotent path."""

    def __init__(
        self,
        channels: ChannelRepository,
        triggers: TriggerService,
        issues: IssueService,
        transactions: Optional[TransactionManager] = None,
    ) -> None:
        self.channels = channels
        self.triggers = triggers
        self.issues = issues
        self.transactions = transactions
        self._memory_lock = RLock()

    def submit(self, command: IngressCommand) -> tuple[Issue, bool]:
        transaction = self.transactions.transaction() if self.transactions else nullcontext()
        # The lock gives the in-memory adapter the same idempotency behavior as a DB transaction.
        lock = nullcontext() if self.transactions else self._memory_lock
        with lock, transaction:
            event, created = self.channels.reserve_event(
                ExternalEvent(
                    channel=command.channel,
                    tenant_key=command.tenant_key,
                    external_event_id=command.external_event_id,
                    event_type=command.event_type,
                    payload_hash=command.payload_hash(),
                )
            )
            if not created:
                if event.payload_hash != command.payload_hash():
                    raise IdempotencyConflictError(
                        "idempotency key was already used with another payload"
                    )
                if not event.issue_id:
                    raise IdempotencyConflictError("event is already being processed")
                return self.issues.get(event.issue_id), False

            user, _ = self.channels.resolve_identity(
                command.channel,
                command.tenant_key,
                command.external_user_id,
                command.workspace_id,
                command.display_name,
            )
            if command.related_issue_id:
                issue = self.issues.get(command.related_issue_id)
                if issue.workspace_id != command.workspace_id:
                    raise ValueError("related issue does not belong to this workspace")
                self.triggers.add_comment(issue.id, command.body, user.id)
                self.channels.attach_issue(
                    command.channel,
                    command.tenant_key,
                    command.external_event_id,
                    issue.id,
                )
                if command.channel == "feishu":
                    self.channels.add_outbox(
                        OutboxMessage(
                            channel="feishu",
                            destination=command.conversation_id,
                            source_event_id=command.external_event_id,
                            message_type="comment.accepted",
                            payload={
                                "text": f"补充要求已加入：{issue.title}\nIssue: {issue.id}",
                                "issue_id": issue.id,
                            },
                        )
                    )
                return self.issues.get(issue.id), True
            issue = self.triggers.create_issue(
                title=command.title,
                body=command.body,
                workspace_id=command.workspace_id,
                assignee_id=command.assignee_id,
                assignee_type=command.assignee_type,
                project_path=command.project_path,
                project_execution_mode=command.project_execution_mode,
                created_by_user_id=user.id,
            )
            self.channels.add_origin(
                IssueOrigin(
                    issue_id=issue.id,
                    channel=command.channel,
                    tenant_key=command.tenant_key,
                    conversation_id=command.conversation_id,
                    external_message_id=command.external_message_id or command.external_event_id,
                )
            )
            self.channels.attach_issue(
                command.channel, command.tenant_key, command.external_event_id, issue.id
            )
            if command.channel == "feishu":
                self.channels.add_outbox(
                    OutboxMessage(
                        channel="feishu",
                        destination=command.conversation_id,
                        source_event_id=command.external_event_id,
                        message_type="issue.accepted",
                        payload={
                            "text": f"任务已受理：{issue.title}\nIssue: {issue.id}",
                            "issue_id": issue.id,
                        },
                    )
                )
            return issue, True


TERMINAL_EVENT_TYPES = {
    "task.completed",
    "task.failed",
    "task.input_required",
    "task.cancelled",
    "squad.completed",
}


class TaskNotificationProjector:
    def __init__(self, events, tasks, channels: ChannelRepository) -> None:
        self.events = events
        self.tasks = tasks
        self.channels = channels
        self._cursor_time = datetime.fromtimestamp(0, timezone.utc)
        self._cursor_id = ""

    def run_once(self) -> int:
        projected = 0
        for event in self.events.list_after(self._cursor_time, self._cursor_id):
            if event.event_type not in TERMINAL_EVENT_TYPES:
                self._advance(event)
                continue
            task = self.tasks.get(event.task_id)
            if task is None:
                self._advance(event)
                continue
            if task.squad_id:
                if event.event_type == "task.completed":
                    self._advance(event)
                    continue
                if (
                    event.event_type in {"task.failed", "task.cancelled"}
                    and not task.is_leader_task
                ):
                    self._advance(event)
                    continue
            origin = self.channels.get_origin(task.issue_id)
            if origin is None or origin.channel != "feishu":
                self._advance(event)
                continue
            self.channels.add_outbox(
                OutboxMessage(
                    channel="feishu",
                    destination=origin.conversation_id,
                    source_event_id=event.id,
                    message_type=event.event_type,
                    payload={"text": self._text(event.event_type, task, event.payload)},
                )
            )
            projected += 1
            # Advance only after the durable outbox write succeeds. A transient
            # repository failure must not make this process skip the event.
            self._advance(event)
        return projected

    def _advance(self, event) -> None:
        self._cursor_time, self._cursor_id = event.created_at, event.id

    @staticmethod
    def _text(event_type: str, task, payload: dict[str, Any]) -> str:
        if event_type == "squad.completed":
            detail = payload.get("summary") or task.output or "Squad 已完成汇总"
            prefix = "Squad 任务已完成"
        elif event_type == "task.completed":
            detail = task.output or "Agent 已完成处理"
            prefix = "任务已完成"
        elif event_type == "task.input_required":
            detail = payload.get("reason") or task.output or "Agent 需要补充信息"
            prefix = "任务等待补充信息"
        elif event_type == "task.failed":
            detail = task.error or payload.get("error") or "执行失败"
            prefix = "任务执行失败"
        else:
            detail = "任务已取消"
            prefix = "任务已取消"
        detail = str(detail)
        if len(detail) > 1800:
            detail = detail[:1800] + "…"
        return f"{prefix}\nTask: {task.id}\n{detail}"
