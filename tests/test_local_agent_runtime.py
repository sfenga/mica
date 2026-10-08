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
from mica.runtime.daemon import Daemon, RuntimeSlotPool
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
    def __init__(self, write_artifact: bool = False, edit_file: str = ""):
        self.calls = []
        self.write_artifact = write_artifact
        self.edit_file = edit_file

    def run(
        self,
        agent,
        issue,
        task,
        on_event,
        workdir=None,
        resume_session_id=None,
        is_cancelled=None,
        replay_conflicts=None,
    ):
        listing = sorted(p.name for p in Path(workdir).iterdir()) if workdir else []
        self.calls.append(
            {
                "workdir": workdir,
                "resume_session_id": resume_session_id,
                "listing": listing,
                "replay_conflicts": list(replay_conflicts or []),
            }
        )
        if self.write_artifact and workdir:
            Path(workdir, f"artifact-{len(self.calls)}.txt").write_text("work", encoding="utf-8")
        if self.edit_file and workdir:
            target = Path(workdir, self.edit_file)
            target.write_text(
                target.read_text(encoding="utf-8") + f"agent turn {len(self.calls)}\n",
                encoding="utf-8",
            )
        on_event("agent.text", {"text": "done"})
        return ProviderRunResult(output="done", session_id=resume_session_id or "session-first")


class FailOnceProvider:
    """Fails the first run (infrastructure-style error), then delegates."""

    def __init__(self, inner: RecordingProvider):
        self.inner = inner
        self.failures = 0

    def run(self, *args, **kwargs):
        if self.failures == 0:
            self.failures += 1
            raise RuntimeError("provider exploded before doing any work")
        return self.inner.run(*args, **kwargs)


class BlockingProvider:
    def __init__(self):
        self.started = threading.Event()

    def run(
        self,
        agent,
        issue,
        task,
        on_event,
        workdir=None,
        resume_session_id=None,
        is_cancelled=None,
        replay_conflicts=None,
    ):
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

    def run(
        self,
        agent,
        issue,
        task,
        on_event,
        workdir=None,
        resume_session_id=None,
        is_cancelled=None,
        replay_conflicts=None,
    ):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        time.sleep(0.15)
        with self.lock:
            self.active -= 1
        return ProviderRunResult(output="done", session_id=f"session-{task.id}")


class SlowProvider:
    def run(
        self,
        agent,
        issue,
        task,
        on_event,
        workdir=None,
        resume_session_id=None,
        is_cancelled=None,
        replay_conflicts=None,
    ):
        time.sleep(0.3)
        return ProviderRunResult(output="done", session_id=f"session-{task.id}")


class SlowWorktrees:
    """Delays WorktreeManager.prepare to simulate a slow repository setup."""

    def __init__(self, inner: WorktreeManager, delay: float) -> None:
        self.inner = inner
        self.delay = delay

    def prepare(self, *args, **kwargs):
        time.sleep(self.delay)
        return self.inner.prepare(*args, **kwargs)

    def finalize(self, *args, **kwargs):
        return self.inner.finalize(*args, **kwargs)

    def release(self, *args, **kwargs):
        return self.inner.release(*args, **kwargs)

    def sweep(self, *args, **kwargs):
        return self.inner.sweep(*args, **kwargs)


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

    def test_follow_up_continues_branch_and_session_with_a_fresh_worktree(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider(write_artifact=True)
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

            # The branch and the session continue; the worktree does not.
            self.assertEqual(first.branch_name, second.branch_name)
            self.assertNotEqual(first.worktree_path, second.worktree_path)
            self.assertEqual(first.session_id, second.prior_session_id)
            self.assertEqual(first.session_id, provider.calls[1]["resume_session_id"])
            self.assertIsNotNone(first.recorded_tip)
            self.assertIsNotNone(second.recorded_tip)
            # Both worktrees are finalized away; the second turn saw the first
            # turn's delivered file through the branch.
            self.assertFalse(Path(first.worktree_path).exists())
            self.assertFalse(Path(second.worktree_path).exists())
            self.assertIn("artifact-1.txt", provider.calls[1]["listing"])
            # The delivered branch carries both turns' work in the user repo.
            files = subprocess.run(
                ["git", "-C", source, "ls-tree", "--name-only", first.branch_name],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertIn("artifact-1.txt", files)
            self.assertIn("artifact-2.txt", files)

    def test_dirty_source_repo_is_snapshotted_into_the_worktree(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            Path(source, "README.md").write_text("seed\nuser edit\n", encoding="utf-8")
            Path(source, "scratch.txt").write_text("uncommitted", encoding="utf-8")
            container = build_memory_container()
            provider = RecordingProvider()
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            container.daemon.worktrees = WorktreeManager(worktree_root)
            issue = container.trigger_service.create_issue(
                title="dirty repo",
                body="review the current state",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )

            self.assertTrue(container.daemon.run_once())

            # The agent saw the user's uncommitted state...
            self.assertIn("scratch.txt", provider.calls[0]["listing"])
            # ...and the user's own directory was never written to.
            status = subprocess.run(
                ["git", "-C", source, "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("scratch.txt", status)
            self.assertIn("README.md", status)

    def test_merged_branch_resets_to_latest_head(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider(write_artifact=True)
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            container.daemon.worktrees = WorktreeManager(worktree_root)
            issue = container.trigger_service.create_issue(
                title="two turns",
                body="turn one delivers",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )
            self.assertTrue(container.daemon.run_once())
            first = container.tasks.list(issue.id)[0]

            # The user merges the delivered branch and advances main.
            subprocess.run(
                ["git", "-C", source, "merge", "-q", "--no-edit", first.branch_name], check=True
            )
            Path(source, "main-progress.txt").write_text("new", encoding="utf-8")
            subprocess.run(["git", "-C", source, "add", "main-progress.txt"], check=True)
            subprocess.run(
                ["git", "-C", source, "commit", "-q", "-m", "main moves on"], check=True
            )

            container.trigger_service.add_comment(issue.id, "继续", "user-1")
            self.assertTrue(container.daemon.run_once())
            second = container.tasks.list(issue.id)[1]

            # Same branch, but rebased onto the latest HEAD: the second turn
            # sees the merged work and the new main commit.
            self.assertEqual(first.branch_name, second.branch_name)
            self.assertIn("main-progress.txt", provider.calls[1]["listing"])
            self.assertIn("artifact-1.txt", provider.calls[1]["listing"])
            tip = subprocess.run(
                ["git", "-C", source, "rev-parse", second.branch_name],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            head = subprocess.run(
                ["git", "-C", source, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            is_descendant = subprocess.run(
                ["git", "-C", source, "merge-base", "--is-ancestor", head, tip],
                check=False,
            ).returncode
            self.assertEqual(0, is_descendant)

    def test_conflicting_user_edits_block_delivery_and_keep_the_worktree(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider(edit_file="README.md")
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            container.daemon.worktrees = WorktreeManager(worktree_root)
            issue = container.trigger_service.create_issue(
                title="conflict turn",
                body="turn one edits README",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )
            self.assertTrue(container.daemon.run_once())
            first = container.tasks.list(issue.id)[0]
            self.assertEqual(TaskStatus.COMPLETED, first.status)

            # The agent's first turn rewrote README; the user then edited the
            # same file in their own directory without committing.
            Path(source, "README.md").write_text("user version\n", encoding="utf-8")

            container.trigger_service.add_comment(issue.id, "继续", "user-1")
            self.assertTrue(container.daemon.run_once())
            second = container.tasks.list(issue.id)[1]

            # The conflict reached the agent instead of being silently dropped...
            self.assertIn("README.md", provider.calls[1]["replay_conflicts"])
            # ...and finalize refused to deliver while it stayed unresolved.
            self.assertEqual(TaskStatus.FAILED, second.status)
            self.assertIn("unresolved merge", second.error or "")
            preserved = Path(second.worktree_path).with_name("worktree-preserved")
            self.assertTrue(preserved.is_dir())
            self.assertEqual("user version\n", Path(source, "README.md").read_text())

    def test_read_only_task_leaves_no_branch_behind(self):
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
                title="read only",
                body="just look",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )

            self.assertTrue(container.daemon.run_once())

            branches = subprocess.run(
                ["git", "-C", source, "branch", "--list", "mica/*"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual("", branches.strip())
            self.assertEqual([], list(Path(worktree_root).iterdir()))

    def test_branch_without_checkpoint_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            # A branch named like ours but with no delivery record: a legacy
            # branch from the durable-worktree era, or the user's own.
            legacy = WorktreeManager._branch_name("unused", "unused")
            container = build_memory_container()
            provider = RecordingProvider(write_artifact=True)
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            container.daemon.worktrees = WorktreeManager(worktree_root)
            issue = container.trigger_service.create_issue(
                title="legacy branch",
                body="a branch with this name already exists",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )
            expected = WorktreeManager._branch_name(issue.id, "local-assistant")
            subprocess.run(["git", "-C", source, "branch", expected], check=True)

            self.assertTrue(container.daemon.run_once())
            task = container.tasks.list(issue.id)[0]

            self.assertTrue(task.branch_name.startswith(expected + "-"))
            self.assertNotEqual(legacy, task.branch_name)

    def test_deleted_and_recreated_branch_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider(write_artifact=True)
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            container.daemon.worktrees = WorktreeManager(worktree_root)
            issue = container.trigger_service.create_issue(
                title="tampered branch",
                body="turn one",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )
            self.assertTrue(container.daemon.run_once())
            first = container.tasks.list(issue.id)[0]

            # The user deletes the delivered branch and recreates the name on
            # unrelated content.
            subprocess.run(["git", "-C", source, "branch", "-D", first.branch_name], check=True)
            Path(source, "unrelated.txt").write_text("theirs", encoding="utf-8")
            subprocess.run(["git", "-C", source, "add", "unrelated.txt"], check=True)
            subprocess.run(
                ["git", "-C", source, "checkout", "-q", "-b", first.branch_name], check=True
            )
            subprocess.run(
                ["git", "-C", source, "commit", "-q", "-m", "user's own branch"], check=True
            )
            subprocess.run(["git", "-C", source, "checkout", "-q", "-"], check=True)

            container.trigger_service.add_comment(issue.id, "继续", "user-1")
            self.assertTrue(container.daemon.run_once())
            second = container.tasks.list(issue.id)[1]

            self.assertNotEqual(first.branch_name, second.branch_name)
            self.assertTrue(second.branch_name.startswith(first.branch_name + "-"))

    def test_cancelled_task_preserves_files_and_frees_the_branch(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = BlockingProvider()
            container.providers.register("blocking", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="blocking")
            )
            container.daemon.worktrees = WorktreeManager(worktree_root)
            issue = container.trigger_service.create_issue(
                title="cancel me",
                body="blocking",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )
            task = container.tasks.list(issue.id)[0]
            worker = threading.Thread(target=container.daemon.run_once)
            worker.start()
            self.assertTrue(provider.started.wait(timeout=2))
            container.task_service.request_cancel(task.id)
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            task = container.task_service.get(task.id)
            self.assertEqual(TaskStatus.CANCELLED, task.status)
            # Files preserved for inspection, branch registration released.
            env_root = Path(task.worktree_path).parent
            self.assertTrue((env_root / "worktree-preserved").is_dir())
            listed = subprocess.run(
                ["git", "-C", source, "worktree", "list"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertNotIn(str(env_root), listed)

            # The freed branch can be checked out again immediately; a still
            # registered worktree would make `worktree add` fail here.
            probe = Path(worktree_root, "probe-checkout")
            subprocess.run(
                ["git", "-C", source, "worktree", "add", str(probe), task.branch_name],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", source, "worktree", "remove", "--force", str(probe)],
                check=True,
            )

    def test_sweep_reclaims_terminal_task_env_roots(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider(write_artifact=True)
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            manager = WorktreeManager(worktree_root)
            container.daemon.worktrees = manager
            issue = container.trigger_service.create_issue(
                title="sweep",
                body="cancel while running",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )

            # Simulate a crashed daemon's leftover: an env root whose task is
            # terminal (here: cancelled before start, then preserved).
            prepared = manager.prepare(source, issue.id, "local-assistant", "task_orphan")
            manager.release(prepared)
            orphan_root = Path(prepared.env_root)
            self.assertTrue(orphan_root.exists())

            container.daemon.sweep_environments()

            self.assertFalse(orphan_root.exists())

    def test_failed_first_attempt_keeps_the_branch_name_on_retry(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            recorder = RecordingProvider(write_artifact=True)
            container.providers.register("flaky", FailOnceProvider(recorder))
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="flaky")
            )
            container.daemon.worktrees = WorktreeManager(worktree_root)
            issue = container.trigger_service.create_issue(
                title="flaky first turn",
                body="provider dies, then retry",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )

            self.assertTrue(container.daemon.run_once())
            first = container.tasks.list(issue.id)[0]
            self.assertEqual(TaskStatus.FAILED, first.status)

            container.trigger_service.add_comment(issue.id, "重试", "user-1")
            self.assertTrue(container.daemon.run_once())
            second = container.tasks.list(issue.id)[1]

            # The retry adopts the branch the failed attempt created (the
            # record ref carries its checkpoint even without a delivery):
            # no suffixed fork, and the turn's work lands on the same branch.
            self.assertEqual(TaskStatus.COMPLETED, second.status)
            self.assertEqual(first.branch_name, second.branch_name)
            self.assertIsNotNone(second.recorded_tip)

    def test_untracked_payload_over_limit_fails_with_a_clear_error(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            for index in range(3):
                Path(source, f"extra-{index}.bin").write_bytes(b"x" * 16)
            container = build_memory_container()
            provider = RecordingProvider()
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            container.daemon.worktrees = WorktreeManager(worktree_root)
            import mica.runtime.worktree as worktree_module

            original_limit = worktree_module.MAX_UNTRACKED_FILES
            worktree_module.MAX_UNTRACKED_FILES = 2
            try:
                issue = container.trigger_service.create_issue(
                    title="too much",
                    body="untracked payload",
                    workspace_id="demo",
                    assignee_id="local-assistant",
                    project_path=source,
                )
                self.assertTrue(container.daemon.run_once())
            finally:
                worktree_module.MAX_UNTRACKED_FILES = original_limit

            task = container.tasks.list(issue.id)[0]
            self.assertEqual(TaskStatus.FAILED, task.status)
            self.assertIn("snapshot limit", task.error or "")
            self.assertEqual([], provider.calls)

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
            self.assertEqual(source, provider.calls[0]["workdir"])
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
            workdir = Path(provider.calls[0]["workdir"])
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

    def test_heartbeat_renews_lease_while_preparation_runs(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider()
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            container.daemon.worktrees = SlowWorktrees(WorktreeManager(worktree_root), delay=2.0)
            container.daemon.lease_seconds = 1
            container.daemon.heartbeat_seconds = 0.05
            issue = container.trigger_service.create_issue(
                title="big worktree",
                body="preparation outlasts the initial lease",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )

            worker = threading.Thread(target=container.daemon.run_once)
            worker.start()
            time.sleep(1.5)
            self.assertEqual([], container.task_service.recover_expired("local"))
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            task = container.tasks.list(issue.id)[0]
            self.assertEqual(TaskStatus.COMPLETED, task.status)
            self.assertEqual(1, task.attempt)
            self.assertEqual(1, len(provider.calls))
            self.assertNotIn(
                "task.recovered", [event.event_type for event in container.events.list(task.id)]
            )

    def test_lease_lost_during_preparation_aborts_before_provider(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider()
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            container.daemon.worktrees = SlowWorktrees(WorktreeManager(worktree_root), delay=0.3)
            container.daemon.lease_seconds = 1
            container.daemon.heartbeat_seconds = 0.05

            def always_lost(task_id, worker_id, lease_seconds=120):
                return False

            container.task_service.renew_lease = always_lost
            issue = container.trigger_service.create_issue(
                title="stolen lease",
                body="another daemon reclaimed the task during preparation",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )

            self.assertTrue(container.daemon.run_once())

            self.assertEqual([], provider.calls)
            task = container.tasks.list(issue.id)[0]
            self.assertEqual(TaskStatus.DISPATCHED, task.status)
            self.assertIsNone(task.output)
            time.sleep(1.0)
            recovered = container.task_service.recover_expired("local")
            self.assertEqual(1, len(recovered))
            self.assertEqual(1, recovered[0].attempt)
            self.assertEqual(TaskStatus.QUEUED, container.task_service.get(task.id).status)

    def test_second_daemon_does_not_reclaim_a_slowly_preparing_task(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as worktree_root:
            create_git_repository(source)
            container = build_memory_container()
            provider = RecordingProvider()
            container.providers.register("recording", provider)
            container.agents.add(
                Agent(id="local-assistant", name="Local", runtime_id="local", provider="recording")
            )
            slow_worktrees = SlowWorktrees(WorktreeManager(worktree_root), delay=2.0)
            container.daemon.worktrees = slow_worktrees
            container.daemon.lease_seconds = 1
            container.daemon.heartbeat_seconds = 0.05
            second = Daemon(
                worker_id="daemon-b",
                runtime_id="local",
                tasks=container.task_service,
                issues=container.issues,
                agents=container.agents,
                providers=container.providers,
                slots=RuntimeSlotPool({"local": 1}),
                worktrees=slow_worktrees,
                lease_seconds=1,
                heartbeat_seconds=0.05,
            )
            issue = container.trigger_service.create_issue(
                title="two daemons",
                body="preparation outlasts the initial lease of the first daemon",
                workspace_id="demo",
                assignee_id="local-assistant",
                project_path=source,
            )

            worker = threading.Thread(target=container.daemon.run_once)
            worker.start()
            polls = 0
            deadline = time.monotonic() + 3
            while worker.is_alive() and time.monotonic() < deadline:
                self.assertFalse(second.run_once())
                polls += 1
                time.sleep(0.05)
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertGreaterEqual(polls, 5)
            task = container.tasks.list(issue.id)[0]
            self.assertEqual(TaskStatus.COMPLETED, task.status)
            self.assertEqual(1, task.attempt)
            self.assertEqual("daemon-local-1", task.claimed_by)
            self.assertEqual(1, len(provider.calls))

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
