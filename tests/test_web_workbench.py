from pathlib import Path

from fastapi.testclient import TestClient

from mica.api import create_app
from mica.bootstrap import build_memory_container
from mica.domain import Agent, RuntimeCapability


def test_workbench_assets_are_served():
    with TestClient(create_app(build_memory_container())) as client:
        page = client.get("/app")
        assert page.status_code == 200
        assert "任务看板" in page.text
        assert client.get("/assets/styles.css").status_code == 200
        script = client.get("/assets/app.js")
        assert script.status_code == 200
        assert "ISSUE_COLUMNS" in script.text


def test_runtime_can_be_edited_and_capabilities_are_exposed():
    container = build_memory_container()
    with TestClient(create_app(container)) as client:
        response = client.patch(
            "/api/runtimes/local",
            json={"name": "My Mac", "max_slots": 2},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "My Mac"
        assert response.json()["max_slots"] == 2
        capabilities = client.get(
            "/api/runtime-capabilities", params={"runtime_id": "local"}
        ).json()
        assert capabilities[0]["provider"] == "stub"


def test_local_agent_requires_discovered_provider():
    container = build_memory_container()
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/api/agents",
            json={
                "id": "codex-agent",
                "name": "Codex Agent",
                "runtime_id": "local",
                "provider": "codex",
                "provider_config": {},
            },
        )

        assert response.status_code == 422
        assert "not available" in response.json()["detail"]


def test_existing_agent_can_switch_to_discovered_runtime_provider():
    container = build_memory_container()
    container.runtime_capabilities.replace_for_runtime(
        "local",
        [
            RuntimeCapability(
                runtime_id="local",
                provider="codex",
                executable_path="/opt/bin/codex",
                version="codex-cli 1.0",
            )
        ],
    )
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/api/agents",
            json={
                "id": "local-assistant",
                "name": "Repository Assistant",
                "runtime_id": "local",
                "provider": "codex",
                "instructions": "Work in the assigned repository.",
                "provider_config": {},
            },
        )

        assert response.status_code == 201
        assert response.json()["provider"] == "codex"
        assert response.json()["provider_config"]["executable"] == "/opt/bin/codex"


def test_claude_permission_mode_is_owned_by_runtime_adapter():
    container = build_memory_container()
    container.runtime_capabilities.replace_for_runtime(
        "local",
        [
            RuntimeCapability(
                runtime_id="local",
                provider="claude",
                executable_path="/opt/bin/claude",
                version="claude-code 1.0",
            )
        ],
    )
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/api/agents",
            json={
                "id": "claude-agent",
                "name": "Claude Agent",
                "runtime_id": "local",
                "provider": "claude",
                "provider_config": {"permission_mode": "plan"},
            },
        )

    assert response.status_code == 201
    assert response.json()["provider_config"] == {"executable": "/opt/bin/claude"}


def test_discovered_project_can_be_selected_without_typing_path(
    tmp_path: Path, monkeypatch
):
    repository = tmp_path / "orders-service"
    (repository / ".git").mkdir(parents=True)
    monkeypatch.setenv("MICA_PROJECT_ROOTS", str(tmp_path))
    container = build_memory_container()

    with TestClient(create_app(container)) as client:
        candidates = client.get("/api/project-candidates").json()
        assert candidates == [
            {"name": "orders-service", "local_path": str(repository)}
        ]

        project = client.post(
            "/api/projects",
            json={
                "name": "Orders Service",
                "local_path": str(repository),
                "workspace_id": "local",
                "runtime_id": "local",
                "execution_mode": "in_place",
            },
        ).json()
        assert project["execution_mode"] == "in_place"
        issue = client.post(
            "/api/issues",
            json={
                "title": "Fix order sync",
                "body": "Find and fix the failing synchronization.",
                "workspace_id": "local",
                "assignee_id": "local-assistant",
                "project_id": project["id"],
            },
        )

        assert issue.status_code == 201
        assert issue.json()["project_path"] == str(repository)
        assert issue.json()["project_execution_mode"] == "in_place"

        updated = client.patch(
            f"/api/projects/{project['id']}",
            json={"execution_mode": "worktree"},
        )
        assert updated.status_code == 200
        assert updated.json()["execution_mode"] == "worktree"
        # The issue records how it was created; later project changes do not rewrite history.
        assert client.get(f"/api/issues/{issue.json()['id']}").json()[
            "project_execution_mode"
        ] == "in_place"


def test_local_issue_can_run_without_selecting_a_project():
    container = build_memory_container()
    container.agents.add(
        Agent(
            id="local-assistant",
            name="Codex",
            runtime_id="local",
            provider="codex",
        )
    )
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/api/issues",
            json={
                "title": "Explain the architecture",
                "body": "Answer without opening a repository.",
                "workspace_id": "local",
                "assignee_id": "local-assistant",
            },
        )

        assert response.status_code == 201
        assert response.json()["project_path"] is None
        assert response.json()["project_execution_mode"] is None


def test_squad_configuration_can_be_updated_when_idle():
    container = build_memory_container()
    with TestClient(create_app(container)) as client:
        agent = client.post(
            "/api/agents",
            json={
                "id": "remote-consultant",
                "name": "Remote Consultant",
                "runtime_id": "local",
                "provider": "stub",
                "provider_config": {},
            },
        )
        assert agent.status_code == 201
        squad = client.post(
            "/api/squads",
            json={
                "name": "Consulting Squad",
                "workspace_id": "local",
                "leader_agent_id": "local-assistant",
                "members": [
                    {
                        "agent_id": "remote-consultant",
                        "role": "consultant",
                        "capabilities": ["knowledge-search"],
                    }
                ],
            },
        ).json()

        updated = client.put(
            f"/api/squads/{squad['id']}",
            json={
                "name": "Business Consulting Squad",
                "leader_agent_id": "remote-consultant",
                "description": "Handle internal consultation",
                "instructions": "Delegate before summarizing.",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["leader_agent_id"] == "remote-consultant"
        assert next(
            member
            for member in updated.json()["members"]
            if member["agent_id"] == "remote-consultant"
        )["role"] == "leader"

        removed = client.delete(
            f"/api/squads/{squad['id']}/members/local-assistant"
        )
        assert removed.status_code == 204


def test_websocket_serializes_history_and_live_event_dates(monkeypatch):
    from fastapi import WebSocketDisconnect

    from mica.domain import TaskEvent

    monkeypatch.setenv("MICA_EMBEDDED_DAEMON", "0")
    container = build_memory_container()
    issue = container.trigger_service.create_issue(
        title="Live dates",
        body="Verify JSON websocket frames.",
        workspace_id="local",
        assignee_id="local-assistant",
    )
    task = container.tasks.list(issue.id)[0]
    live = TaskEvent(task_id=task.id, event_type="agent.text", payload={"text": "live"})

    class Channel:
        delivered = False

        def get(self, *_args):
            if self.delivered:
                raise WebSocketDisconnect()
            self.delivered = True
            return live

    monkeypatch.setattr(container.event_bus, "subscribe", lambda _task_id: Channel())
    monkeypatch.setattr(container.event_bus, "unsubscribe", lambda *_args: None)
    with TestClient(create_app(container)) as client:
        with client.websocket_connect(f"/api/tasks/{task.id}/events") as socket:
            history = socket.receive_json()
            message = socket.receive_json()
            assert history["event_type"] == "task.queued"
            assert isinstance(history["created_at"], str)
            assert message["event_type"] == "agent.text"
            assert isinstance(message["created_at"], str)
