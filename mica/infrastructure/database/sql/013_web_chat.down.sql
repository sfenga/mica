DROP TABLE chat_read_cursor;
ALTER TABLE chat_session DROP COLUMN pinned;
-- Nullable external IDs are required by assistant messages; retain this fix.
