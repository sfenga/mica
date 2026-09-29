from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from threading import RLock
from typing import Dict, List, Optional

from mica.domain import (
    Agent,
    Issue,
    Project,
    Runtime,
    RuntimeCapability,
    Squad,
    SquadMember,
    Task,
    TaskEvent,
    TaskStatus,
    utc_now,
)


class InMemoryIssueRepository:
    def __init__(self) -> None:
        self._items: Dict[str, Issue] = {}
        self._lock = RLock()

    def add(self, issue: Issue) -> Issue:
        with self._lock:
            self._items[issue.id] = deepcopy(issue)
            return deepcopy(issue)

    def get(self, issue_id: str) -> Optional[Issue]:
        with self._lock:
            item = self._items.get(issue_id)
            return deepcopy(item) if item else None

    def get_for_update(self, issue_id: str) -> Optional[Issue]:
        return self.get(issue_id)

    def save(self, issue: Issue) -> Issue:
        with self._lock:
            if issue.id not in self._items:
                raise KeyError(issue.id)
            self._items[issue.id] = deepcopy(issue)
            return deepcopy(issue)

    def list(self) -> List[Issue]:
        with self._lock:
            return [
                deepcopy(item)
                for item in sorted(self._items.values(), key=lambda value: value.created_at)
            ]


class InMemoryTaskRepository:
    """Thread-safe adapter mirroring the PostgreSQL task-claim contract."""

    def __init__(self) -> None:
        self._items: Dict[str, Task] = {}
        self._lock = RLock()

    def add(self, task: Task) -> Task:
        with self._lock:
            self._items[task.id] = deepcopy(task)
            return deepcopy(task)

    def enqueue_or_merge(self, task: Task):
        with self._lock:
            queued = next(
                (
                    item
                    for item in self._items.values()
                    if item.issue_id == task.issue_id
                    and item.agent_id == task.agent_id
                    and item.status == TaskStatus.QUEUED
                ),
                None,
            )
            if queued is None:
                self._items[task.id] = deepcopy(task)
                return deepcopy(task), False
            previous_ids = [*queued.merged_comment_ids]
            if queued.trigger_comment_id:
                previous_ids.append(queued.trigger_comment_id)
            queued.merged_comment_ids = list(
                dict.fromkeys(
                    item for item in previous_ids if item != task.trigger_comment_id
                )
            )
            queued.trigger_type = task.trigger_type
            queued.trigger_id = task.trigger_id
            queued.trigger_comment_id = task.trigger_comment_id
            if task.squad_id:
                queued.squad_id = task.squad_id
            queued.is_leader_task = queued.is_leader_task or task.is_leader_task
            queued.handoff_note = task.handoff_note or queued.handoff_note
            queued.updated_at = utc_now()
            return deepcopy(queued), True

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            item = self._items.get(task_id)
            return deepcopy(item) if item else None

    def get_for_update(self, task_id: str) -> Optional[Task]:
        return self.get(task_id)

    def save(self, task: Task) -> Task:
        with self._lock:
            if task.id not in self._items:
                raise KeyError(task.id)
            self._items[task.id] = deepcopy(task)
            return deepcopy(task)

    def list(self, issue_id: Optional[str] = None) -> List[Task]:
        with self._lock:
            values = self._items.values()
            if issue_id is not None:
                values = [item for item in values if item.issue_id == issue_id]
            return [deepcopy(item) for item in sorted(values, key=lambda value: value.created_at)]

    def claim_next(self, runtime_id: str, worker_id: str, lease_seconds: int) -> Optional[Task]:
        with self._lock:
            candidates = sorted(self._items.values(), key=lambda value: value.created_at)
            for task in candidates:
                if task.runtime_id != runtime_id or task.status != TaskStatus.QUEUED:
                    continue
                assignment_busy = any(
                    other.id != task.id
                    and other.issue_id == task.issue_id
                    and other.agent_id == task.agent_id
                    and other.status in {TaskStatus.DISPATCHED, TaskStatus.RUNNING}
                    for other in self._items.values()
                )
                if assignment_busy:
                    continue
                workers_active = (
                    task.is_leader_task
                    and task.squad_id
                    and any(
                        other.id != task.id
                        and other.issue_id == task.issue_id
                        and other.squad_id == task.squad_id
                        and not other.is_leader_task
                        and other.status
                        in {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.RUNNING}
                        for other in self._items.values()
                    )
                )
                if workers_active:
                    continue
                task.status = TaskStatus.DISPATCHED
                delivered = [*task.merged_comment_ids]
                if task.trigger_comment_id:
                    delivered.append(task.trigger_comment_id)
                task.delivered_comment_ids = list(dict.fromkeys(delivered))
                task.claimed_by = worker_id
                task.attempt += 1
                task.lease_expires_at = utc_now() + timedelta(seconds=lease_seconds)
                task.updated_at = utc_now()
                return deepcopy(task)
        return None

    def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self._lock:
            task = self._items.get(task_id)
            if (
                task is None
                or task.claimed_by != worker_id
                or task.status
                not in {
                    TaskStatus.DISPATCHED,
                    TaskStatus.RUNNING,
                }
            ):
                return False
            task.lease_expires_at = utc_now() + timedelta(seconds=lease_seconds)
            task.updated_at = utc_now()
            return True

    def recover_expired(self, runtime_id: str) -> List[Task]:
        recovered: List[Task] = []
        now = utc_now()
        with self._lock:
            for task in self._items.values():
                if (
                    task.runtime_id == runtime_id
                    and task.status in {TaskStatus.DISPATCHED, TaskStatus.RUNNING}
                    and task.lease_expires_at is not None
                    and task.lease_expires_at <= now
                ):
                    task.status = TaskStatus.QUEUED
                    task.claimed_by = None
                    task.lease_expires_at = None
                    task.updated_at = now
                    recovered.append(deepcopy(task))
        return recovered

    def request_cancel(self, task_id: str) -> Optional[Task]:
        with self._lock:
            task = self._items.get(task_id)
            if task is None:
                return None
            if task.status in {TaskStatus.QUEUED, TaskStatus.WAITING_INPUT}:
                task.status = TaskStatus.CANCELLED
            elif task.status in {TaskStatus.DISPATCHED, TaskStatus.RUNNING}:
                task.cancel_requested = True
            task.updated_at = utc_now()
            return deepcopy(task)

    def find_previous(self, issue_id: str, agent_id: str, exclude_task_id: str) -> Optional[Task]:
        with self._lock:
            candidates = [
                task
                for task in self._items.values()
                if task.issue_id == issue_id
                and task.agent_id == agent_id
                and task.id != exclude_task_id
                and (task.session_id or task.branch_name or task.worktree_path)
            ]
            if not candidates:
                return None
            return deepcopy(max(candidates, key=lambda value: value.created_at))

    def has_pending(
        self, issue_id: str, agent_id: str, is_leader_task: Optional[bool] = None
    ) -> bool:
        pending = {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.RUNNING}
        with self._lock:
            return any(
                task.issue_id == issue_id
                and task.agent_id == agent_id
                and task.status in pending
                and (is_leader_task is None or task.is_leader_task == is_leader_task)
                for task in self._items.values()
            )


class InMemoryEventRepository:
    def __init__(self) -> None:
        self._items: List[TaskEvent] = []
        self._lock = RLock()

    def add(self, event: TaskEvent) -> TaskEvent:
        with self._lock:
            self._items.append(event)
            return event

    def list(self, task_id: Optional[str] = None) -> List[TaskEvent]:
        with self._lock:
            items = (
                self._items
                if task_id is None
                else [item for item in self._items if item.task_id == task_id]
            )
            return list(items)

    def list_after(self, created_at, event_id: str) -> List[TaskEvent]:
        with self._lock:
            return [
                item
                for item in sorted(self._items, key=lambda value: (value.created_at, value.id))
                if (item.created_at, item.id) > (created_at, event_id)
            ]


class InMemoryAgentRepository:
    def __init__(self) -> None:
        self._items: Dict[str, Agent] = {}
        self._lock = RLock()

    def add(self, agent: Agent) -> Agent:
        with self._lock:
            self._items[agent.id] = agent
            return agent

    def get(self, agent_id: str) -> Optional[Agent]:
        with self._lock:
            return self._items.get(agent_id)

    def list(self) -> List[Agent]:
        with self._lock:
            return list(self._items.values())


class InMemoryRuntimeRepository:
    def __init__(self) -> None:
        self._items: Dict[str, Runtime] = {}
        self._lock = RLock()

    def add(self, runtime: Runtime) -> Runtime:
        with self._lock:
            self._items[runtime.id] = runtime
            return runtime

    def get(self, runtime_id: str) -> Optional[Runtime]:
        with self._lock:
            return self._items.get(runtime_id)

    def list(self) -> List[Runtime]:
        with self._lock:
            return list(self._items.values())


class InMemoryRuntimeCapabilityRepository:
    def __init__(self) -> None:
        self._items: Dict[tuple[str, str], RuntimeCapability] = {}
        self._lock = RLock()

    def replace_for_runtime(
        self, runtime_id: str, capabilities: List[RuntimeCapability]
    ) -> List[RuntimeCapability]:
        with self._lock:
            self._items = {
                key: value for key, value in self._items.items() if key[0] != runtime_id
            }
            for capability in capabilities:
                self._items[(runtime_id, capability.provider)] = capability
            return list(capabilities)

    def list(self, runtime_id: Optional[str] = None) -> List[RuntimeCapability]:
        with self._lock:
            values = list(self._items.values())
            return [item for item in values if item.runtime_id == runtime_id] if runtime_id else values

    def get(self, runtime_id: str, provider: str) -> Optional[RuntimeCapability]:
        with self._lock:
            return self._items.get((runtime_id, provider))


class InMemoryProjectRepository:
    def __init__(self) -> None:
        self._items: Dict[str, Project] = {}
        self._lock = RLock()

    def add(self, project: Project) -> Project:
        with self._lock:
            existing = self.find_by_path(
                project.workspace_id, project.runtime_id, project.local_path
            )
            if existing is not None:
                return existing
            self._items[project.id] = project
            return project

    def save(self, project: Project) -> Project:
        with self._lock:
            if project.id not in self._items:
                raise KeyError(project.id)
            self._items[project.id] = project
            return project

    def get(self, project_id: str) -> Optional[Project]:
        with self._lock:
            return self._items.get(project_id)

    def find_by_path(
        self, workspace_id: str, runtime_id: str, local_path: str
    ) -> Optional[Project]:
        with self._lock:
            return next(
                (
                    item
                    for item in self._items.values()
                    if item.workspace_id == workspace_id
                    and item.runtime_id == runtime_id
                    and item.local_path == local_path
                ),
                None,
            )

    def list(self, workspace_id: Optional[str] = None) -> List[Project]:
        with self._lock:
            values = list(self._items.values())
            return [item for item in values if item.workspace_id == workspace_id] if workspace_id else values


class InMemorySquadRepository:
    def __init__(self) -> None:
        self._items: Dict[str, Squad] = {}
        self._members: Dict[str, Dict[str, SquadMember]] = {}
        self._lock = RLock()

    def add(self, squad: Squad) -> Squad:
        with self._lock:
            if any(
                item.workspace_id == squad.workspace_id and item.name == squad.name
                for item in self._items.values()
                if item.id != squad.id
            ):
                raise ValueError(f"squad name already exists in workspace: {squad.name}")
            self._items[squad.id] = squad
            return squad

    def save(self, squad: Squad) -> Squad:
        with self._lock:
            if squad.id not in self._items:
                raise KeyError(squad.id)
            if any(
                item.workspace_id == squad.workspace_id and item.name == squad.name
                for item in self._items.values()
                if item.id != squad.id
            ):
                raise ValueError(f"squad name already exists in workspace: {squad.name}")
            self._items[squad.id] = squad
            return squad

    def get(self, squad_id: str) -> Optional[Squad]:
        with self._lock:
            return self._items.get(squad_id)

    def list(self, workspace_id: Optional[str] = None) -> List[Squad]:
        with self._lock:
            values = self._items.values()
            if workspace_id is not None:
                values = [item for item in values if item.workspace_id == workspace_id]
            return list(values)

    def add_member(self, member: SquadMember) -> SquadMember:
        with self._lock:
            members = self._members.setdefault(member.squad_id, {})
            existing = members.get(member.agent_id)
            if existing is None:
                members[member.agent_id] = member
            else:
                members[member.agent_id] = SquadMember(
                    id=existing.id,
                    squad_id=existing.squad_id,
                    agent_id=existing.agent_id,
                    role=member.role,
                    capabilities=list(member.capabilities),
                    created_at=existing.created_at,
                )
            return members[member.agent_id]

    def list_members(self, squad_id: str) -> List[SquadMember]:
        with self._lock:
            return list(self._members.get(squad_id, {}).values())

    def remove_member(self, squad_id: str, agent_id: str) -> bool:
        with self._lock:
            return self._members.get(squad_id, {}).pop(agent_id, None) is not None
