DROP INDEX IF EXISTS issue_origin_channel_idx;
DROP INDEX IF EXISTS outbox_claim_idx;
DROP TABLE IF EXISTS outbox_message;
DROP TABLE IF EXISTS issue_origin;
DROP TABLE IF EXISTS external_event;
ALTER TABLE issue DROP COLUMN IF EXISTS created_by_user_id;
DROP TABLE IF EXISTS external_identity;
DROP TABLE IF EXISTS platform_user;
