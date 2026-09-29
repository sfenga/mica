CREATE TABLE project (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    runtime_id TEXT NOT NULL REFERENCES runtime(id),
    local_path TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (workspace_id, runtime_id, local_path)
);

CREATE INDEX project_workspace_idx ON project(workspace_id, name);
