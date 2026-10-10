import unittest
from queue import Empty
from pathlib import Path

from mica.application.services import InvalidStateError
from mica.bootstrap import build_memory_container
from mica.domain import TaskEvent, TaskStatus, utc_now
from mica.realtime import DatabaseEventRelay, EventBus


MIGRATION = (
    Path(__file__).parents[1]
    / "mica"
    / "infrastructure"
    / "database"
    / "sql"
    / "001_core.up.sql"
)
WAITING_INPUT_MIGRATION = (
    Path(__file__).parents[1]
    / "mica"
    / "infrastructure"
    / "database"
    / "sql"
    / "002_a2a_waiting_input.up.sql"
)
CHANNEL_MIGRATION = (
    Path(__file__).parents[1]
    / "mica"
    / "infrastructure"
    / "database"
    / "sql"
    / "003_channels_and_outbox.up.sql"
)
SQUAD_MIGRATION = (
    Path(__file__).parents[1]
    / "mica"
    / "infrastructure"
    / "database"
    / "sql"
    / "004_squads.up.sql"
)
OUTBOX_FAILURE_MIGRATION = (
    Path(__file__).parents[1]
    / "mica"
    / "infrastructure"
    / "database"
    / "sql"
    / "005_outbox_failure.up.sql"
)
WORKTREE_DELIVERY_MIGRATION = (
    Path(__file__).parents[1]
    / "mica"
    / "infrastructure"
    / "database"
    / "sql"
    / "010_worktree_delivery.up.sql"
)


class DatabaseContractTest(unittest.TestCase):
    def test_claim_query_uses_postgres_skip_locked(self):
        repository_source = (
            Path(__file__).parents[1]
            / "mica"
            / "infrastructure"
            / "database"
            / "repositories.py"
        ).read_text(encoding="utf-8")
        self.assertIn("FOR UPDATE OF queued SKIP LOCKED", repository_source)
        self.assertIn("make_interval(secs => %s)", repository_source)
        self.assertIn("assigned_agent.runtime_id=queued.runtime_id", repository_source)
        self.assertIn("owner_issue.status NOT IN ('done', 'cancelled')", repository_source)

    def test_migration_has_claim_and_lease_indexes(self):
        migration = MIGRATION.read_text(encoding="utf-8")
        self.assertIn("agent_task_claim_idx", migration)
        self.assertIn("agent_task_expired_lease_idx", migration)

    def test_a2a_migration_adds_waiting_input_status(self):
        migration = WAITING_INPUT_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("waiting_input", migration)
        self.assertIn("agent_task_status_check", migration)

    def test_parallel_processes_serialize_schema_migrations(self):
        migration_runner = (
            Path(__file__).parents[1]
            / "mica"
            / "infrastructure"
            / "database"
            / "migrations.py"
        ).read_text(encoding="utf-8")
        self.assertIn("pg_advisory_xact_lock", migration_runner)

    def test_channel_migration_has_idempotency_and_outbox_contracts(self):
        migration = CHANNEL_MIGRATION.read_text(encoding="utf-8")
        repository = (
            Path(__file__).parents[1]
            / "mica"
            / "infrastructure"
            / "database"
            / "channel_repository.py"
        ).read_text(encoding="utf-8")
        self.assertIn("PRIMARY KEY (channel, tenant_key, external_event_id)", migration)
        self.assertIn("UNIQUE (channel, source_event_id, message_type)", migration)
        self.assertIn("FOR UPDATE SKIP LOCKED", repository)

    def test_squad_migration_stamps_task_provenance(self):
        migration = SQUAD_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("leader_agent_id", migration)
        self.assertIn("is_leader_task", migration)
        self.assertIn("delegated_from_task_id", migration)

    def test_outbox_has_terminal_failure_state(self):
        migration = OUTBOX_FAILURE_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("'failed'", migration)

    def test_worktree_delivery_migration_records_branch_tip(self):
        migration = WORKTREE_DELIVERY_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("recorded_tip", migration)
        self.assertIn("agent_task", migration)

    def test_stale_attempt_cannot_finish_reclaimed_task(self):
        container = build_memory_container()
        container.trigger_service.create_issue(
            title="fence", body="fence", workspace_id="demo", assignee_id="local-assistant"
        )
        first = container.task_service.claim_next("local", "daemon-old", lease_seconds=1)
        first.lease_expires_at = utc_now().replace(year=2000)
        container.tasks.save(first)
        container.task_service.recover_expired("local")
        second = container.task_service.claim_next("local", "daemon-new", lease_seconds=120)
        container.task_service.start(second.id, "daemon-new", second.attempt)

        with self.assertRaises(InvalidStateError):
            container.task_service.complete(
                first.id,
                "late result",
                worker_id="daemon-old",
                attempt=first.attempt,
            )

        current = container.task_service.get(second.id)
        self.assertEqual(TaskStatus.RUNNING, current.status)
        self.assertEqual("daemon-new", current.claimed_by)
        self.assertEqual(2, current.attempt)

    def test_follow_up_for_same_issue_waits_for_active_task(self):
        container = build_memory_container()
        issue = container.trigger_service.create_issue(
            title="serial", body="serial", workspace_id="demo", assignee_id="local-assistant"
        )
        container.trigger_service.add_comment(issue.id, "follow-up", "user-1")
        first = container.task_service.claim_next("local", "daemon-1")

        self.assertIsNotNone(first)
        self.assertIsNone(container.task_service.claim_next("local", "daemon-2"))

    def test_database_event_relay_forwards_daemon_events(self):
        container = build_memory_container()
        bus = EventBus()
        relay = DatabaseEventRelay(container.events, bus, poll_interval=0.01)
        channel = bus.subscribe("task-external")
        relay.start()
        try:
            container.events.add(TaskEvent(task_id="task-external", event_type="agent.text"))
            event = channel.get(timeout=1)
        except Empty as exc:
            self.fail(f"event relay did not publish: {exc}")
        finally:
            relay.stop()
        self.assertEqual("agent.text", event.event_type)


if __name__ == "__main__":
    unittest.main()
