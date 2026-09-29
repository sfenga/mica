CREATE TABLE runtime (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    max_slots INTEGER NOT NULL CHECK (max_slots > 0)
);

CREATE TABLE agent (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    runtime_id TEXT NOT NULL REFERENCES runtime(id),
    provider TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    provider_config JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE issue (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    assignee_type TEXT NOT NULL CHECK (assignee_type IN ('agent', 'squad')),
    assignee_id TEXT NOT NULL,
    project_path TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'backlog', 'todo', 'in_progress', 'in_review', 'done', 'blocked', 'cancelled'
    )),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE issue_comment (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    author_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE agent_task (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issue(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agent(id),
    runtime_id TEXT NOT NULL REFERENCES runtime(id),
    trigger_type TEXT NOT NULL,
    trigger_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'dispatched', 'running', 'completed', 'failed', 'cancelled'
    )),
    session_id TEXT,
    prior_session_id TEXT,
    branch_name TEXT,
    worktree_path TEXT,
    lease_expires_at TIMESTAMPTZ,
    claimed_by TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    output TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX agent_task_claim_idx
    ON agent_task(runtime_id, created_at)
    WHERE status = 'queued';

CREATE INDEX agent_task_expired_lease_idx
    ON agent_task(runtime_id, lease_expires_at)
    WHERE status IN ('dispatched', 'running');

CREATE INDEX agent_task_execution_history_idx
    ON agent_task(issue_id, agent_id, created_at DESC);

CREATE INDEX agent_task_active_assignment_idx
    ON agent_task(issue_id, agent_id)
    WHERE status IN ('dispatched', 'running');

CREATE TABLE task_event (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES agent_task(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX task_event_task_idx ON task_event(task_id, created_at);
