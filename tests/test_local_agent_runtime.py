import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

from mica.bootstrap import build_memory_container
from mica.domain import Agent, AssigneeType, Issue, IssueStatus, Runtime, TaskStatus, utc_now
from mica.runtime import WorkspaceManager, WorktreeManager
from mica.runtime.providers import (
    CliProvider,
    ProviderCancelled,
    ProviderRunResult,
    parse_cli_event,
)
from mica.runtime.providers.cli import (
    _claude_control_response,
    _claude_input_frame,
    _claude_launched_async_task,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_streaming_cli.py"


class RecordingProvider:
    def __init__(self):
        self.calls = []

    def run(self, agent, issue, task, on_event, workdir=None, resume_session_id=None, is_cancelled=None):
        self.calls.append((workdir, resume_session_id))
        on_event("agent.text", {"text": "done"})
        return ProviderRunResult(output="done", session_id=resume_session_id or "session-first")


class BlockingProvider:
    def __init__(self):
        self.started = threading.Event()

    def run(self, agent, issue, task, on_event, workdir=None, resume_session_id=None, is_cancelled=None):
        self.started.set()
        while True:
            if is_cancelled and is_cancelled():
                raise ProviderCancelled("cancelled")
            time.sleep(0.01)


class ConcurrencyProvider:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def run(self, agent, issue, task, on_event, workdir=None, resume_session_id=None, is_cancelled=None):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        time.sleep(0.15)
        with self.lock:
            self.active -= 1
        return ProviderRunResult(output="done", session_id=f"session-{task.id}")


class SlowProvider:
    def run(self, agent, issue, task, on_event, workdir=None, resume_session_id=None, is_cancelled=None):
        time.sleep(0.3)
        return ProviderRunResult(output="done", session_id=f"session-{task.id}")


def create_git_repository(path: str) -> None:
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "mica@example.test"], check=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "Mica Test"], check=True)
    Path(path, "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", path, "add", "README.md"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-q", "-m", "seed"], check=True)


class LocalAgentRuntimeTest(unittest.TestCase):
    def test_codex_and_claude_streams_are_normalized(self):
        codex_events, codex_final, codex_session = parse_cli_event(
            "codex", '{"type":"thread.started","thread_id":"thread-1"}'
        )
        self.assertEqual("thread-1", codex_session)
        self.assertEqual("agent.system", codex_events[0][0])
        _, claude_final, claude_session = parse_cli_event(
            "claude",
            '{"type":"result","is_error":false,"result":"ok","session_id":"session-1"}',
        )
        self.assertEqual("ok", claude_final)
        self.assertEqual("session-1", claude_session)
        self.assertIsNone(codex_final)

    def test_claude_internal_telemetry_is_not_persisted(self):
        for subtype in ("thinking_tokens", "hook_started", "hook_response"):
            events, final, session = parse_cli_event(
                "claude",
                f'{{"type":"system","subtype":"{subtype}","session_id":"session-1"}}',
            )
            self.assertEqual([], events)
            self.assertIsNone(final)
            self.assertEqual("session-1", session)

    def test_claude_protocol_auto_approves_tools_and_forces_foreground(self):
        request = json.dumps(
            {
                "type": "control_request",
                "request_id": "permission-1",
                "request": {
                    "subtype": "tool_use",
                    "tool_name": "Bash",
                    "input": {"command": "sleep 10", "run_in_background": True},
                },
            }
        )

        response = json.loads(_claude_control_response(request))
        decision = response["response"]["response"]

        self.assertEqual("allow", decision["behavior"])
        self.assertEqual("sleep 10", decision["updatedInput"]["command"])
        self.assertFalse(decision["updatedInput"]["run_in_background"])

    def test_claude_input_uses_stream_json_user_frame(self):
        frame = json.loads(_claude_input_frame("inspect this repository"))

        self.assertEqual("user", frame["type"])
        self.assertEqual("user", frame["message"]["role"])
        self.assertEqual("inspect this repository", frame["message"]["content"][0]["text"])

    def test_claude_async_tool_result_is_rejected(self):
        event = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": {"status": "async_launched"},
                        }
                    ]
                },
            }
        )

        self.assertTrue(_claude_launched_async_task(event))

    def test_claude_bidirectional_permission_flow_completes(self):
        with tempfile.TemporaryDirectory() as workdir:
            agent = Agent(id="a", name="Claude", runtime_id="local", provider="claude")
            container = build_memory_container(seed_defaults=False)
            issue = container.issues.add(self._issue(container, agent, workdir))
            task = container.task_service.enqueue(
                issue, agent.id, agent.runtime_id, "issue.created", issue.id
            )
            provider = CliProvider(
                "claude",
                command_prefix=[
                    sys.executable,
                    str(FIXTURE),
                    "claude",
                    "control-request",
                ],
            )

            result = provider.run(agent, issue, task, lambda *_: None, workdir=workdir)

        self.assertEqual("permission approved", result.output)
        self.assertEqual("claude-session-control", result.session_id)

    def test_claude_command_owns_non_interactive_permission_flags(self):
        provider = CliProvider("claude")
        agent = Agent(
            id="a",
            name="Claude",
            runtime_id="local",
            provider="claude",
            provider_config={"permission_mode": "plan"},
        )

        command = provider._command(agent, "prompt must use stdin", None)

        self.assertEqual("bypassPermissions", command[command.index("--permission-mode") + 1])
        self.assertEqual("stream-json", command[command.index("--input-format") + 1])
        self.assertEqual("AskUserQuestion", command[command.index("--disallowedTools") + 1])
        self.assertNotIn("prompt must use stdin", command)

    def test_claude_uses_last_assistant_text_when_result_event_is_missing(self):
        with tempfile.TemporaryDirectory() as workdir:
            agent = Agent(id="a", name="Claude", runtime_id="local", provider="claude")
            container = build_memory_container(seed_defaults=False)
            issue = container.issues.add(self._issue(container, agent, workdir))
            task = container.task_service.enqueue(
                issue, agent.id, agent.runtime_id, "issue.created", issue.id
            )
            provider = CliProvider(
                "claude",
                command_prefix=[
                    sys.executable,
                    str(FIXTURE),
                    "claude",
                    "assistant-only",
                ],
            )
            events = []

            result = provider.run(
                agent,
                issue,
                task,
                lambda event_type, payload: events.append((event_type, payload)),
                workdir=workdir,
            )

        self.assertEqual("claude streaming", result.output)
        self.assertEqual("claude-session-1", result.session_id)
        self.assertEqual(
            [("agent.system", {"subtype": "init"}), ("agent.text", {"text": "claude streaming"})],
            events,
        )

    def test_cli_provider_collects_final_output_and_session(self):
        with tempfile.TemporaryDirectory() as workdir:
            agent = Agent(id="a", name="Codex", runtime_id="local", provider="codex")
            container = build_memory_container(seed_defaults=False)
            issue = container.issues.add(
                self._issue(container, agent, workdir)
            )
            task = container.task_service.enqueue(issue, agent.id, agent.runtime_id, "issue.created", issue.id)
            provider = CliProvider(
                "codex",
                command_prefix=[sys.executable, str(FIXTURE), "codex", "complete"],
            )
            result = provider.run(agent, issue, task, lambda *_: None, workdir=workdir)
        self.assertEqual("codex complete", result.output)
        self.assertEqual("codex-session-1", result.session_id)

    def test_cli_provider_terminates_process_when_cancelled(self):
        with tempfile.TemporaryDirectory() as workdir:
            agent = Agent(id="a", name="Claude", runtime_id="local", provider="claude")
            container = build_memory_container(seed_defaults=False)
            issue = container.issues.add(self._issue(container, agent, workdir))
            task = container.task_service.enqueue(issue, agent.id, agent.runtime_id, "issue.created", issue.id)
            provider = CliProvider(
                "claude",
                command_prefix=[sys.executable, str(FIXTURE), "claude", "slow"],
            )
            started = time.monotonic()
            with self.assertRaises(ProviderCancelled):
                provider.run(
                    agent,
                    issue,
                    task,
                    lambda *_: None,
                    workdir=workdir,
                    is_cancelled=lambda: time.monotonic() - started > 0.2,
                )

    def test_follow_up_reuses_worktree_branch_and_session(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider()
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            container.daemon.worktrees = WorktreeManager(worktree_root)
            issue = container.trigger_service.create_issue(
                title="修复导出异常",
                body="修改代码并运行测试",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )
            self.assertTrue(container.daemon.run_once())
            first = container.tasks.list(issue.id)[0]
            container.trigger_service.add_comment(issue.id, "补充大数据量测试", "user-1")
            self.assertTrue(container.daemon.run_once())
            second = container.tasks.list(issue.id)[1]

            self.assertEqual(first.branch_name, second.branch_name)
            self.assertEqual(first.worktree_path, second.worktree_path)
            self.assertEqual(first.session_id, second.prior_session_id)
            self.assertEqual(first.session_id, provider.calls[1][1])
            self.assertTrue(Path(second.worktree_path).is_dir())

    def test_project_can_run_in_place_without_creating_a_worktree(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as workspace_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider()
            container.providers.register("codex", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="codex")
            )
            container.daemon.workspaces = WorkspaceManager(workspace_root)
            issue = container.trigger_service.create_issue(
                title="阅读调用链",
                body="只分析代码",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
                project_execution_mode="in_place",
            )

            self.assertTrue(container.daemon.run_once())
            task = container.tasks.list(issue.id)[0]
            self.assertEqual(source, provider.calls[0][0])
            self.assertIsNone(task.worktree_path)
            self.assertIsNone(task.branch_name)

    def test_local_agent_without_project_gets_an_empty_managed_workspace(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            container = build_memory_container()
            provider = RecordingProvider()
            container.providers.register("codex", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="codex")
            )
            container.daemon.workspaces = WorkspaceManager(workspace_root)
            container.trigger_service.create_issue(
                title="通用问题",
                body="解释一个概念",
                workspace_id="demo",
                assignee_id="local-assistant",
            )

            self.assertTrue(container.daemon.run_once())
            workdir = Path(provider.calls[0][0])
            self.assertTrue(workdir.is_dir())
            self.assertTrue(workdir.is_relative_to(Path(workspace_root).resolve()))
            self.assertEqual([], list(workdir.iterdir()))

    def test_in_place_tasks_on_same_project_are_serialized(self):
        with tempfile.TemporaryDirectory() as source:
            create_git_repository(source)
            container = build_memory_container()
            provider = ConcurrencyProvider()
            container.providers.register("concurrent", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="concurrent")
            )
            container.slots.configure("local", 2)
            for number in range(2):
                container.trigger_service.create_issue(
                    title=f"read-{number}",
                    body="inspect",
                    workspace_id="demo",
                    assignee_id="local-assistant",
                    project_path=source,
                    project_execution_mode="in_place",
                )
            container.worker.start()
            container.worker.wake()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if all(task.status == TaskStatus.COMPLETED for task in container.tasks.list()):
                    break
                time.sleep(0.02)
            container.worker.stop()

            self.assertEqual(1, provider.maximum)
            self.assertTrue(all(task.status == TaskStatus.COMPLETED for task in container.tasks.list()))

    def test_daemon_recovers_expired_lease(self):
        container = build_memory_container()
        issue = container.trigger_service.create_issue(
            title="recover", body="recover", workspace_id="demo", assignee_id="local-assistant"
        )
        claimed = container.task_service.claim_next("local", "dead-daemon", lease_seconds=1)
        claimed.lease_expires_at = utc_now() - timedelta(seconds=1)
        container.tasks.save(claimed)

        self.assertTrue(container.daemon.run_once())

        recovered = container.tasks.list(issue.id)[0]
        self.assertEqual(TaskStatus.COMPLETED, recovered.status)
        self.assertEqual(2, recovered.attempt)
        self.assertIn("task.recovered", [event.event_type for event in container.events.list(recovered.id)])

    def test_running_task_can_be_cancelled(self):
        container = build_memory_container()
        provider = BlockingProvider()
        container.providers.register("blocking", provider)
        container.agents.add(
            Agent(id="local-assistant", name="Local", runtime_id="local", provider="blocking")
        )
        issue = container.trigger_service.create_issue(
            title="cancel", body="cancel", workspace_id="demo", assignee_id="local-assistant"
        )
        task = container.tasks.list(issue.id)[0]
        worker = threading.Thread(target=container.daemon.run_once)
        worker.start()
        self.assertTrue(provider.started.wait(timeout=2))
        container.task_service.request_cancel(task.id)
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(TaskStatus.CANCELLED, container.task_service.get(task.id).status)
        self.assertEqual(IssueStatus.CANCELLED, container.issue_service.get(issue.id).status)

    def test_heartbeat_renewal_errors_stop_execution_at_lease_deadline(self):
        container = build_memory_container()
        provider = BlockingProvider()
        container.providers.register("blocking", provider)
        container.agents.add(
            Agent(id="local-assistant", name="Local", runtime_id="local", provider="blocking")
        )
        container.daemon.lease_seconds = 1
        container.daemon.heartbeat_seconds = 0.05

        def always_raise(task_id, worker_id, lease_seconds=120):
            raise RuntimeError("database unavailable")

        container.task_service.renew_lease = always_raise
        issue = container.trigger_service.create_issue(
            title="heartbeat", body="heartbeat", workspace_id="demo", assignee_id="local-assistant"
        )
        task = container.tasks.list(issue.id)[0]
        worker = threading.Thread(target=container.daemon.run_once)
        worker.start()
        self.assertTrue(provider.started.wait(timeout=2))
        with self.assertLogs("mica.runtime.daemon", level="ERROR"):
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        events = [event.event_type for event in container.events.list(task.id)]
        self.assertIn("task.interrupted", events)
        self.assertNotIn("task.completed", events)
        self.assertEqual(TaskStatus.RUNNING, container.task_service.get(task.id).status)

    def test_transient_renewal_error_does_not_interrupt_execution(self):
        container = build_memory_container()
        container.providers.register("slow", SlowProvider())
        container.agents.add(
            Agent(id="local-assistant", name="Local", runtime_id="local", provider="slow")
        )
        container.daemon.lease_seconds = 2
        container.daemon.heartbeat_seconds = 0.05
        original = container.task_service.renew_lease
        calls = {"count": 0}

        def flaky(task_id, worker_id, lease_seconds=120):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("database blip")
            return original(task_id, worker_id, lease_seconds)

        container.task_service.renew_lease = flaky
        issue = container.trigger_service.create_issue(
            title="transient", body="transient", workspace_id="demo", assignee_id="local-assistant"
        )

        self.assertTrue(container.daemon.run_once())

        task = container.tasks.list(issue.id)[0]
        self.assertEqual(TaskStatus.COMPLETED, task.status)
        self.assertGreaterEqual(calls["count"], 2)
        events = [event.event_type for event in container.events.list(task.id)]
        self.assertNotIn("task.interrupted", events)

    def test_worker_uses_all_configured_runtime_slots(self):
        container = build_memory_container()
        provider = ConcurrencyProvider()
        container.providers.register("concurrent", provider)
        container.agents.add(
            Agent(id="local-assistant", name="Local", runtime_id="local", provider="concurrent")
        )
        container.slots.configure("local", 2)
        for number in range(2):
            container.trigger_service.create_issue(
                title=f"task-{number}",
                body="run",
                workspace_id="demo",
                assignee_id="local-assistant",
            )
        container.worker.start()
        container.worker.wake()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if all(task.status == TaskStatus.COMPLETED for task in container.tasks.list()):
                break
            time.sleep(0.02)
        container.worker.stop()

        self.assertEqual(2, provider.maximum)
        self.assertTrue(all(task.status == TaskStatus.COMPLETED for task in container.tasks.list()))

    @staticmethod
    def _issue(container, agent, project_path):
        container.runtimes.add(Runtime(id="local", name="Local"))
        container.agents.add(agent)
        return Issue(
            title="task",
            body="body",
            workspace_id="demo",
            assignee_type=AssigneeType.AGENT,
            assignee_id=agent.id,
            project_path=project_path,
        )


if __name__ == "__main__":
    unittest.main()
