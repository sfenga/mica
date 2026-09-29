ALTER TABLE outbox_message DROP CONSTRAINT outbox_message_status_check;

ALTER TABLE outbox_message
    ADD CONSTRAINT outbox_message_status_check
    CHECK (status IN ('pending', 'sending', 'sent', 'failed'));
