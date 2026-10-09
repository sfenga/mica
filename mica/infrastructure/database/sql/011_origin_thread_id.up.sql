-- KI-008: persist the Lark topic (thread_id) of the originating message so
-- outbound replies can stay inside the topic and future KI-001 chat sessions
-- can isolate by (chat_id, thread_id) without a second migration.
ALTER TABLE issue_origin
    ADD COLUMN thread_id TEXT;
