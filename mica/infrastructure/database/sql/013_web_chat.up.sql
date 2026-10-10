ALTER TABLE chat_message ALTER COLUMN external_message_id DROP NOT NULL;
ALTER TABLE chat_session ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE chat_read_cursor (
    chat_session_id TEXT NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    read_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chat_session_id, user_id)
);
CREATE INDEX chat_session_workspace_activity_idx ON chat_session(workspace_id, updated_at DESC);
