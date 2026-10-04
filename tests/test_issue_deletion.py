import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mica.api.app import create_app
from mica.application import IngressCommand
from mica.application.services import NotFoundError
from mica.bootstrap import build_memory_container
from mica.domain import OutboxMessage, TaskStatus
from mica.runtime.providers import ProviderCancelled
from mica.runtime.providers.base import ProviderRunResult


@pytest.fixture
def container():
    return build_memory_container()


def create_issue(container, event_id="web-delete"):
    return container.ingress_service.submit(
        IngressCommand(
            channel="web",
            tenant_key="local",
            external_event_id=event_id,
            external_user_id="user",
            display_name="User",
            conversation_id="user",
            title="Delete me",
            body="Delete platform history only",
            workspace_id="local",
            assignee_id="local-assistant",
        )
    )[0]


def test_api_deletes_history_and_only_its_associations(container, monkeypatch):
    monkeypatch.setenv("MICA_EMBEDDED_DAEMON", "0")
    issue = create_issue(container)
    other = create_issue(container, "keep-web")
    container.trigger_service.add_comment(issue.id, "follow-up", "user")
    task = container.tasks.list(issue.id)[0]
    event = container.events.list(task.id)[0]
    associated = container.channels.add_outbox(
        OutboxMessage(
            channel="test",
            destination="user",
            source_event_id=event.id,
            message_type="task.queued",
            payload={"text": "old event"},
        )
    )
    unrelated = container.channels.add_outbox(
        OutboxMessage(
            channel="test",
            destination="user",
            source_event_id="unrelated",
            message_type="notice",
            payload={"text": "keep"},
        )
    )
    with TestClient(create_app(container)) as client:
        response = client.delete(f"/api/issues/{issue.id}")
        assert response.status_code == 204 and response.content == b""
        assert client.get(f"/api/issues/{issue.id}").status_code == 404
        assert client.delete(f"/api/issues/{issue.id}").status_code == 404
    assert container.tasks.list(issue.id) == []
    assert container.events.list(task.id) == []
    assert container.channels.get_origin(issue.id) is None
    assert associated.id not in container.channels._outbox
    assert unrelated.id in container.channels._outbox
    assert container.issues.get(other.id) is not None
    assert (
        container.channels.reserve_event(container.channels._events[("web", "local", "keep-web")])[
            1
        ]
        is False
    )
    assert ("web", "local", "web-delete") not in container.channels._events


def test_delete_failure_rolls_back_history_and_can_retry(container, monkeypatch):
    monkeypatch.setenv("MICA_EMBEDDED_DAEMON", "0")
    issue = create_issue(container)
    task = container.tasks.list(issue.id)[0]
    events = container.events.list(task.id)
    with TestClient(create_app(container)) as client:
        with patch.object(
            container.issues, "delete", side_effect=RuntimeError("database unavailable")
        ):
            assert client.delete(f"/api/issues/{issue.id}").status_code == 500
        assert container.issues.get(issue.id) is not None
        assert container.tasks.list(issue.id)
        assert container.events.list(task.id) == events
        assert container.channels.get_origin(issue.id) is not None
        assert client.delete(f"/api/issues/{issue.id}").status_code == 204


@pytest.mark.parametrize("late_event", [False, True])
def test_running_delete_does_not_wait_and_worker_exits_cleanly(container, late_event):
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    errors = []

    class BlockingProvider:
        def run(self, agent, issue, task, on_event, **kwargs):
            entered.set()
            assert release.wait(5)
            try:
                assert kwargs["is_cancelled"]() is True
                if late_event:
                    on_event("agent.message", {"text": "late output"})
                return ProviderRunResult("late result")
            except ProviderCancelled:
                stopped.set()
                raise

    container.providers.register("stub", BlockingProvider())
    issue = create_issue(container)
    task = container.tasks.list(issue.id)[0]

    def work():
        try:
            container.daemon.run_once()
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    assert entered.wait(5)
    try:
        container.deletion_service.delete(issue.id)
        assert worker.is_alive()  # The provider has not stopped, but DELETE finished.
        assert container.issues.get(issue.id) is None
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and errors == []
    assert container.tasks.list(issue.id) == []
    assert container.events.list(task.id) == []
    assert container.slots.acquire("local") is True
    container.slots.release("local")
    if late_event:
        assert stopped.is_set()


def test_cancellation_failure_does_not_prevent_delete(container):
    issue = create_issue(container)
    with patch.object(container.tasks, "request_cancel", side_effect=RuntimeError("cancel failed")):
        container.deletion_service.delete(issue.id)
    assert container.issues.get(issue.id) is None


def test_late_comment_and_event_cannot_recreate_history(container):
    issue = create_issue(container)
    task = container.tasks.list(issue.id)[0]
    container.deletion_service.delete(issue.id)
    with pytest.raises(NotFoundError):
        container.trigger_service.add_comment(issue.id, "late comment", "user")
    with pytest.raises(NotFoundError):
        container.task_service.record(task.id, "agent.message", {"text": "late"})
    with pytest.raises(NotFoundError):
        container.task_service.enqueue(issue, "local-assistant", "local", "late", "late")
    assert container.events.list() == [] and container.tasks.list() == []


def test_delete_completed_issue_preserves_worktree_and_session(container, tmp_path):
    issue = create_issue(container)
    container.daemon.run_once()
    task = container.tasks.list(issue.id)[0]
    assert task.status == TaskStatus.COMPLETED
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    session = worktree / "session.json"
    session.write_text("keep")
    task.worktree_path = str(worktree)
    task.session_id = str(session)
    task.branch_name = "keep-branch"
    container.tasks.save(task)
    container.deletion_service.delete(issue.id)
    assert session.read_text() == "keep"


def test_deleted_squad_callbacks_do_not_dispatch_new_tasks(container):
    from mica.application.squads import LeaderDecision
    from mica.domain import AssigneeType, Task

    squad = container.squad_service.create(
        name="delete squad", workspace_id="local", leader_agent_id="local-assistant", members=[]
    )
    issue = container.trigger_service.create_issue(
        title="squad",
        body="delete",
        workspace_id="local",
        assignee_type=AssigneeType.SQUAD,
        assignee_id=squad.id,
    )
    leader = container.tasks.list(issue.id)[0]
    worker = Task(
        issue_id=issue.id,
        agent_id="local-assistant",
        runtime_id="local",
        trigger_type="squad.delegated",
        trigger_id=leader.id,
        squad_id=squad.id,
        delegated_from_task_id=leader.id,
    )
    container.deletion_service.delete(issue.id)
    container.squad_service._apply_leader_decision(leader, LeaderDecision("complete", "late"))
    container.squad_service.handle_worker_terminal(worker)
    assert container.tasks.list() == [] and container.events.list() == []


def test_delete_does_not_remove_other_channels_same_external_id(container):
    issue = create_issue(container, "shared-external-id")
    keep = container.channels.add_outbox(
        OutboxMessage(
            channel="other",
            destination="test",
            source_event_id="shared-external-id",
            message_type="notice",
            payload={"text": "unrelated channel"},
        )
    )
    container.deletion_service.delete(issue.id)
    assert keep.id in container.channels._outbox
