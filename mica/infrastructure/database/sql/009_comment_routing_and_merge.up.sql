ALTER TABLE issue_comment
    ADD COLUMN parent_comment_id TEXT REFERENCES issue_comment(id) ON DELETE SET NULL,
    ADD COLUMN reply_to_task_id TEXT REFERENCES agent_task(id) ON DELETE SET NULL,
    ADD COLUMN author_type TEXT NOT NULL DEFAULT 'user'
        CHECK (author_type IN ('user', 'agent', 'system')),
    ADD COLUMN source_task_id TEXT REFERENCES agent_task(id) ON DELETE SET NULL;

ALTER TABLE agent_task
    ADD COLUMN trigger_comment_id TEXT REFERENCES issue_comment(id) ON DELETE SET NULL,
    ADD COLUMN merged_comment_ids TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN delivered_comment_ids TEXT[] NOT NULL DEFAULT '{}';

UPDATE agent_task
SET trigger_comment_id = trigger_id
WHERE trigger_type = 'comment.created'
  AND EXISTS (SELECT 1 FROM issue_comment WHERE id = agent_task.trigger_id);

CREATE UNIQUE INDEX agent_task_one_queued_per_issue_agent_idx
    ON agent_task(issue_id, agent_id)
    WHERE status = 'queued';

CREATE INDEX issue_comment_parent_idx ON issue_comment(parent_comment_id);
