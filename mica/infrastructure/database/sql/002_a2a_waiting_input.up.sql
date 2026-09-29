ALTER TABLE agent_task DROP CONSTRAINT agent_task_status_check;

ALTER TABLE agent_task ADD CONSTRAINT agent_task_status_check CHECK (status IN (
    'queued', 'dispatched', 'running', 'waiting_input', 'completed', 'failed', 'cancelled'
));
