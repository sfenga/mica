from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from mica.bootstrap import build_memory_container
from mica.domain import Agent, AssigneeType, Issue, Task
from mica.domain import IssueStatus, TaskStatus
from mica.runtime.providers import A2aProvider, AcpProvider, ProviderCancelled


ACP_FIXTURE = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"


def issue_and_task(agent: Agent, project_path: str | None = None, trigger_type: str = "issue.created"):
    issue = Issue(
        title="设计办公区布局",
        body="为 20 人团队生成满足消防通道约束的布局 JSON",
        workspace_id="demo",
        assignee_type=AssigneeType.AGENT,
        assignee_id=agent.id,
        project_path=project_path,
    )
    task = Task(
        issue_id=issue.id,
        agent_id=agent.id,
        runtime_id=agent.runtime_id,
        trigger_type=trigger_type,
        trigger_id=issue.id,
    )
    return issue, task


class AcpProviderTest(unittest.TestCase):
    def test_acp_runs_handshake_streams_updates_and_audits_permission(self):
        with tempfile.TemporaryDirectory() as workdir:
            agent = Agent(
                id="coding-acp",
                name="Coding ACP",
                runtime_id="local",
                provider="acp",
                provider_config={
                    "command": [sys.executable, str(ACP_FIXTURE)],
                    "permission_policy": "allow",
                },
            )
            issue, task = issue_and_task(agent, workdir)
            events = []
            result = AcpProvider(timeout_seconds=5).run(
                agent, issue, task, lambda kind, payload: events.append((kind, payload)), workdir
            )

        self.assertEqual("acp-session-1", result.session_id)
        self.assertEqual("working permission=allow", result.output)
        self.assertIn("agent.system", [kind for kind, _ in events])
        permission = next(payload for kind, payload in events if kind == "agent.permission")
        self.assertEqual("allow", permission["option_id"])

    def test_acp_loads_previous_session(self):
        with tempfile.TemporaryDirectory() as workdir:
            agent = Agent(
                id="coding-acp",
                name="Coding ACP",
                runtime_id="local",
                provider="acp",
                provider_config={"command": [sys.executable, str(ACP_FIXTURE)]},
            )
            issue, task = issue_and_task(agent, workdir)
            result = AcpProvider(timeout_seconds=5).run(
                agent,
                issue,
                task,
                lambda *_: None,
                workdir,
                resume_session_id="existing-session",
            )

        self.assertEqual("existing-session", result.session_id)

    def test_acp_cancel_terminates_agent_process(self):
        with tempfile.TemporaryDirectory() as workdir:
            agent = Agent(
                id="coding-acp",
                name="Coding ACP",
                runtime_id="local",
                provider="acp",
                provider_config={"command": [sys.executable, str(ACP_FIXTURE), "slow"]},
            )
            issue, task = issue_and_task(agent, workdir)
            started = time.monotonic()
            with self.assertRaises(ProviderCancelled):
                AcpProvider(timeout_seconds=5).run(
                    agent,
                    issue,
                    task,
                    lambda *_: None,
                    workdir,
                    is_cancelled=lambda: time.monotonic() - started > 0.3,
                )


class A2aProviderTest(unittest.TestCase):
    def _agent(self, **overrides):
        config = {
            "agent_card_url": "https://geometry.example/.well-known/agent-card.json",
            "skill_id": "layout",
            "poll_interval": 0,
            "output_schema": {
                "type": "object",
                "required": ["layoutId", "valid"],
                "properties": {
                    "layoutId": {"type": "string"},
                    "valid": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        }
        config.update(overrides)
        return Agent(
            id="geometry",
            name="Geometry",
            runtime_id="remote",
            provider="a2a",
            provider_config=config,
        )

    @staticmethod
    def _card():
        return {
            "name": "Geometry Agent",
            "supportedInterfaces": [
                {
                    "url": "https://geometry.example/a2a",
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0",
                }
            ],
            "capabilities": {"streaming": False},
            "skills": [{"id": "layout", "name": "Layout"}],
        }

    def test_a2a_discovers_agent_polls_task_and_validates_json(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            if request.url.path.endswith("agent-card.json"):
                return httpx.Response(200, json=self._card())
            if request.url.path == "/a2a/message:send":
                self.assertEqual("1.0", request.headers["A2A-Version"])
                body = json.loads(request.content)
                self.assertEqual("ROLE_USER", body["message"]["role"])
                self.assertEqual("application/json", body["message"]["parts"][0]["metadata"]["mediaType"])
                return httpx.Response(
                    200,
                    json={
                        "task": {
                            "id": "remote-1",
                            "contextId": "context-1",
                            "status": {"state": "TASK_STATE_WORKING"},
                        }
                    },
                )
            if request.url.path == "/a2a/tasks/remote-1":
                return httpx.Response(
                    200,
                    json={
                        "id": "remote-1",
                        "contextId": "context-1",
                        "status": {"state": "TASK_STATE_COMPLETED"},
                        "artifacts": [
                            {"artifactId": "layout-1", "parts": [{"data": {"layoutId": "L-1", "valid": True}}]}
                        ],
                    },
                )
            return httpx.Response(404)

        agent = self._agent()
        issue, task = issue_and_task(agent)
        events = []
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = A2aProvider(client).run(
                agent, issue, task, lambda kind, payload: events.append((kind, payload))
            )

        self.assertEqual('{"layoutId":"L-1","valid":true}', result.output)
        self.assertTrue(result.session_id.startswith("a2a1:"))
        self.assertEqual(1, sum(request.url.path.endswith("agent-card.json") for request in requests))
        self.assertIn("agent.data", [kind for kind, _ in events])

    def test_a2a_follow_up_reuses_context(self):
        sent_messages = []

        def handler(request: httpx.Request):
            if request.url.path.endswith("agent-card.json"):
                return httpx.Response(200, json=self._card())
            if request.url.path == "/a2a/message:send":
                sent_messages.append(json.loads(request.content)["message"])
                return httpx.Response(
                    200,
                    json={
                        "message": {
                            "messageId": "answer",
                            "contextId": "context-1",
                            "role": "ROLE_AGENT",
                            "parts": [{"text": '{"layoutId":"L-2","valid":true}'}],
                        }
                    },
                )
            return httpx.Response(404)

        agent = self._agent()
        issue, first_task = issue_and_task(agent)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = A2aProvider(client)
            first = provider.run(agent, issue, first_task, lambda *_: None)
            _, second_task = issue_and_task(agent)
            provider.run(
                agent,
                issue,
                second_task,
                lambda *_: None,
                resume_session_id=first.session_id,
            )

        self.assertNotIn("contextId", sent_messages[0])
        self.assertEqual("context-1", sent_messages[1]["contextId"])
        self.assertNotIn("taskId", sent_messages[1])

    def test_a2a_schema_mismatch_fails_task_result(self):
        def handler(request: httpx.Request):
            if request.url.path.endswith("agent-card.json"):
                return httpx.Response(200, json=self._card())
            return httpx.Response(
                200,
                json={
                    "message": {
                        "messageId": "answer",
                        "role": "ROLE_AGENT",
                        "parts": [{"data": {"layoutId": 42, "valid": True}}],
                    }
                },
            )

        agent = self._agent()
        issue, task = issue_and_task(agent)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RuntimeError, "schema validation failed"):
                A2aProvider(client).run(agent, issue, task, lambda *_: None)

    def test_a2a_input_required_follow_up_targets_existing_remote_task(self):
        sent_messages = []

        def handler(request: httpx.Request):
            if request.url.path.endswith("agent-card.json"):
                return httpx.Response(200, json=self._card())
            if request.url.path == "/a2a/message:send":
                message = json.loads(request.content)["message"]
                sent_messages.append(message)
                if len(sent_messages) == 1:
                    return httpx.Response(
                        200,
                        json={
                            "task": {
                                "id": "remote-input",
                                "contextId": "context-input",
                                "status": {
                                    "state": "TASK_STATE_INPUT_REQUIRED",
                                    "message": {
                                        "role": "ROLE_AGENT",
                                        "parts": [{"text": "请补充房间尺寸"}],
                                    },
                                },
                            }
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "message": {
                            "messageId": "answer",
                            "contextId": "context-input",
                            "role": "ROLE_AGENT",
                            "parts": [{"text": "已收到尺寸"}],
                        }
                    },
                )
            return httpx.Response(404)

        agent = self._agent(output_schema=None)
        issue, first_task = issue_and_task(agent)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = A2aProvider(client)
            first = provider.run(agent, issue, first_task, lambda *_: None)
            _, second_task = issue_and_task(agent)
            provider.run(
                agent,
                issue,
                second_task,
                lambda *_: None,
                resume_session_id=first.session_id,
            )

        self.assertTrue(first.requires_input)
        self.assertEqual("TASK_STATE_INPUT_REQUIRED", first.wait_reason)
        self.assertEqual("context-input", sent_messages[1]["contextId"])
        self.assertEqual("remote-input", sent_messages[1]["taskId"])

    def test_a2a_cancel_calls_remote_cancel_operation(self):
        state = {"submitted": False, "cancelled": False}

        def handler(request: httpx.Request):
            if request.url.path.endswith("agent-card.json"):
                return httpx.Response(200, json=self._card())
            if request.url.path == "/a2a/message:send":
                state["submitted"] = True
                return httpx.Response(
                    200,
                    json={"task": {"id": "remote-2", "status": {"state": "TASK_STATE_WORKING"}}},
                )
            if request.url.path == "/a2a/tasks/remote-2:cancel":
                state["cancelled"] = True
                return httpx.Response(
                    200,
                    json={"id": "remote-2", "status": {"state": "TASK_STATE_CANCELED"}},
                )
            return httpx.Response(404)

        agent = self._agent()
        issue, task = issue_and_task(agent)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ProviderCancelled):
                A2aProvider(client).run(
                    agent,
                    issue,
                    task,
                    lambda *_: None,
                    is_cancelled=lambda: state["submitted"],
                )
        self.assertTrue(state["cancelled"])

    def test_daemon_executes_a2a_agent_without_a_git_worktree(self):
        def handler(request: httpx.Request):
            if request.url.path.endswith("agent-card.json"):
                return httpx.Response(200, json=self._card())
            return httpx.Response(
                200,
                json={
                    "message": {
                        "messageId": "answer",
                        "contextId": "business-context",
                        "role": "ROLE_AGENT",
                        "parts": [{"data": {"layoutId": "L-3", "valid": True}}],
                    }
                },
            )

        agent = self._agent()
        container = build_memory_container()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            container.providers.register("a2a", A2aProvider(client))
            container.agents.add(
                Agent(
                    id="local-assistant",
                    name=agent.name,
                    runtime_id="local",
                    provider="a2a",
                    provider_config=agent.provider_config,
                )
            )
            issue = container.trigger_service.create_issue(
                title="生成布局",
                body="20 人办公区",
                workspace_id="demo",
                assignee_id="local-assistant",
            )
            self.assertTrue(container.daemon.run_once())

        saved_task = container.tasks.list(issue.id)[0]
        self.assertEqual(TaskStatus.COMPLETED, saved_task.status)
        self.assertEqual(IssueStatus.IN_REVIEW, container.issue_service.get(issue.id).status)
        self.assertIn("agent.data", [event.event_type for event in container.events.list(saved_task.id)])

    def test_daemon_blocks_for_remote_input_then_continues_same_a2a_task(self):
        calls = []

        def handler(request: httpx.Request):
            if request.url.path.endswith("agent-card.json"):
                return httpx.Response(200, json=self._card())
            message = json.loads(request.content)["message"]
            calls.append(message)
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={
                        "task": {
                            "id": "remote-wait",
                            "contextId": "context-wait",
                            "status": {
                                "state": "TASK_STATE_INPUT_REQUIRED",
                                "message": {
                                    "role": "ROLE_AGENT",
                                    "parts": [{"text": "需要房间宽度"}],
                                },
                            },
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "message": {
                        "messageId": "final",
                        "contextId": "context-wait",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "方案已生成"}],
                    }
                },
            )

        agent = self._agent(output_schema=None)
        container = build_memory_container()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            container.providers.register("a2a", A2aProvider(client))
            container.agents.add(
                Agent(
                    id="local-assistant",
                    name=agent.name,
                    runtime_id="local",
                    provider="a2a",
                    provider_config=agent.provider_config,
                )
            )
            issue = container.trigger_service.create_issue(
                title="生成布局", body="20 人办公区", workspace_id="demo", assignee_id="local-assistant"
            )
            self.assertTrue(container.daemon.run_once())
            waiting = container.tasks.list(issue.id)[0]
            self.assertEqual(TaskStatus.WAITING_INPUT, waiting.status)
            self.assertEqual(IssueStatus.BLOCKED, container.issue_service.get(issue.id).status)

            container.trigger_service.add_comment(issue.id, "房间宽度 8 米", "user-1")
            self.assertTrue(container.daemon.run_once())

        tasks = container.tasks.list(issue.id)
        self.assertEqual(TaskStatus.COMPLETED, tasks[1].status)
        self.assertEqual(IssueStatus.IN_REVIEW, container.issue_service.get(issue.id).status)
        self.assertEqual("context-wait", calls[1]["contextId"])
        self.assertEqual("remote-wait", calls[1]["taskId"])


if __name__ == "__main__":
    unittest.main()
