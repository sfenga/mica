DROP INDEX IF EXISTS agent_task_delegation_idx;
DROP INDEX IF EXISTS agent_task_squad_idx;
ALTER TABLE agent_task
    DROP COLUMN IF EXISTS handoff_note,
    DROP COLUMN IF EXISTS delegated_from_task_id,
    DROP COLUMN IF EXISTS is_leader_task,
    DROP COLUMN IF EXISTS squad_id;
DROP TABLE IF EXISTS squad_member;
DROP TABLE IF EXISTS squad;
