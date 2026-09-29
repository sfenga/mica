CREATE TABLE squad (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    leader_agent_id TEXT NOT NULL REFERENCES agent(id),
    description TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (workspace_id, name)
);

CREATE TABLE squad_member (
    id TEXT PRIMARY KEY,
    squad_id TEXT NOT NULL REFERENCES squad(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agent(id),
    role TEXT NOT NULL DEFAULT '',
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (squad_id, agent_id)
);

ALTER TABLE agent_task
    ADD COLUMN squad_id TEXT REFERENCES squad(id),
    ADD COLUMN is_leader_task BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN delegated_from_task_id TEXT REFERENCES agent_task(id),
    ADD COLUMN handoff_note TEXT;

CREATE INDEX agent_task_squad_idx ON agent_task(squad_id, created_at)
    WHERE squad_id IS NOT NULL;

CREATE INDEX agent_task_delegation_idx ON agent_task(delegated_from_task_id, created_at)
    WHERE delegated_from_task_id IS NOT NULL;
