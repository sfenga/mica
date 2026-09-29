ALTER TABLE project
    ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'worktree'
    CHECK (execution_mode IN ('in_place', 'worktree'));

ALTER TABLE issue
    ADD COLUMN project_execution_mode TEXT
    CHECK (project_execution_mode IN ('in_place', 'worktree'));

UPDATE issue
SET project_execution_mode = 'worktree'
WHERE project_path IS NOT NULL;
