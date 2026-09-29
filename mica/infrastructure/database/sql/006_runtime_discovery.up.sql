ALTER TABLE runtime
    ADD COLUMN daemon_id TEXT NOT NULL DEFAULT 'daemon-local-1',
    ADD COLUMN status TEXT NOT NULL DEFAULT 'offline',
    ADD COLUMN hostname TEXT NOT NULL DEFAULT '',
    ADD COLUMN platform TEXT NOT NULL DEFAULT '',
    ADD COLUMN last_seen_at TIMESTAMPTZ;

CREATE TABLE runtime_capability (
    runtime_id TEXT NOT NULL REFERENCES runtime(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    executable_path TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'available',
    error TEXT,
    detected_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (runtime_id, provider)
);
