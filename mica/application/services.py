from __future__ import annotations

import re
from contextlib import nullcontext
from typing import Any, Dict, List, Optional

from mica.domain import (
    AssigneeType,
    Comment,
    Issue,
    IssueStatus,
    Task,
    TaskEvent,
    TaskStatus,
    utc_now,
)

from .ports import (
    AgentRepository,
    EventPublisher,
    EventRepository,
    IssueRepository,
    SquadRepository,
    TaskRepository,
    TransactionManager,
)


class NotFoundError(LookupError):
    pass


class InvalidStateError(RuntimeError):
    pass


class IssueService:
    _transitions = {
        IssueStatus.BACKLOG: {IssueStatus.TODO, IssueStatus.CANCELLED},
        IssueStatus.TODO: {IssueStatus.IN_PROGRESS, IssueStatus.BACKLOG, IssueStatus.CANCELLED},
        IssueStatus.IN_PROGRESS: {
            IssueStatus.IN_REVIEW,
            IssueStatus.BLOCKED,
            IssueStatus.CANCELLED,
        },
        IssueStatus.IN_REVIEW: {
            IssueStatus.DONE,
            IssueStatus.IN_PROGRESS,
            IssueStatus.BLOCKED,
            IssueStatus.CANCELLED,
        },
        IssueStatus.BLOCKED: {IssueStatus.TODO, IssueStatus.IN_PROGRESS, IssueStatus.CANCELLED},
        IssueStatus.DONE: set(),
        IssueStatus.CANCELLED: set(),
    }

    def __init__(
        self,
        issues: IssueRepository,
        transactions: Optional[TransactionManager] = None,
    ) -> None:
        self.issues = issues
        self.transactions = transactions

    def _transaction(self):
        return self.transactions.transaction() if self.transactions is not None else nullcontext()

    def get(self, issue_id: str) -> Issue:
        issue = self.issues.get(issue_id)
        if issue is None:
            raise NotFoundError(f"issue not found: {issue_id}")
        return issue

    def list(self) -> List[Issue]:
        return self.issues.list()

    def transition(self, issue_id: str, target: IssueStatus) -> Issue:
        with self._transaction():
            issue = self._locked_issue(issue_id)
            if target not in self._transitions[issue.status]:
                raise InvalidStateError(
                    f"issue {issue.id} cannot transition from {issue.status.value} to {target.value}"
                )
            issue.status = target
            issue.updated_at = utc_now()
            return self.issues.save(issue)

    def add_comment(
        self,
        issue_id: str,
        body: str,
        author_id: str,
        parent_comment_id: Optional[str] = None,
    ) -> Comment:
        with self._transaction():
            issue = self._locked_issue(issue_id)
            if parent_comment_id and not any(
                item.id == parent_comment_id for item in issue.comments
            ):
                raise NotFoundError(f"parent comment not found: {parent_comment_id}")
            comment = Comment(
                issue_id=issue_id,
                body=body,
                author_id=author_id,
                parent_comment_id=parent_comment_id,
            )
            issue.comments.append(comment)
            issue.updated_at = utc_now()
            self.issues.save(issue)
        return comment

    def _locked_issue(self, issue_id: str) -> Issue:
        issue = self.issues.get_for_update(issue_id)
        if issue is None:
            raise NotFoundError(f"issue not found: {issue_id}")
        return issue


class TaskService:
    def __init__(
        self,
        tasks: TaskRepository,
        issues: IssueRepository,
        events: EventRepository,
        publisher: Optional[EventPublisher] = None,
        transactions: Optional[TransactionManager] = None,
    ) -> None:
        self.tasks = tasks
        self.issues = issues
        self.events = events
        self.publisher = publisher
        self.transactions = transactions

    def _transaction(self):
        return self.transactions.transaction() if self.transactions is not None else nullcontext()

    def enqueue(
        self,
        issue: Issue,
        agent_id: str,
        runtime_id: str,
        trigger_type: str,
        trigger_id: str,
        squad_id: Optional[str] = None,
        is_leader_task: bool = False,
        delegated_from_task_id: Optional[str] = None,
        handoff_note: Optional[str] = None,
        merge_comment: bool = False,
    ) -> Task:
        task = Task(
            issue_id=issue.id,
            agent_id=agent_id,
            runtime_id=runtime_id,
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            squad_id=squad_id,
            is_leader_task=is_leader_task,
            delegated_from_task_id=delegated_from_task_id,
            handoff_note=handoff_note,
            trigger_comment_id=trigger_id if trigger_type == "comment.created" else None,
        )
        merged = False
        if merge_comment and task.trigger_comment_id:
            task, merged = self.tasks.enqueue_or_merge(task)
        else:
            task = self.tasks.add(task)
        self.record(
            task.id,
            "task.input_merged" if merged else "task.queued",
            {
                "issue_id": issue.id,
                "runtime_id": runtime_id,
                "comment_id": task.trigger_comment_id,
            },
        )
        return task

    def get(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        return task

    def claim_next(
        self, runtime_id: str, worker_id: str, lease_seconds: int = 120
    ) -> Optional[Task]:
        with self._transaction():
            task = self.tasks.claim_next(runtime_id, worker_id, lease_seconds)
            if task is not None:
                self.record(task.id, "task.dispatched", {"worker_id": worker_id})
        return task

    def recover_expired(self, runtime_id: str) -> List[Task]:
        with self._transaction():
            recovered = self.tasks.recover_expired(runtime_id)
            for task in recovered:
                self.record(task.id, "task.recovered", {"attempt": task.attempt})
        return recovered

    def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int = 120) -> bool:
        return self.tasks.renew_lease(task_id, worker_id, lease_seconds)

    def request_cancel(self, task_id: str) -> Task:
        with self._transaction():
            task = self.tasks.request_cancel(task_id)
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if task.status == TaskStatus.CANCELLED:
                self.record(task.id, "task.cancelled", {"phase": "queued"})
            elif task.cancel_requested:
                self.record(task.id, "task.cancel_requested", {})
        return task

    def is_cancel_requested(self, task_id: str) -> bool:
        task = self.get(task_id)
        return task.cancel_requested or task.status == TaskStatus.CANCELLED

    def cancel_running(
        self,
        task_id: str,
        worker_id: Optional[str] = None,
        attempt: Optional[int] = None,
        advance_issue: bool = True,
    ) -> Task:
        with self._transaction():
            task = self._locked_task(task_id)
            self._require_owner(task, worker_id, attempt)
            self._require(task, TaskStatus.DISPATCHED, TaskStatus.RUNNING)
            task.status = TaskStatus.CANCELLED
            task.cancel_requested = True
            task.lease_expires_at = None
            task.updated_at = utc_now()
            self.tasks.save(task)
            if advance_issue:
                issue = self._get_issue(task.issue_id, for_update=True)
                issue.status = IssueStatus.CANCELLED
                issue.updated_at = utc_now()
                self.issues.save(issue)
            self.record(task.id, "task.cancelled", {"phase": "running"})
        return task

    def bind_execution_context(
        self,
        task_id: str,
        prior_session_id: Optional[str],
        branch_name: Optional[str],
        worktree_path: Optional[str],
        worker_id: Optional[str] = None,
        attempt: Optional[int] = None,
    ) -> Task:
        with self._transaction():
            task = self._locked_task(task_id)
            self._require_owner(task, worker_id, attempt)
            self._require(task, TaskStatus.DISPATCHED)
            task.prior_session_id = prior_session_id
            task.branch_name = branch_name
            task.worktree_path = worktree_path
            task.updated_at = utc_now()
            return self.tasks.save(task)

    def previous_execution(self, task: Task) -> Optional[Task]:
        return self.tasks.find_previous(task.issue_id, task.agent_id, task.id)

    def has_pending(
        self, issue_id: str, agent_id: str, is_leader_task: Optional[bool] = None
    ) -> bool:
        return self.tasks.has_pending(issue_id, agent_id, is_leader_task)

    def active_for_issue(self, issue_id: str) -> List[Task]:
        active = {
            TaskStatus.QUEUED,
            TaskStatus.DISPATCHED,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_INPUT,
        }
        return [task for task in self.tasks.list(issue_id) if task.status in active]

    def start(
        self,
        task_id: str,
        worker_id: Optional[str] = None,
        attempt: Optional[int] = None,
    ) -> Task:
        with self._transaction():
            task = self._locked_task(task_id)
            self._require_owner(task, worker_id, attempt)
            self._require(task, TaskStatus.DISPATCHED)
            task.status = TaskStatus.RUNNING
            task.updated_at = utc_now()
            self.tasks.save(task)
            issue = self._get_issue(task.issue_id, for_update=True)
            issue.status = IssueStatus.IN_PROGRESS
            issue.last_error = None
            issue.updated_at = utc_now()
            self.issues.save(issue)
            self.record(task.id, "task.started", {})
        return task

    def complete(
        self,
        task_id: str,
        output: str,
        session_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        attempt: Optional[int] = None,
        advance_issue: bool = True,
        publish_comment: bool = True,
        comment_body: Optional[str] = None,
    ) -> Task:
        with self._transaction():
            task = self._locked_task(task_id)
            self._require_owner(task, worker_id, attempt)
            self._require(task, TaskStatus.RUNNING)
            task.status = TaskStatus.COMPLETED
            task.output = output
            task.session_id = session_id
            task.lease_expires_at = None
            task.updated_at = utc_now()
            self.tasks.save(task)
            if advance_issue or (publish_comment and (comment_body or output).strip()):
                issue = self._get_issue(task.issue_id, for_update=True)
                if advance_issue:
                    issue.status = IssueStatus.IN_REVIEW
                if publish_comment:
                    self._append_agent_comment(issue, task, comment_body or output)
                issue.updated_at = utc_now()
                self.issues.save(issue)
            self.record(task.id, "task.completed", {"output": output, "session_id": session_id})
        return task

    def wait_for_input(
        self,
        task_id: str,
        output: str,
        session_id: Optional[str],
        reason: str,
        worker_id: Optional[str] = None,
        attempt: Optional[int] = None,
    ) -> Task:
        with self._transaction():
            task = self._locked_task(task_id)
            self._require_owner(task, worker_id, attempt)
            self._require(task, TaskStatus.RUNNING)
            task.status = TaskStatus.WAITING_INPUT
            task.output = output
            task.session_id = session_id
            task.lease_expires_at = None
            task.updated_at = utc_now()
            self.tasks.save(task)
            issue = self._get_issue(task.issue_id, for_update=True)
            issue.status = IssueStatus.BLOCKED
            issue.last_error = None
            self._append_agent_comment(issue, task, output)
            issue.updated_at = utc_now()
            self.issues.save(issue)
            self.record(
                task.id,
                "task.input_required",
                {"output": output, "session_id": session_id, "reason": reason},
            )
        return task

    @staticmethod
    def _append_agent_comment(issue: Issue, task: Task, body: str) -> None:
        content = body.strip()
        if not content or any(comment.source_task_id == task.id for comment in issue.comments):
            return
        issue.comments.append(
            Comment(
                issue_id=issue.id,
                body=content,
                author_id=task.agent_id,
                author_type="agent",
                parent_comment_id=task.trigger_comment_id,
                source_task_id=task.id,
            )
        )

    def fail(
        self,
        task_id: str,
        error: str,
        worker_id: Optional[str] = None,
        attempt: Optional[int] = None,
        advance_issue: bool = True,
    ) -> Task:
        with self._transaction():
            task = self._locked_task(task_id)
            self._require_owner(task, worker_id, attempt)
            self._require(task, TaskStatus.DISPATCHED, TaskStatus.RUNNING)
            task.status = TaskStatus.FAILED
            task.error = error
            task.lease_expires_at = None
            task.updated_at = utc_now()
            self.tasks.save(task)
            if advance_issue:
                issue = self._get_issue(task.issue_id, for_update=True)
                issue.status = IssueStatus.BLOCKED
                issue.last_error = error
                issue.updated_at = utc_now()
                self.issues.save(issue)
            self.record(task.id, "task.failed", {"error": error})
        return task

    def record(self, task_id: str, event_type: str, payload: Dict[str, Any]) -> TaskEvent:
        event = self.events.add(TaskEvent(task_id=task_id, event_type=event_type, payload=payload))
        if self.publisher is not None:
            self.publisher.publish(event)
        return event

    def _get_issue(self, issue_id: str, for_update: bool = False) -> Issue:
        issue = self.issues.get_for_update(issue_id) if for_update else self.issues.get(issue_id)
        if issue is None:
            raise NotFoundError(f"issue not found: {issue_id}")
        return issue

    def _locked_task(self, task_id: str) -> Task:
        task = self.tasks.get_for_update(task_id)
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        return task

    @staticmethod
    def _require_owner(task: Task, worker_id: Optional[str], attempt: Optional[int]) -> None:
        if worker_id is not None and task.claimed_by != worker_id:
            raise InvalidStateError(f"task {task.id} is owned by another daemon")
        if attempt is not None and task.attempt != attempt:
            raise InvalidStateError(
                f"task {task.id} attempt {attempt} is stale; current attempt is {task.attempt}"
            )

    @staticmethod
    def _require(task: Task, *allowed: TaskStatus) -> None:
        if task.status not in allowed:
            expected = ", ".join(status.value for status in allowed)
            raise InvalidStateError(f"task {task.id} is {task.status.value}; expected {expected}")


class TriggerService:
    """Turns user actions into durable work without knowing how an Agent executes it."""

    def __init__(
        self,
        issues: IssueRepository,
        agents: AgentRepository,
        issue_service: IssueService,
        task_service: TaskService,
        squads: Optional[SquadRepository] = None,
        transactions: Optional[TransactionManager] = None,
    ) -> None:
        self.issues = issues
        self.agents = agents
        self.issue_service = issue_service
        self.task_service = task_service
        self.squads = squads
        self.transactions = transactions

    def _transaction(self):
        return self.transactions.transaction() if self.transactions is not None else nullcontext()

    def create_issue(
        self,
        title: str,
        body: str,
        workspace_id: str,
        assignee_id: str,
        assignee_type: AssigneeType = AssigneeType.AGENT,
        project_path: Optional[str] = None,
        project_execution_mode: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
    ) -> Issue:
        squad = None
        if assignee_type == AssigneeType.AGENT:
            agent = self._get_agent(assignee_id)
        else:
            if self.squads is None:
                raise NotFoundError("squad support is not configured")
            squad = self.squads.get(assignee_id)
            if squad is None or squad.workspace_id != workspace_id:
                raise NotFoundError(f"squad not found in workspace: {assignee_id}")
            agent = self._get_agent(squad.leader_agent_id)
        with self._transaction():
            issue = self.issues.add(
                Issue(
                    title=title,
                    body=body,
                    workspace_id=workspace_id,
                    assignee_type=assignee_type,
                    assignee_id=assignee_id,
                    project_path=project_path,
                    project_execution_mode=project_execution_mode,
                    created_by_user_id=created_by_user_id,
                )
            )
            self.task_service.enqueue(
                issue,
                agent.id,
                agent.runtime_id,
                "issue.created",
                issue.id,
                squad_id=squad.id if squad else None,
                is_leader_task=squad is not None,
                handoff_note="Issue assigned to squad" if squad else None,
            )
        return issue

    def add_comment(
        self,
        issue_id: str,
        body: str,
        author_id: str,
        parent_comment_id: Optional[str] = None,
    ) -> Comment:
        issue = self.issue_service.get(issue_id)
        with self._transaction():
            comment = self.issue_service.add_comment(
                issue_id,
                body,
                author_id,
                parent_comment_id,
            )
            if issue.status not in {
                IssueStatus.BACKLOG,
                IssueStatus.DONE,
                IssueStatus.CANCELLED,
            }:
                for agent, squad_id, is_leader in self._comment_targets(issue, comment):
                    self.task_service.enqueue(
                        issue,
                        agent.id,
                        agent.runtime_id,
                        "comment.created",
                        comment.id,
                        squad_id=squad_id,
                        is_leader_task=is_leader,
                        merge_comment=True,
                    )
        return comment

    def _comment_targets(self, issue: Issue, comment: Comment):
        mentions = re.findall(
            r"mention://(agent|squad)/([\w.-]+)|@(agent|squad):([\w.-]+)",
            comment.body,
        )
        targets = []
        for markdown_kind, markdown_id, plain_kind, plain_id in mentions:
            kind = markdown_kind or plain_kind
            target_id = markdown_id or plain_id
            if kind == "agent":
                targets.append((self._get_agent(target_id), None, False))
            else:
                if self.squads is None:
                    raise NotFoundError("squad support is not configured")
                squad = self.squads.get(target_id)
                if squad is None or squad.workspace_id != issue.workspace_id:
                    raise NotFoundError(f"squad not found in workspace: {target_id}")
                targets.append((self._get_agent(squad.leader_agent_id), squad.id, True))
        if not targets and comment.parent_comment_id:
            parent = next(
                (
                    item
                    for item in issue.comments
                    if item.id == comment.parent_comment_id
                ),
                None,
            )
            if parent and parent.source_task_id:
                replied = self.task_service.get(parent.source_task_id)
                targets.append(
                    (
                        self._get_agent(replied.agent_id),
                        replied.squad_id,
                        replied.is_leader_task,
                    )
                )
            elif parent and parent.author_type == "agent":
                targets.append((self._get_agent(parent.author_id), None, False))
            elif parent:
                root = parent
                visited = set()
                while root.parent_comment_id and root.id not in visited:
                    visited.add(root.id)
                    next_root = next(
                        (item for item in issue.comments if item.id == root.parent_comment_id),
                        None,
                    )
                    if next_root is None:
                        break
                    root = next_root
                for task in self.task_service.tasks.list(issue.id):
                    if (
                        task.trigger_comment_id == root.id
                        or root.id in task.merged_comment_ids
                    ):
                        targets.append(
                            (
                                self._get_agent(task.agent_id),
                                task.squad_id,
                                task.is_leader_task,
                            )
                        )
                        break
                if not targets:
                    return []
        if not targets:
            if issue.assignee_type == AssigneeType.AGENT:
                targets.append((self._get_agent(issue.assignee_id), None, False))
            else:
                if self.squads is None:
                    raise NotFoundError("squad support is not configured")
                squad = self.squads.get(issue.assignee_id)
                if squad is None:
                    raise NotFoundError(f"squad not found: {issue.assignee_id}")
                targets.append((self._get_agent(squad.leader_agent_id), squad.id, True))
        deduplicated = {}
        for target in targets:
            existing = deduplicated.get(target[0].id)
            if existing is None or target[2]:
                deduplicated[target[0].id] = target
        return list(deduplicated.values())

    def transition_issue(self, issue_id: str, target: IssueStatus) -> Issue:
        with self._transaction():
            active = self.task_service.active_for_issue(issue_id)
            if target == IssueStatus.DONE and active:
                raise InvalidStateError("issue cannot be completed while tasks are still active")
            issue = self.issue_service.transition(issue_id, target)
            if target == IssueStatus.CANCELLED:
                for task in active:
                    self.task_service.request_cancel(task.id)
            return issue

    def _get_agent(self, agent_id: str):
        agent = self.agents.get(agent_id)
        if agent is None:
            raise NotFoundError(f"agent not found: {agent_id}")
        return agent
