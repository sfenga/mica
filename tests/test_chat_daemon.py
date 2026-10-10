"""Chat task execution on the shared daemon: claim fork, resume, pin, write-back."""

import tempfile

from mica.bootstrap import build_memory_container
from mica.domain import Task, TaskStatus
from mica.runtime.providers import StubProvider
from tests.test_chat_ingress import make_command


def chat_container(monkeypatch):
    monkeypatch.setenv("MICA_CHAT_BATCH_WINDOW", "0.05")
    tmp = tempfile.mkdtemp(prefix="mica-chat-test-")
    monkeypatch.setenv("MICA_WORKSPACE_ROOT", tmp)
    return build_memory_container()


def flush(container, session_id):
    return container.chat_ingress.flush_session(session_id)


def test_chat_task_completes_without_touching_the_board(monkeypatch):
    container = chat_container(monkeypatch)
    session_id = container.chat_ingress.submit(make_command("om-1"))
    assert flush(container, session_id) is not None

    assert container.daemon.run_once()

    task = container.tasks.list()[0]
    assert task.status == TaskStatus.COMPLETED
    assert task.issue_id is None
    # The reply lands in the transcript with produced-by linkage.
    stored = container.chats.list_messages(session_id)
    assert [item.role for item in stored] == ["user", "assistant"]
    assert stored[1].task_id == task.id
    # The daemon-owned triple pins back to the session.
    session = container.chats.get_session(session_id)
    assert session.session_id == task.session_id
    assert session.runtime_id == task.runtime_id
    assert session.work_dir is not None
    # Chat traffic never reaches the issue board.
    assert container.issues.list() == []


def test_second_turn_resumes_the_pinned_session(monkeypatch):
    container = chat_container(monkeypatch)
    session_id = container.chat_ingress.submit(make_command("om-1"))
    flush(container, session_id)
    assert container.daemon.run_once()
    first = container.tasks.list()[0]

    container.chat_ingress.submit(make_command("om-2", "追问：换个方案"))
    flush(container, session_id)
    assert container.daemon.run_once()
    second = container.tasks.list()[-1]

    # The stub echoes the resume pointer, so equality proves the resume chain.
    assert first.session_id == f"stub-session-{first.id}"
    assert second.prior_session_id == first.session_id
    assert second.session_id == second.prior_session_id
    session = container.chats.get_session(session_id)
    assert session.work_dir is not None
    assert len(container.chats.list_messages(session_id)) == 4


def test_resume_falls_back_to_last_task_pointer_when_session_pin_is_lost(monkeypatch):
    container = chat_container(monkeypatch)
    session_id = container.chat_ingress.submit(make_command("om-1"))
    flush(container, session_id)
    assert container.daemon.run_once()
    first = container.tasks.list()[0]

    # A later writer wiped the session pointer (e.g. a stray update): the
    # fallback must recover it from the most recent task row.
    session = container.chats.get_session(session_id)
    session.session_id = None
    container.chats.save_session(session)

    container.chat_ingress.submit(make_command("om-2", "继续"))
    flush(container, session_id)
    assert container.daemon.run_once()
    second = container.tasks.list()[-1]

    assert second.prior_session_id == first.session_id


def test_chat_and_issue_tasks_share_the_daemon(monkeypatch):
    container = chat_container(monkeypatch)
    session_id = container.chat_ingress.submit(make_command("om-1"))
    flush(container, session_id)
    issue = container.trigger_service.create_issue(
        title="正式事项",
        body="修一个 bug",
        workspace_id="local",
        assignee_id="local-assistant",
    )

    executed = 0
    while container.daemon.run_once():
        executed += 1

    assert executed == 2
    chat_task = next(task for task in container.tasks.list() if task.chat_session_id)
    issue_task = container.tasks.list(issue.id)[0]
    assert chat_task.status == TaskStatus.COMPLETED
    assert issue_task.status == TaskStatus.COMPLETED
    assert container.issues.get(issue.id).status.value in {"in_progress", "in_review"}


def test_chat_task_failure_leaves_the_board_alone(monkeypatch):
    container = chat_container(monkeypatch)
    container.providers.register("stub", StubProvider(fail_with="boom"))
    session_id = container.chat_ingress.submit(make_command("om-1"))
    flush(container, session_id)

    assert container.daemon.run_once()

    task = container.tasks.list()[0]
    assert task.status == TaskStatus.FAILED
    assert task.error == "boom"
    assert container.issues.list() == []


def test_missing_input_batch_preserves_the_task_for_redelivery(monkeypatch):
    container = chat_container(monkeypatch)
    session_id = container.chat_ingress.submit(make_command("om-1"))
    flush(container, session_id)
    task = container.tasks.list()[0]
    # Corrupt the sealed batch link the way a partial write would.
    stored = container.chats.input_messages(task.chat_input_task_id)
    assert stored  # sanity: the batch exists before the corruption below
    task.chat_input_task_id = "missing-batch"
    container.tasks.save(task)

    assert container.daemon.run_once()

    preserved = container.tasks.get(task.id)
    assert preserved.status == TaskStatus.DISPATCHED
    assert container.issues.list() == []


def test_archived_session_tasks_are_never_claimed(monkeypatch):
    container = chat_container(monkeypatch)
    session_id = container.chat_ingress.submit(make_command("om-1"))
    flush(container, session_id)
    session = container.chats.get_session(session_id)
    session.status = "archived"
    container.chats.save_session(session)

    assert container.daemon.run_once() is False

    assert container.tasks.list()[0].status == TaskStatus.QUEUED


def test_chat_completion_projects_to_the_binding_with_frozen_reply_target(monkeypatch):
    container = chat_container(monkeypatch)
    session_id = container.chat_ingress.submit(make_command("om-1", thread_id="omt-5"))
    flush(container, session_id)
    assert container.daemon.run_once()
    container.channel_worker.projector.run_once()

    notice = container.channels.list_outbox()[0]
    assert notice.message_type == "task.completed"
    assert notice.payload["reply_to"] == "om-1"
    assert notice.payload["reply_in_thread"] is True
    assert notice.destination == "oc-chat-1"
    assert "任务已完成" in notice.payload["text"]


def test_manual_chat_task_with_unreadable_input_survives_recovery(monkeypatch):
    """A chat task whose batch never materializes is preserved, not failed."""
    container = chat_container(monkeypatch)
    session_id = container.chat_ingress.submit(make_command("om-1"))
    orphan = Task(
        issue_id=None,
        agent_id="local-assistant",
        runtime_id="local",
        trigger_type="chat.message",
        trigger_id=session_id,
        chat_session_id=session_id,
        chat_input_task_id="never-sealed",
    )
    container.tasks.add(orphan)

    assert container.daemon.run_once()

    assert container.tasks.get(orphan.id).status == TaskStatus.DISPATCHED
