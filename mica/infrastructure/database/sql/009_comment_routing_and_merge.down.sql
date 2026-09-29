DROP INDEX issue_comment_parent_idx;
DROP INDEX agent_task_one_queued_per_issue_agent_idx;

ALTER TABLE agent_task
    DROP COLUMN delivered_comment_ids,
    DROP COLUMN merged_comment_ids,
    DROP COLUMN trigger_comment_id;

ALTER TABLE issue_comment
    DROP COLUMN source_task_id,
    DROP COLUMN author_type,
    DROP COLUMN reply_to_task_id,
    DROP COLUMN parent_comment_id;
