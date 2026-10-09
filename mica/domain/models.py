from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .status import AssigneeType, IssueStatus, TaskStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@dataclass
class Comment:
    issue_id: str
    body: str
    author_id: str
    parent_comment_id: Optional[str] = None
    author_type: str = "user"
    source_task_id: Optional[str] = None
    id: str = field(default_factory=lambda: new_id("comment"))
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class Issue:
    title: str
    body: str
    workspace_id: str
    assignee_type: AssigneeType
    assignee_id: str
    project_path: Optional[str] = None
    project_execution_mode: Optional[str] = None
    created_by_user_id: Optional[str] = None
    id: str = field(default_factory=lambda: new_id("issue"))
    status: IssueStatus = IssueStatus.TODO
    comments: List[Comment] = field(default_factory=list)
    last_error: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class Task:
    issue_id: str
    agent_id: str
    runtime_id: str
    trigger_type: str
    trigger_id: str
    squad_id: Optional[str] = None
    is_leader_task: bool = False
    delegated_from_task_id: Optional[str] = None
    handoff_note: Optional[str] = None
    trigger_comment_id: Optional[str] = None
    merged_comment_ids: List[str] = field(default_factory=list)
    delivered_comment_ids: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("task"))
    status: TaskStatus = TaskStatus.QUEUED
    session_id: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    claimed_by: Optional[str] = None
    prior_session_id: Optional[str] = None
    branch_name: Optional[str] = None
    worktree_path: Optional[str] = None
    recorded_tip: Optional[str] = None
    attempt: int = 0
    cancel_requested: bool = False
    output: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("event"))
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    runtime_id: str
    provider: str
    instructions: str = ""
    provider_config: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Runtime:
    id: str
    name: str
    max_slots: int = 1
    daemon_id: str = "daemon-local-1"
    status: str = "offline"
    hostname: str = ""
    platform: str = ""
    last_seen_at: Optional[datetime] = None


@dataclass(frozen=True)
class RuntimeCapability:
    runtime_id: str
    provider: str
    executable_path: str
    version: str = ""
    status: str = "available"
    error: Optional[str] = None
    detected_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Project:
    name: str
    workspace_id: str
    runtime_id: str
    local_path: str
    execution_mode: str = "worktree"
    id: str = field(default_factory=lambda: new_id("project"))
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Squad:
    name: str
    workspace_id: str
    leader_agent_id: str
    description: str = ""
    instructions: str = ""
    id: str = field(default_factory=lambda: new_id("squad"))
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class SquadMember:
    squad_id: str
    agent_id: str
    role: str = ""
    capabilities: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("squad_member"))
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class PlatformUser:
    workspace_id: str
    display_name: str
    id: str = field(default_factory=lambda: new_id("user"))
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class ExternalIdentity:
    channel: str
    tenant_key: str
    external_user_id: str
    user_id: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class ExternalEvent:
    channel: str
    tenant_key: str
    external_event_id: str
    event_type: str
    payload_hash: str
    issue_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class IssueOrigin:
    issue_id: str
    channel: str
    tenant_key: str
    conversation_id: str
    external_message_id: str
    thread_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class OutboxMessage:
    channel: str
    destination: str
    source_event_id: str
    message_type: str
    payload: Dict[str, Any]
    id: str = field(default_factory=lambda: new_id("outbox"))
    status: str = "pending"
    attempt: int = 0
    available_at: datetime = field(default_factory=utc_now)
    claimed_by: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
