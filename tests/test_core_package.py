import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mica.api.app import create_app
from mica.application.services import InvalidStateError
from mica.bootstrap import build_memory_container
from mica.domain import Agent, AssigneeType, IssueStatus, Runtime, TaskEvent, TaskStatus
from mica.runtime.providers import StubProvider


class CoreApplicationTest(unittest.TestCase):
    def setUp(self):
        self.container = build_memory_container()

    def create_issue(self):
        return self.container.trigger_service.create_issue(
            title="修复订单导出异常",
            body="导出大量订单时会中断，请定位并给出修复。",
            workspace_id="workspace-demo",
            assignee_id="local-assistant",
        )

    def test_create_issue_enqueues_task_for_assigned_agent(self):
        issue = self.create_issue()

        tasks = self.container.tasks.list(issue.id)
        self.assertEqual(1, len(tasks))
        self.assertEqual(TaskStatus.QUEUED, tasks[0].status)
        self.assertEqual("local-assistant", tasks[0].agent_id)
        self.assertEqual("local", tasks[0].runtime_id)

    def test_unknown_agent_does_not_leave_orphan_issue(self):
        with self.assertRaises(LookupError):
            self.container.trigger_service.create_issue(
                title="无负责人任务",
                body="不应写入事项仓储。",
                workspace_id="workspace-demo",
                assignee_id="missing-agent",
            )

        self.assertEqual([], self.container.issues.list())

    def test_claim_is_atomic_across_workers(self):
        self.create_issue()
        claimed = []
        barrier = threading.Barrier(3)

        def claim(worker_id):
            barrier.wait()
            claimed.append(self.container.task_service.claim_next("local", worker_id))

        workers = [threading.Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()

        self.assertEqual(1, sum(task is not None for task in claimed))

    def test_daemon_completes_task_and_moves_issue_to_review(self):
        issue = self.create_issue()

        self.assertTrue(self.container.daemon.run_once())

        task = self.container.tasks.list(issue.id)[0]
        saved_issue = self.container.issue_service.get(issue.id)
        event_types = [event.event_type for event in self.container.events.list(task.id)]
        self.assertEqual(TaskStatus.COMPLETED, task.status)
        self.assertEqual(IssueStatus.IN_REVIEW, saved_issue.status)
        self.assertEqual(
            ["task.queued", "task.dispatched", "task.started", "agent.message", "task.completed"],
            event_types,
        )

        confirmed = self.container.issue_service.transition(issue.id, IssueStatus.DONE)
        self.assertEqual(IssueStatus.DONE, confirmed.status)

    def test_provider_failure_blocks_issue_and_records_error(self):
        self.container.providers.register("stub", StubProvider(fail_with="agent unavailable"))
        issue = self.create_issue()

        self.assertTrue(self.container.daemon.run_once())

        task = self.container.tasks.list(issue.id)[0]
        saved_issue = self.container.issue_service.get(issue.id)
        self.assertEqual(TaskStatus.FAILED, task.status)
        self.assertEqual("agent unavailable", task.error)
        self.assertEqual(IssueStatus.BLOCKED, saved_issue.status)

    def test_daemon_only_claims_tasks_for_its_runtime(self):
        self.container.runtimes.add(Runtime(id="remote", name="Remote Runtime"))
        self.container.agents.add(Agent(id="remote-agent", name="Remote", runtime_id="remote", provider="stub"))
        issue = self.container.trigger_service.create_issue(
            title="查询产品目录",
            body="返回符合约束的产品 JSON。",
            workspace_id="workspace-demo",
            assignee_id="remote-agent",
            assignee_type=AssigneeType.AGENT,
        )

        self.assertFalse(self.container.daemon.run_once())
        task = self.container.tasks.list(issue.id)[0]
        self.assertEqual(TaskStatus.QUEUED, task.status)

    def test_agent_configuration_cannot_change_while_task_is_active(self):
        self.create_issue()

        with patch.dict("os.environ", {"MICA_EMBEDDED_DAEMON": "0"}):
            with TestClient(create_app(self.container)) as client:
                response = client.post(
                    "/api/agents",
                    json={
                        "id": "local-assistant",
                        "name": "Moved Assistant",
                        "runtime_id": "local",
                        "provider": "claude",
                    },
                )

        self.assertEqual(409, response.status_code)
        self.assertEqual("stub", self.container.agents.get("local-assistant").provider)

    def test_invalid_task_transition_is_rejected(self):
        issue = self.create_issue()
        task = self.container.tasks.list(issue.id)[0]
        with self.assertRaises(InvalidStateError):
            self.container.task_service.complete(task.id, "cannot skip running")

    def test_event_timeline_hides_internal_claude_telemetry(self):
        issue = self.create_issue()
        task = self.container.tasks.list(issue.id)[0]
        self.container.events.add(
            TaskEvent(
                task_id=task.id,
                event_type="agent.system",
                payload={"subtype": "thinking_tokens"},
            )
        )
        self.container.events.add(
            TaskEvent(task_id=task.id, event_type="agent.text", payload={"text": "done"})
        )

        with patch.dict("os.environ", {"MICA_EMBEDDED_DAEMON": "0"}):
            with TestClient(create_app(self.container)) as client:
                response = client.get(
                    f"/api/tasks/{task.id}/events",
                    params={"include_internal": "false", "limit": 200},
                )

        assert response.status_code == 200
        assert [item["event_type"] for item in response.json()] == [
            "task.queued",
            "agent.text",
        ]

    def test_unknown_default_provider_fails_during_startup(self):
        with self.assertRaisesRegex(ValueError, "unknown default provider"):
            build_memory_container(default_provider="typo")

    def test_cancelling_issue_also_cancels_its_queued_tasks(self):
        issue = self.create_issue()

        cancelled = self.container.trigger_service.transition_issue(
            issue.id, IssueStatus.CANCELLED
        )

        self.assertEqual(IssueStatus.CANCELLED, cancelled.status)
        self.assertEqual(TaskStatus.CANCELLED, self.container.tasks.list(issue.id)[0].status)
        self.assertFalse(self.container.daemon.run_once())

    def test_issue_cannot_skip_human_review(self):
        issue = self.create_issue()
        with self.assertRaises(InvalidStateError):
            self.container.issue_service.transition(issue.id, IssueStatus.DONE)


if __name__ == "__main__":
    unittest.main()
