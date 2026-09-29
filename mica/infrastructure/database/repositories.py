from __future__ import annotations

import json
from typing import List, Optional

from mica.domain import (
    Agent,
    AssigneeType,
    Comment,
    Issue,
    IssueStatus,
    Project,
    Runtime,
    RuntimeCapability,
    Squad,
    SquadMember,
    Task,
    TaskEvent,
    TaskStatus,
)

from .connection import PostgresDatabase

TASK_COLUMNS = """
id, issue_id, agent_id, runtime_id, trigger_type, trigger_id, status,
 squad_id, is_leader_task, delegated_from_task_id, handoff_note,
trigger_comment_id, merged_comment_ids, delivered_comment_ids,
session_id, prior_session_id, branch_name, worktree_path, lease_expires_at,
claimed_by, attempt, cancel_requested, output, error, created_at, updated_at
"""


def _task(row) -> Optional[Task]:
    if row is None:
        return None
    return Task(
        id=row["id"],
        issue_id=row["issue_id"],
        agent_id=row["agent_id"],
        runtime_id=row["runtime_id"],
        trigger_type=row["trigger_type"],
        trigger_id=row["trigger_id"],
        status=TaskStatus(row["status"]),
        squad_id=row["squad_id"],
        is_leader_task=row["is_leader_task"],
        delegated_from_task_id=row["delegated_from_task_id"],
        handoff_note=row["handoff_note"],
        trigger_comment_id=row["trigger_comment_id"],
        merged_comment_ids=list(row["merged_comment_ids"] or []),
        delivered_comment_ids=list(row["delivered_comment_ids"] or []),
        session_id=row["session_id"],
        prior_session_id=row["prior_session_id"],
        branch_name=row["branch_name"],
        worktree_path=row["worktree_path"],
        lease_expires_at=row["lease_expires_at"],
        claimed_by=row["claimed_by"],
        attempt=row["attempt"],
        cancel_requested=row["cancel_requested"],
        output=row["output"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresIssueRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def add(self, issue: Issue) -> Issue:
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO issue (
                    id, title, body, workspace_id, assignee_type, assignee_id,
                    project_path, project_execution_mode, created_by_user_id, status,
                    last_error, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    issue.id,
                    issue.title,
                    issue.body,
                    issue.workspace_id,
                    issue.assignee_type.value,
                    issue.assignee_id,
                    issue.project_path,
                    issue.project_execution_mode,
                    issue.created_by_user_id,
                    issue.status.value,
                    issue.last_error,
                    issue.created_at,
                    issue.updated_at,
                ),
            )
        return issue

    def get(self, issue_id: str) -> Optional[Issue]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM issue WHERE id = %s", (issue_id,)).fetchone()
            if row is None:
                return None
            comments = connection.execute(
                "SELECT * FROM issue_comment WHERE issue_id = %s ORDER BY created_at", (issue_id,)
            ).fetchall()
        return self._from_row(row, comments)

    def get_for_update(self, issue_id: str) -> Optional[Issue]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM issue WHERE id = %s FOR UPDATE", (issue_id,)
            ).fetchone()
            if row is None:
                return None
            comments = connection.execute(
                "SELECT * FROM issue_comment WHERE issue_id = %s ORDER BY created_at", (issue_id,)
            ).fetchall()
        return self._from_row(row, comments)

    def save(self, issue: Issue) -> Issue:
        with self.database.connection() as connection:
            row = connection.execute(
                """UPDATE issue SET title=%s, body=%s, workspace_id=%s, assignee_type=%s,
                    assignee_id=%s, project_path=%s, project_execution_mode=%s, created_by_user_id=%s,
                    status=%s, last_error=%s, updated_at=%s
                    WHERE id=%s RETURNING id""",
                (
                    issue.title,
                    issue.body,
                    issue.workspace_id,
                    issue.assignee_type.value,
                    issue.assignee_id,
                    issue.project_path,
                    issue.project_execution_mode,
                    issue.created_by_user_id,
                    issue.status.value,
                    issue.last_error,
                    issue.updated_at,
                    issue.id,
                ),
            ).fetchone()
            if row is None:
                raise KeyError(issue.id)
            for comment in issue.comments:
                connection.execute(
                    """INSERT INTO issue_comment(
                        id, issue_id, body, author_id, parent_comment_id,
                        author_type, source_task_id, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING""",
                    (
                        comment.id,
                        comment.issue_id,
                        comment.body,
                        comment.author_id,
                        comment.parent_comment_id,
                        comment.author_type,
                        comment.source_task_id,
                        comment.created_at,
                    ),
                )
        return issue

    def list(self) -> List[Issue]:
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM issue ORDER BY created_at").fetchall()
        return [self.get(row["id"]) for row in rows]

    @staticmethod
    def _from_row(row, comments) -> Issue:
        return Issue(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            workspace_id=row["workspace_id"],
            assignee_type=AssigneeType(row["assignee_type"]),
            assignee_id=row["assignee_id"],
            project_path=row["project_path"],
            project_execution_mode=row.get("project_execution_mode"),
            status=IssueStatus(row["status"]),
            created_by_user_id=row.get("created_by_user_id"),
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            comments=[
                Comment(
                    id=item["id"],
                    issue_id=item["issue_id"],
                    body=item["body"],
                    author_id=item["author_id"],
                    parent_comment_id=item.get("parent_comment_id"),
                    author_type=item.get("author_type") or "user",
                    source_task_id=item.get("source_task_id"),
                    created_at=item["created_at"],
                )
                for item in comments
            ],
        )


class PostgresTaskRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def add(self, task: Task) -> Task:
        values = self._values(task)
        with self.database.connection() as connection:
            connection.execute(
                f"INSERT INTO agent_task ({TASK_COLUMNS}) VALUES ({', '.join(['%s'] * len(values))})",
                values,
            )
        return task

    def enqueue_or_merge(self, task: Task):
        values = self._values(task)
        returned_columns = ", ".join(
            "agent_task." + name.strip() for name in TASK_COLUMNS.split(",")
        )
        with self.database.connection() as connection:
            row = connection.execute(
                f"""INSERT INTO agent_task ({TASK_COLUMNS})
                VALUES ({', '.join(['%s'] * len(values))})
                ON CONFLICT (issue_id, agent_id) WHERE status='queued'
                DO UPDATE SET
                    merged_comment_ids = array_remove(
                        CASE WHEN agent_task.trigger_comment_id IS NULL
                            THEN agent_task.merged_comment_ids
                            ELSE array_append(
                                agent_task.merged_comment_ids,
                                agent_task.trigger_comment_id
                            )
                        END,
                        EXCLUDED.trigger_comment_id
                    ),
                    trigger_type = EXCLUDED.trigger_type,
                    trigger_id = EXCLUDED.trigger_id,
                    trigger_comment_id = EXCLUDED.trigger_comment_id,
                    squad_id = COALESCE(EXCLUDED.squad_id, agent_task.squad_id),
                    is_leader_task = agent_task.is_leader_task OR EXCLUDED.is_leader_task,
                    handoff_note = COALESCE(EXCLUDED.handoff_note, agent_task.handoff_note),
                    updated_at = now()
                RETURNING {returned_columns}, (xmax = 0) AS inserted""",
                values,
            ).fetchone()
        return _task(row), not bool(row["inserted"])

    def get(self, task_id: str) -> Optional[Task]:
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT {TASK_COLUMNS} FROM agent_task WHERE id=%s", (task_id,)
            ).fetchone()
        return _task(row)

    def get_for_update(self, task_id: str) -> Optional[Task]:
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT {TASK_COLUMNS} FROM agent_task WHERE id=%s FOR UPDATE", (task_id,)
            ).fetchone()
        return _task(row)

    def save(self, task: Task) -> Task:
        with self.database.connection() as connection:
            row = connection.execute(
                """UPDATE agent_task SET status=%s, trigger_comment_id=%s,
                    merged_comment_ids=%s, delivered_comment_ids=%s,
                    session_id=%s, prior_session_id=%s,
                    branch_name=%s, worktree_path=%s, lease_expires_at=%s, claimed_by=%s,
                    attempt=%s, cancel_requested=%s, output=%s, error=%s, updated_at=%s
                    WHERE id=%s RETURNING id""",
                (
                    task.status.value,
                    task.trigger_comment_id,
                    task.merged_comment_ids,
                    task.delivered_comment_ids,
                    task.session_id,
                    task.prior_session_id,
                    task.branch_name,
                    task.worktree_path,
                    task.lease_expires_at,
                    task.claimed_by,
                    task.attempt,
                    task.cancel_requested,
                    task.output,
                    task.error,
                    task.updated_at,
                    task.id,
                ),
            ).fetchone()
            if row is None:
                raise KeyError(task.id)
        return task

    def list(self, issue_id: Optional[str] = None) -> List[Task]:
        query = f"SELECT {TASK_COLUMNS} FROM agent_task"
        params = ()
        if issue_id is not None:
            query += " WHERE issue_id=%s"
            params = (issue_id,)
        query += " ORDER BY created_at"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_task(row) for row in rows]

    def claim_next(self, runtime_id: str, worker_id: str, lease_seconds: int) -> Optional[Task]:
        returned_columns = ", ".join(
            "task." + name.strip() for name in TASK_COLUMNS.split(",")
        )
        with self.database.connection() as connection:
            row = connection.execute(
                f"""WITH candidate AS (
                    SELECT queued.id FROM agent_task AS queued
                    JOIN issue AS owner_issue ON owner_issue.id=queued.issue_id
                    JOIN agent AS assigned_agent ON assigned_agent.id=queued.agent_id
                        AND assigned_agent.runtime_id=queued.runtime_id
                    WHERE queued.runtime_id=%s AND queued.status='queued'
                    AND owner_issue.status NOT IN ('done', 'cancelled')
                    AND NOT EXISTS (
                        SELECT 1 FROM agent_task AS active
                        WHERE active.issue_id=queued.issue_id AND active.agent_id=queued.agent_id
                        AND active.status IN ('dispatched', 'running')
                    )
                    AND (
                        NOT queued.is_leader_task OR queued.squad_id IS NULL OR NOT EXISTS (
                            SELECT 1 FROM agent_task AS squad_worker
                            WHERE squad_worker.issue_id=queued.issue_id
                                AND squad_worker.squad_id=queued.squad_id
                                AND NOT squad_worker.is_leader_task
                                AND squad_worker.status IN ('queued', 'dispatched', 'running')
                        )
                    )
                    ORDER BY queued.created_at
                    FOR UPDATE OF queued, owner_issue SKIP LOCKED
                    LIMIT 1
                )
                UPDATE agent_task AS task
                SET status='dispatched', claimed_by=%s,
                    delivered_comment_ids=(
                        SELECT COALESCE(
                            array_agg(comment_id ORDER BY first_position),
                            '{{}}'::text[]
                        )
                        FROM (
                            SELECT comment_id, min(position) AS first_position
                            FROM unnest(
                                task.merged_comment_ids ||
                                CASE WHEN task.trigger_comment_id IS NULL THEN '{{}}'::text[]
                                    ELSE ARRAY[task.trigger_comment_id] END
                            ) WITH ORDINALITY AS comments(comment_id, position)
                            GROUP BY comment_id
                        ) AS ordered_comments
                    ),
                    lease_expires_at=now() + make_interval(secs => %s),
                    attempt=task.attempt + 1, updated_at=now()
                FROM candidate
                WHERE task.id=candidate.id
                RETURNING {returned_columns}""",
                (runtime_id, worker_id, lease_seconds),
            ).fetchone()
        return _task(row)

    def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                """UPDATE agent_task SET lease_expires_at=now() + make_interval(secs => %s), updated_at=now()
                WHERE id=%s AND claimed_by=%s AND status IN ('dispatched', 'running') RETURNING id""",
                (lease_seconds, task_id, worker_id),
            ).fetchone()
        return row is not None

    def recover_expired(self, runtime_id: str) -> List[Task]:
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""UPDATE agent_task SET status='queued', claimed_by=NULL,
                    lease_expires_at=NULL, updated_at=now()
                WHERE runtime_id=%s AND status IN ('dispatched', 'running')
                    AND lease_expires_at <= now()
                RETURNING {TASK_COLUMNS}""",
                (runtime_id,),
            ).fetchall()
        return [_task(row) for row in rows]

    def request_cancel(self, task_id: str) -> Optional[Task]:
        with self.database.connection() as connection:
            row = connection.execute(
                f"""UPDATE agent_task SET
                    status=CASE WHEN status IN ('queued', 'waiting_input') THEN 'cancelled' ELSE status END,
                    cancel_requested=CASE WHEN status IN ('dispatched', 'running') THEN TRUE ELSE cancel_requested END,
                    lease_expires_at=CASE WHEN status IN ('queued', 'waiting_input') THEN NULL ELSE lease_expires_at END,
                    updated_at=now()
                WHERE id=%s RETURNING {TASK_COLUMNS}""",
                (task_id,),
            ).fetchone()
        return _task(row)

    def find_previous(self, issue_id: str, agent_id: str, exclude_task_id: str) -> Optional[Task]:
        with self.database.connection() as connection:
            row = connection.execute(
                f"""SELECT {TASK_COLUMNS} FROM agent_task
                WHERE issue_id=%s AND agent_id=%s AND id<>%s
                    AND (session_id IS NOT NULL OR branch_name IS NOT NULL OR worktree_path IS NOT NULL)
                ORDER BY created_at DESC LIMIT 1""",
                (issue_id, agent_id, exclude_task_id),
            ).fetchone()
        return _task(row)

    def has_pending(
        self, issue_id: str, agent_id: str, is_leader_task: Optional[bool] = None
    ) -> bool:
        query = """SELECT EXISTS(SELECT 1 FROM agent_task
            WHERE issue_id=%s AND agent_id=%s AND status IN ('queued','dispatched','running')"""
        params = [issue_id, agent_id]
        if is_leader_task is not None:
            query += " AND is_leader_task=%s"
            params.append(is_leader_task)
        query += ") AS present"
        with self.database.connection() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
        return bool(row["present"])

    @staticmethod
    def _values(task: Task):
        return (
            task.id,
            task.issue_id,
            task.agent_id,
            task.runtime_id,
            task.trigger_type,
            task.trigger_id,
            task.status.value,
            task.squad_id,
            task.is_leader_task,
            task.delegated_from_task_id,
            task.handoff_note,
            task.trigger_comment_id,
            task.merged_comment_ids,
            task.delivered_comment_ids,
            task.session_id,
            task.prior_session_id,
            task.branch_name,
            task.worktree_path,
            task.lease_expires_at,
            task.claimed_by,
            task.attempt,
            task.cancel_requested,
            task.output,
            task.error,
            task.created_at,
            task.updated_at,
        )


class PostgresEventRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def add(self, event: TaskEvent) -> TaskEvent:
        with self.database.connection() as connection:
            connection.execute(
                "INSERT INTO task_event(id, task_id, event_type, payload, created_at) VALUES (%s, %s, %s, %s, %s)",
                (
                    event.id,
                    event.task_id,
                    event.event_type,
                    json.dumps(event.payload),
                    event.created_at,
                ),
            )
        return event

    def list(self, task_id: Optional[str] = None) -> List[TaskEvent]:
        query = "SELECT * FROM task_event"
        params = ()
        if task_id is not None:
            query += " WHERE task_id=%s"
            params = (task_id,)
        query += " ORDER BY created_at"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            TaskEvent(
                id=row["id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                payload=row["payload"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def list_after(self, created_at, event_id: str) -> List[TaskEvent]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM task_event WHERE (created_at, id) > (%s, %s)
                ORDER BY created_at, id""",
                (created_at, event_id),
            ).fetchall()
        return [
            TaskEvent(
                id=row["id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                payload=row["payload"],
                created_at=row["created_at"],
            )
            for row in rows
        ]


class PostgresAgentRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def add(self, agent: Agent) -> Agent:
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO agent(id, name, runtime_id, provider, instructions, provider_config)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, runtime_id=EXCLUDED.runtime_id,
                    provider=EXCLUDED.provider, instructions=EXCLUDED.instructions,
                    provider_config=EXCLUDED.provider_config""",
                (
                    agent.id,
                    agent.name,
                    agent.runtime_id,
                    agent.provider,
                    agent.instructions,
                    json.dumps(agent.provider_config),
                ),
            )
        return agent

    def get(self, agent_id: str) -> Optional[Agent]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM agent WHERE id=%s", (agent_id,)).fetchone()
        return self._from_row(row) if row else None

    def list(self) -> List[Agent]:
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM agent ORDER BY id").fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row) -> Agent:
        return Agent(
            id=row["id"],
            name=row["name"],
            runtime_id=row["runtime_id"],
            provider=row["provider"],
            instructions=row["instructions"],
            provider_config=row["provider_config"],
        )


class PostgresRuntimeRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def add(self, runtime: Runtime) -> Runtime:
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO runtime(
                    id, name, max_slots, daemon_id, status, hostname, platform, last_seen_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name,
                    max_slots=EXCLUDED.max_slots, daemon_id=EXCLUDED.daemon_id,
                    status=EXCLUDED.status, hostname=EXCLUDED.hostname,
                    platform=EXCLUDED.platform, last_seen_at=EXCLUDED.last_seen_at""",
                (
                    runtime.id,
                    runtime.name,
                    runtime.max_slots,
                    runtime.daemon_id,
                    runtime.status,
                    runtime.hostname,
                    runtime.platform,
                    runtime.last_seen_at,
                ),
            )
        return runtime

    def get(self, runtime_id: str) -> Optional[Runtime]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM runtime WHERE id=%s", (runtime_id,)).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def list(self) -> List[Runtime]:
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM runtime ORDER BY id").fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row) -> Runtime:
        return Runtime(
            id=row["id"],
            name=row["name"],
            max_slots=row["max_slots"],
            daemon_id=row["daemon_id"],
            status=row["status"],
            hostname=row["hostname"],
            platform=row["platform"],
            last_seen_at=row["last_seen_at"],
        )


class PostgresRuntimeCapabilityRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def replace_for_runtime(
        self, runtime_id: str, capabilities: List[RuntimeCapability]
    ) -> List[RuntimeCapability]:
        with self.database.transaction():
            with self.database.connection() as connection:
                connection.execute(
                    "DELETE FROM runtime_capability WHERE runtime_id=%s", (runtime_id,)
                )
                for item in capabilities:
                    connection.execute(
                        """INSERT INTO runtime_capability(
                            runtime_id, provider, executable_path, version,
                            status, error, detected_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            item.runtime_id,
                            item.provider,
                            item.executable_path,
                            item.version,
                            item.status,
                            item.error,
                            item.detected_at,
                        ),
                    )
        return capabilities

    def list(self, runtime_id: Optional[str] = None) -> List[RuntimeCapability]:
        query = "SELECT * FROM runtime_capability"
        params = ()
        if runtime_id is not None:
            query += " WHERE runtime_id=%s"
            params = (runtime_id,)
        query += " ORDER BY runtime_id, provider"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, runtime_id: str, provider: str) -> Optional[RuntimeCapability]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_capability WHERE runtime_id=%s AND provider=%s",
                (runtime_id, provider),
            ).fetchone()
        return self._from_row(row) if row else None

    @staticmethod
    def _from_row(row) -> RuntimeCapability:
        return RuntimeCapability(
            runtime_id=row["runtime_id"],
            provider=row["provider"],
            executable_path=row["executable_path"],
            version=row["version"],
            status=row["status"],
            error=row["error"],
            detected_at=row["detected_at"],
        )


class PostgresProjectRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def add(self, project: Project) -> Project:
        with self.database.connection() as connection:
            row = connection.execute(
                """INSERT INTO project(id, name, workspace_id, runtime_id, local_path, execution_mode, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (workspace_id, runtime_id, local_path) DO UPDATE SET
                    name=EXCLUDED.name, execution_mode=EXCLUDED.execution_mode
                RETURNING *""",
                (
                    project.id,
                    project.name,
                    project.workspace_id,
                    project.runtime_id,
                    project.local_path,
                    project.execution_mode,
                    project.created_at,
                ),
            ).fetchone()
        return self._from_row(row)

    def save(self, project: Project) -> Project:
        with self.database.connection() as connection:
            row = connection.execute(
                """UPDATE project SET name=%s, execution_mode=%s
                WHERE id=%s RETURNING *""",
                (project.name, project.execution_mode, project.id),
            ).fetchone()
        if row is None:
            raise KeyError(project.id)
        return self._from_row(row)

    def get(self, project_id: str) -> Optional[Project]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM project WHERE id=%s", (project_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def find_by_path(
        self, workspace_id: str, runtime_id: str, local_path: str
    ) -> Optional[Project]:
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM project
                WHERE workspace_id=%s AND runtime_id=%s AND local_path=%s""",
                (workspace_id, runtime_id, local_path),
            ).fetchone()
        return self._from_row(row) if row else None

    def list(self, workspace_id: Optional[str] = None) -> List[Project]:
        query = "SELECT * FROM project"
        params = ()
        if workspace_id is not None:
            query += " WHERE workspace_id=%s"
            params = (workspace_id,)
        query += " ORDER BY name, created_at"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            workspace_id=row["workspace_id"],
            runtime_id=row["runtime_id"],
            local_path=row["local_path"],
            execution_mode=row["execution_mode"],
            created_at=row["created_at"],
        )


class PostgresSquadRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def add(self, squad: Squad) -> Squad:
        try:
            with self.database.connection() as connection:
                connection.execute(
                    """INSERT INTO squad(
                        id, name, workspace_id, leader_agent_id, description, instructions, created_at, updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        squad.id,
                        squad.name,
                        squad.workspace_id,
                        squad.leader_agent_id,
                        squad.description,
                        squad.instructions,
                        squad.created_at,
                        squad.updated_at,
                    ),
                )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ValueError(f"squad name already exists in workspace: {squad.name}") from exc
            raise
        return squad

    def save(self, squad: Squad) -> Squad:
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """UPDATE squad SET name=%s, leader_agent_id=%s, description=%s,
                    instructions=%s, updated_at=%s WHERE id=%s RETURNING id""",
                    (
                        squad.name,
                        squad.leader_agent_id,
                        squad.description,
                        squad.instructions,
                        squad.updated_at,
                        squad.id,
                    ),
                ).fetchone()
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ValueError(f"squad name already exists in workspace: {squad.name}") from exc
            raise
        if row is None:
            raise KeyError(squad.id)
        return squad

    def get(self, squad_id: str) -> Optional[Squad]:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM squad WHERE id=%s", (squad_id,)).fetchone()
        return self._squad(row) if row else None

    def list(self, workspace_id: Optional[str] = None) -> List[Squad]:
        query = "SELECT * FROM squad"
        params = ()
        if workspace_id is not None:
            query += " WHERE workspace_id=%s"
            params = (workspace_id,)
        query += " ORDER BY created_at"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._squad(row) for row in rows]

    def add_member(self, member: SquadMember) -> SquadMember:
        with self.database.connection() as connection:
            row = connection.execute(
                """INSERT INTO squad_member(id, squad_id, agent_id, role, capabilities, created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (squad_id, agent_id) DO UPDATE SET
                    role=EXCLUDED.role, capabilities=EXCLUDED.capabilities
                RETURNING *""",
                (
                    member.id,
                    member.squad_id,
                    member.agent_id,
                    member.role,
                    json.dumps(member.capabilities),
                    member.created_at,
                ),
            ).fetchone()
        return self._member(row)

    def list_members(self, squad_id: str) -> List[SquadMember]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM squad_member WHERE squad_id=%s ORDER BY created_at", (squad_id,)
            ).fetchall()
        return [self._member(row) for row in rows]

    def remove_member(self, squad_id: str, agent_id: str) -> bool:
        with self.database.connection() as connection:
            result = connection.execute(
                "DELETE FROM squad_member WHERE squad_id=%s AND agent_id=%s",
                (squad_id, agent_id),
            )
        return result.rowcount > 0

    @staticmethod
    def _squad(row) -> Squad:
        return Squad(
            id=row["id"],
            name=row["name"],
            workspace_id=row["workspace_id"],
            leader_agent_id=row["leader_agent_id"],
            description=row["description"],
            instructions=row["instructions"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _member(row) -> SquadMember:
        return SquadMember(
            id=row["id"],
            squad_id=row["squad_id"],
            agent_id=row["agent_id"],
            role=row["role"],
            capabilities=row["capabilities"],
            created_at=row["created_at"],
        )
