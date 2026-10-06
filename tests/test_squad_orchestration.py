import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mica.api.app import create_app
from mica.application import IngressCommand
from mica.application.services import NotFoundError
from mica.bootstrap import build_memory_container
from mica.domain import Agent, AssigneeType, IssueStatus, TaskStatus
from mica.runtime import WorkspaceManager
from mica.runtime.providers import CliProvider, ProviderRunResult


class SquadProvider:
    def __init__(self, worker_ids):
        self.worker_ids = worker_ids
        self.leader_turn = 0
        self.instructions = []

    def run(self, agent, issue, task, on_event, **_kwargs):
        self.instructions.append((task.id, agent.instructions))
        if task.is_leader_task:
            self.leader_turn += 1
            if self.leader_turn == 1:
                return ProviderRunResult(output=json.dumps({
                    "decision": "delegate",
                    "summary": "parallel domain checks",
                    "assignments": [
                        {"agent_id": agent_id, "instruction": f"handle {agent_id}"}
                        for agent_id in self.worker_ids
                    ],
                }))
            return ProviderRunResult(output=json.dumps({
                "decision": "complete",
                "summary": "all member results were reviewed",
                "assignments": [],
            }))
        return ProviderRunResult(output=f"result from {agent.id}")


class WaitingSquadProvider(SquadProvider):
    def run(self, agent, issue, task, on_event, **kwargs):
        if not task.is_leader_task:
            return ProviderRunResult(
                output="need room dimensions",
                requires_input=True,
                wait_reason="missing dimensions",
                session_id="remote-session-1",
            )
        return super().run(agent, issue, task, on_event, **kwargs)


class NoActionSquadProvider(SquadProvider):
    def run(self, agent, issue, task, on_event, **kwargs):
        return ProviderRunResult(
            output=json.dumps({"decision": "no_action", "summary": "ignored"})
        )


def build_squad_container(worker_ids=("geometry", "placement")):
    container = build_memory_container()
    provider = SquadProvider(worker_ids)
    container.providers.register("squad-test", provider)
    container.agents.add(
        Agent(id="leader", name="Coordinator", runtime_id="local", provider="squad-test")
    )
    for agent_id in worker_ids:
        container.agents.add(
            Agent(id=agent_id, name=agent_id.title(), runtime_id="local", provider="squad-test")
        )
    squad = container.squad_service.create(
        name="Layout Squad",
        workspace_id="demo",
        leader_agent_id="leader",
        instructions="Geometry must be checked before final approval.",
        members=[
            {"agent_id": agent_id, "role": "specialist", "capabilities": [agent_id]}
            for agent_id in worker_ids
        ],
    )
    return container, provider, squad


def test_squad_is_user_assembled_and_issue_routes_only_to_leader():
    container, _, squad = build_squad_container()

    issue = container.trigger_service.create_issue(
        title="Generate a valid layout",
        body="Use the product catalog and room geometry.",
        workspace_id="demo",
        assignee_type=AssigneeType.SQUAD,
        assignee_id=squad.id,
    )

    tasks = container.tasks.list(issue.id)
    assert len(tasks) == 1
    assert tasks[0].agent_id == "leader"
    assert tasks[0].is_leader_task is True
    assert tasks[0].squad_id == squad.id
    assert issue.assignee_type == AssigneeType.SQUAD


def test_leader_delegates_workers_and_is_reawakened_only_after_batch_finishes():
    container, provider, squad = build_squad_container()
    issue = container.trigger_service.create_issue(
        title="Generate a valid layout",
        body="Use the product catalog and room geometry.",
        workspace_id="demo",
        assignee_type=AssigneeType.SQUAD,
        assignee_id=squad.id,
    )

    assert container.daemon.run_once()  # leader delegates
    tasks = container.tasks.list(issue.id)
    leader_task = tasks[0]
    workers = [task for task in tasks if not task.is_leader_task]
    assert len(workers) == 2
    assert all(task.delegated_from_task_id == leader_task.id for task in workers)
    assert all(task.handoff_note.startswith("handle ") for task in workers)
    assert container.issue_service.get(issue.id).status == IssueStatus.IN_PROGRESS
    assert "## Squad roster" in provider.instructions[0][1]
    assert "capabilities=geometry" in provider.instructions[0][1]

    assert container.daemon.run_once()  # first worker
    assert len([task for task in container.tasks.list(issue.id) if task.is_leader_task]) == 1

    assert container.daemon.run_once()  # second worker closes batch and wakes leader
    leader_tasks = [task for task in container.tasks.list(issue.id) if task.is_leader_task]
    assert len(leader_tasks) == 2
    assert leader_tasks[-1].status == TaskStatus.QUEUED
    assert "Review the latest delegated results" in leader_tasks[-1].handoff_note

    assert container.daemon.run_once()  # leader reviews and completes
    assert container.issue_service.get(issue.id).status == IssueStatus.IN_REVIEW
    events = [event.event_type for event in container.events.list()]
    assert events.count("squad.leader_decision") == 2
    assert events.count("squad.leader_reawakened") == 1


def test_leader_cannot_delegate_to_agent_outside_roster():
    container, provider, squad = build_squad_container(worker_ids=("geometry",))
    container.agents.add(
        Agent(id="outsider", name="Outsider", runtime_id="local", provider="squad-test")
    )
    provider.worker_ids = ("outsider",)
    issue = container.trigger_service.create_issue(
        title="invalid delegation",
        body="must fail closed",
        workspace_id="demo",
        assignee_type=AssigneeType.SQUAD,
        assignee_id=squad.id,
    )

    assert container.daemon.run_once()

    assert container.issue_service.get(issue.id).status == IssueStatus.BLOCKED
    task = container.tasks.list(issue.id)[0]
    assert task.status == TaskStatus.FAILED
    assert "not a delegatable member" in task.error


def test_leader_must_delegate_or_complete_instead_of_leaving_issue_stuck():
    container, _, squad = build_squad_container(worker_ids=("geometry",))
    container.providers.register("squad-test", NoActionSquadProvider(("geometry",)))
    issue = container.trigger_service.create_issue(
        title="must settle",
        body="leader needs an explicit outcome",
        workspace_id="demo",
        assignee_type=AssigneeType.SQUAD,
        assignee_id=squad.id,
    )

    assert container.daemon.run_once()

    task = container.tasks.list(issue.id)[0]
    assert task.status == TaskStatus.FAILED
    assert "delegate or complete" in task.error
    assert container.issue_service.get(issue.id).status == IssueStatus.BLOCKED


def test_cancelling_only_worker_wakes_leader_without_cancelling_parent():
    container, _, squad = build_squad_container(worker_ids=("geometry",))
    issue = container.trigger_service.create_issue(
        title="cancel one assignment",
        body="leader should decide the fallback",
        workspace_id="demo",
        assignee_type=AssigneeType.SQUAD,
        assignee_id=squad.id,
    )
    assert container.daemon.run_once()
    worker = next(task for task in container.tasks.list(issue.id) if not task.is_leader_task)

    cancelled = container.task_service.request_cancel(worker.id)
    container.squad_service.handle_worker_terminal(cancelled)

    assert container.issue_service.get(issue.id).status == IssueStatus.IN_PROGRESS
    leader_tasks = [task for task in container.tasks.list(issue.id) if task.is_leader_task]
    assert len(leader_tasks) == 2
    assert leader_tasks[-1].status == TaskStatus.QUEUED


def test_waiting_worker_returns_control_to_leader_instead_of_deadlocking_batch():
    container, _, squad = build_squad_container(worker_ids=("geometry",))
    container.providers.register("squad-test", WaitingSquadProvider(("geometry",)))
    issue = container.trigger_service.create_issue(
        title="missing input",
        body="ask for missing dimensions",
        workspace_id="demo",
        assignee_type=AssigneeType.SQUAD,
        assignee_id=squad.id,
    )

    assert container.daemon.run_once()  # leader delegates
    assert container.daemon.run_once()  # member asks for input

    tasks = container.tasks.list(issue.id)
    assert any(task.status == TaskStatus.WAITING_INPUT for task in tasks)
    leader_tasks = [task for task in tasks if task.is_leader_task]
    assert len(leader_tasks) == 2
    assert leader_tasks[-1].status == TaskStatus.QUEUED
    assert "waiting_input" in container.squad_service.briefing(squad.id, issue.id)


def test_comment_during_pending_leader_is_not_lost_or_run_ahead_of_workers():
    container, provider, squad = build_squad_container(worker_ids=("geometry",))
    issue = container.trigger_service.create_issue(
        title="follow-up ordering",
        body="delegate first",
        workspace_id="demo",
        assignee_type=AssigneeType.SQUAD,
        assignee_id=squad.id,
    )
    comment = container.trigger_service.add_comment(issue.id, "also check compliance", "user-1")

    queued = container.tasks.list(issue.id)
    assert len(queued) == 1
    assert queued[0].trigger_id == comment.id
    assert queued[0].trigger_comment_id == comment.id

    assert container.daemon.run_once()  # merged leader input delegates
    assert container.daemon.run_once()  # worker runs before leader is reawakened
    tasks = container.tasks.list(issue.id)
    worker = next(task for task in tasks if not task.is_leader_task)
    assert worker.status == TaskStatus.COMPLETED
    assert len([task for task in tasks if task.is_leader_task]) == 2

    assert container.daemon.run_once()  # reawakened leader reviews worker result
    assert container.issue_service.get(issue.id).status == IssueStatus.IN_REVIEW
    final_leader_instructions = [
        instructions
        for task_id, instructions in provider.instructions
        if container.tasks.get(task_id).is_leader_task
    ][-1]
    assert "Recent delegated results" in final_leader_instructions
    assert "result from geometry" in final_leader_instructions


def test_squad_create_validates_all_members_before_mutating_memory_repository():
    container = build_memory_container()
    container.agents.add(Agent("leader", "Leader", "local", "stub"))

    with pytest.raises(NotFoundError, match="agent not found"):
        container.squad_service.create(
            "Invalid", "demo", "leader", [{"agent_id": "missing"}]
        )

    assert container.squads.list() == []


def test_adding_existing_member_updates_role_and_capabilities_in_memory():
    container, _, squad = build_squad_container(worker_ids=("geometry",))

    updated = container.squad_service.add_member(
        squad.id, "geometry", "reviewer", ["geometry", "compliance"]
    )

    assert updated.role == "reviewer"
    assert updated.capabilities == ["geometry", "compliance"]


def test_squad_can_be_assembled_through_api():
    container, _, _ = build_squad_container()
    with TestClient(create_app(container)) as client:
        response = client.post("/api/squads", json={
            "name": "Second Squad",
            "workspace_id": "local",
            "leader_agent_id": "leader",
            "instructions": "Delegate by capability.",
            "members": [
                {"agent_id": "geometry", "role": "geometry", "capabilities": ["constraints"]}
            ],
        })

    assert response.status_code == 201
    assert response.json()["leader_agent_id"] == "leader"
    assert {item["agent_id"] for item in response.json()["members"]} == {"leader", "geometry"}


def test_feishu_only_receives_squad_level_completion_not_internal_task_completions():
    container, _, squad = build_squad_container()
    container.ingress_service.submit(IngressCommand(
        channel="feishu",
        tenant_key="tenant-1",
        external_event_id="squad-event-1",
        external_user_id="ou-user",
        display_name="User",
        conversation_id="oc-chat",
        title="layout task",
        body="generate layout",
        workspace_id="demo",
        assignee_type=AssigneeType.SQUAD,
        assignee_id=squad.id,
    ))
    while container.daemon.run_once():
        pass

    container.channel_worker.projector.run_once()
    message_types = [message.message_type for message in container.channels.list_outbox()]

    assert message_types == ["issue.accepted", "squad.completed"]


def test_squad_member_prompt_carries_ctx_channel():
    fixture = Path(__file__).parent / "fixtures" / "fake_ctx_probe_cli.py"
    container, _, squad = build_squad_container()
    container.providers.register(
        "claude",
        CliProvider(
            "claude",
            command_prefix=[sys.executable, str(fixture), "probe"],
            timeout_seconds=60,
        ),
    )
    for agent_id in ("geometry", "placement"):
        container.agents.add(replace(container.agents.get(agent_id), provider="claude"))

    with tempfile.TemporaryDirectory() as tmp:
        container.daemon.workspaces = WorkspaceManager(tmp)
        issue = container.trigger_service.create_issue(
            title="Generate a valid layout",
            body="Use the product catalog and room geometry.",
            workspace_id="demo",
            assignee_type=AssigneeType.SQUAD,
            assignee_id=squad.id,
        )
        assert container.daemon.run_once()  # leader delegates
        assert container.daemon.run_once()  # first member runs the probe

    member_tasks = [task for task in container.tasks.list(issue.id) if not task.is_leader_task]
    assert member_tasks[0].status == TaskStatus.COMPLETED
    findings = json.loads(member_tasks[0].output)
    assert "Delegated instruction" in findings["prompt"]
    assert "mica-ctx comments" in findings["prompt"]
    assert findings["issue_id"] == issue.id
