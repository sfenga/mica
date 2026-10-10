-- KI-001: decouple Feishu conversations from Issues. Ordinary messenger
-- traffic becomes Chat (chat_session + chat_message + channel_chat_binding);
-- agent_task learns nullable chat fields so chat runs share the issue queue.

CREATE TABLE chat_session (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    agent_id TEXT NOT NULL REFERENCES agent(id),
    creator_user_id TEXT NOT NULL,
    session_id TEXT,
    runtime_id TEXT,
    work_dir TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- thread_id uses '' (not NULL) for non-topic chats so the composite UNIQUE
-- constraint is total: PostgreSQL treats NULLs as distinct, which would let
-- two bindings exist for the same plain group chat and break the
-- create/re-read protocol in ChatRepository.ensure_session.
CREATE TABLE channel_chat_binding (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    tenant_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    chat_session_id TEXT NOT NULL UNIQUE REFERENCES chat_session(id) ON DELETE CASCADE,
    last_message_id TEXT,
    last_thread_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (channel, tenant_key, conversation_id, thread_id)
);

CREATE TABLE chat_message (
    id TEXT PRIMARY KEY,
    chat_session_id TEXT NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    body TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    parent_external_message_id TEXT,
    -- Consumed-by for user rows, produced-by for assistant rows. No FK:
    -- batches skipped for a missing agent are tagged with a synthetic
    -- 'skipped_*' id that has no task row.
    task_id TEXT,
    message_kind TEXT NOT NULL DEFAULT 'text'
        CHECK (message_kind IN ('text', 'channel_command')),
    channel_ingested BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    -- Inbound dedup: the same Feishu message can only be appended to a session
    -- once. Assistant rows have no external id, and NULLs stay distinct under
    -- UNIQUE, so reply rows are unaffected.
    UNIQUE (chat_session_id, external_message_id)
);

ALTER TABLE agent_task ALTER COLUMN issue_id DROP NOT NULL;

ALTER TABLE agent_task
    ADD COLUMN chat_session_id TEXT REFERENCES chat_session(id) ON DELETE CASCADE,
    ADD COLUMN chat_input_task_id TEXT,
    ADD COLUMN reply_to_message_id TEXT,
    ADD COLUMN reply_in_thread BOOLEAN;

CREATE INDEX chat_message_session_idx ON chat_message(chat_session_id, created_at);
CREATE INDEX agent_task_chat_session_idx ON agent_task(chat_session_id);
