DROP TABLE IF EXISTS runtime_capability;
ALTER TABLE runtime
    DROP COLUMN IF EXISTS last_seen_at,
    DROP COLUMN IF EXISTS platform,
    DROP COLUMN IF EXISTS hostname,
    DROP COLUMN IF EXISTS status,
    DROP COLUMN IF EXISTS daemon_id;
