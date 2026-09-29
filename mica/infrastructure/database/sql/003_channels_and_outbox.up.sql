CREATE TABLE platform_user (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE external_identity (
    channel TEXT NOT NULL,
    tenant_key TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES platform_user(id),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (channel, tenant_key, external_user_id)
);

ALTER TABLE issue
    ADD COLUMN created_by_user_id TEXT REFERENCES platform_user(id);

CREATE TABLE external_event (
    channel TEXT NOT NULL,
    tenant_key TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    issue_id TEXT REFERENCES issue(id),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (channel, tenant_key, external_event_id)
);

CREATE TABLE issue_origin (
    issue_id TEXT PRIMARY KEY REFERENCES issue(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    tenant_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE outbox_message (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    destination TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'sent')),
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL,
    claimed_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (channel, source_event_id, message_type)
);

CREATE INDEX outbox_claim_idx
    ON outbox_message (available_at, created_at)
    WHERE status = 'pending';

CREATE INDEX issue_origin_channel_idx
    ON issue_origin (channel, tenant_key, conversation_id);
