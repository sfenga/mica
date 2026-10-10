DROP INDEX IF EXISTS agent_task_chat_session_idx;
DROP INDEX IF EXISTS chat_message_session_idx;

ALTER TABLE agent_task
    DROP COLUMN IF EXISTS reply_in_thread,
    DROP COLUMN IF EXISTS reply_to_message_id,
    DROP COLUMN IF EXISTS chat_input_task_id,
    DROP COLUMN IF EXISTS chat_session_id;

ALTER TABLE agent_task ALTER COLUMN issue_id SET NOT NULL;

DROP TABLE IF EXISTS chat_message;
DROP TABLE IF EXISTS channel_chat_binding;
DROP TABLE IF EXISTS chat_session;
