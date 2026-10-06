import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn

from mica.api.app import create_app
from mica.bootstrap import build_memory_container
from mica.domain import Comment, TaskStatus
from mica.runtime import WorkspaceManager
from mica.runtime.providers import CliProvider
from mica.runtime.providers.base import build_task_prompt

FIXTURE = Path(__file__).parent / "fixtures" / "fake_ctx_probe_cli.py"


class LiveApi:
    """Serve the real FastAPI app on an ephemeral port for subprocess clients."""

    def __init__(self, container, startup_timeout: float = 10.0):
        config = uvicorn.Config(create_app(container), host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.startup_timeout = startup_timeout
        self.url = ""

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + self.startup_timeout
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("uvicorn server thread exited during startup")
            if time.monotonic() > deadline:
                raise TimeoutError("uvicorn server did not start within timeout")
            time.sleep(0.05)
        socket = self.server.servers[0].sockets[0]
        self.url = f"http://127.0.0.1:{socket.getsockname()[1]}"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=5)


def seed_agent_result(container, issue, body="已完成：workdir/ai-top10.html 含 Top10 表格"):
    stored = container.issues.get(issue.id)
    stored.comments.append(
        Comment(
            issue_id=issue.id,
            body=body,
            author_id="general_cc",
            author_type="agent",
            source_task_id="task_previous",
        )
    )
    container.issues.add(stored)


class CtxCommandTest(unittest.TestCase):
    """mica-ctx against the real API served over HTTP."""

    def setUp(self):
        self.container = build_memory_container()
        self.issue = self.container.trigger_service.create_issue(
            title="每日ai热点",
            body="看下今天的 ai-top10 热点",
            workspace_id="workspace-demo",
            assignee_id="local-assistant",
        )
        seed_agent_result(self.container, self.issue)
        patcher = patch.dict(os.environ, {"MICA_EMBEDDED_DAEMON": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = LiveApi(self.container).__enter__()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.__exit__(None, None, None)

    def run_ctx(self, *args, extra_env=None):
        env = os.environ.copy()
        env["MICA_API_URL"] = self.server.url
        env["MICA_ISSUE_ID"] = self.issue.id
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "mica.ctx", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_comments_lists_authors_and_excerpts(self):
        result = self.run_ctx("comments")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("agent:general_cc", result.stdout)
        self.assertIn("ai-top10.html", result.stdout)

    def test_comment_prints_full_body(self):
        stored = self.container.issues.get(self.issue.id)
        comment_id = stored.comments[0].id
        result = self.run_ctx("comment", comment_id)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Top10 表格", result.stdout)

    def test_unknown_comment_fails(self):
        result = self.run_ctx("comment", "comment_missing")
        self.assertEqual(1, result.returncode)
        self.assertIn("not found", result.stderr)

    def test_missing_issue_env_fails(self):
        result = self.run_ctx("comments", extra_env={"MICA_ISSUE_ID": ""})
        self.assertEqual(2, result.returncode)
        self.assertIn("MICA_ISSUE_ID", result.stderr)


class CtxPromptTest(unittest.TestCase):
    def test_cold_prompt_makes_scan_mandatory(self):
        container = build_memory_container()
        issue = container.trigger_service.create_issue(
            title="t", body="b", workspace_id="w", assignee_id="local-assistant"
        )
        task = container.tasks.list(issue.id)[0]
        prompt = build_task_prompt(
            container.issues.get(issue.id), task, resumed=False, ctx_channel=True
        )
        self.assertIn("Before doing anything else", prompt)
        self.assertIn("mica-ctx comments", prompt)

    def test_warm_prompt_only_offers_the_channel(self):
        container = build_memory_container()
        issue = container.trigger_service.create_issue(
            title="t", body="b", workspace_id="w", assignee_id="local-assistant"
        )
        task = container.tasks.list(issue.id)[0]
        prompt = build_task_prompt(
            container.issues.get(issue.id), task, resumed=True, ctx_channel=True
        )
        self.assertIn("already carries this issue's background", prompt)
        self.assertNotIn("Before doing anything else", prompt)

    def test_channel_is_off_by_default(self):
        container = build_memory_container()
        issue = container.trigger_service.create_issue(
            title="t", body="b", workspace_id="w", assignee_id="local-assistant"
        )
        task = container.tasks.list(issue.id)[0]
        prompt = build_task_prompt(container.issues.get(issue.id), task)
        self.assertNotIn("mica-ctx", prompt)


class CtxChannelWiringTest(unittest.TestCase):
    """Daemon → CliProvider wiring: env vars and prompt reach the agent process."""

    def build(self, tmp, mode):
        container = build_memory_container(default_provider="claude")
        container.providers.register(
            "claude",
            CliProvider(
                "claude", command_prefix=[sys.executable, str(FIXTURE), mode], timeout_seconds=60
            ),
        )
        container.daemon.workspaces = WorkspaceManager(tmp)
        return container

    def test_agent_receives_ctx_env_and_cold_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = self.build(tmp, "probe")
            issue = container.trigger_service.create_issue(
                title="每日ai热点",
                body="看下今天的 ai-top10 热点",
                workspace_id="workspace-demo",
                assignee_id="local-assistant",
            )
            self.assertTrue(container.daemon.run_once())
            task = container.tasks.list(issue.id)[0]
            self.assertEqual(TaskStatus.COMPLETED, task.status)
            findings = json.loads(task.output)
            self.assertEqual(issue.id, findings["issue_id"])
            self.assertEqual(task.id, findings["task_id"])
            self.assertIn("mica-ctx comments", findings["prompt"])
            self.assertIn("Before doing anything else", findings["prompt"])

    def test_follow_up_gets_warm_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = self.build(tmp, "probe")
            issue = container.trigger_service.create_issue(
                title="每日ai热点",
                body="看下今天的 ai-top10 热点",
                workspace_id="workspace-demo",
                assignee_id="local-assistant",
            )
            self.assertTrue(container.daemon.run_once())
            container.trigger_service.add_comment(issue.id, "补充：重点看安全类热点", "local-user")
            self.assertTrue(container.daemon.run_once())
            follow_up = container.tasks.list(issue.id)[1]
            self.assertEqual(TaskStatus.COMPLETED, follow_up.status)
            findings = json.loads(follow_up.output)
            self.assertIn("already carries this issue's background", findings["prompt"])
            self.assertIn("重点看安全类热点", findings["prompt"])

    def test_agent_pulls_comments_through_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = self.build(tmp, "pull")
            issue = container.trigger_service.create_issue(
                title="每日ai热点",
                body="看下今天的 ai-top10 热点",
                workspace_id="workspace-demo",
                assignee_id="local-assistant",
            )
            seed_agent_result(container, issue)
            with (
                patch.dict(os.environ, {"MICA_EMBEDDED_DAEMON": "0"}),
                LiveApi(container) as server,
            ):
                with patch.dict(os.environ, {"MICA_API_URL": server.url}):
                    self.assertTrue(container.daemon.run_once())
            task = container.tasks.list(issue.id)[0]
            self.assertEqual(TaskStatus.COMPLETED, task.status)
            findings = json.loads(task.output)
            self.assertEqual(0, findings["returncode"], findings["stderr"])
            self.assertIn("agent:general_cc", findings["stdout"])
            self.assertIn("ai-top10.html", findings["stdout"])


if __name__ == "__main__":
    unittest.main()
