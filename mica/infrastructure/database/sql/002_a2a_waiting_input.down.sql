UPDATE agent_task
SET status = 'failed', error = COALESCE(error, 'input was still required during schema downgrade')
WHERE status = 'waiting_input';

ALTER TABLE agent_task DROP CONSTRAINT agent_task_status_check;

ALTER TABLE agent_task ADD CONSTRAINT agent_task_status_check CHECK (status IN (
    'queued', 'dispatched', 'running', 'completed', 'failed', 'cancelled'
));
