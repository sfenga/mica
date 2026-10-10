"""PostgreSQL integration: chat repository contract and claim-shape mutexes."""

import os
import threading
from uuid import uuid4

import pytest

from mica.bootstrap import build_postgres_container
from mica.domain import AssigneeType, ChatMessage, Issue, Task, TaskStatus

pytestmark = pytest.mark.skipif(
    not os.getenv("MICA_TEST_DATABASE_URL"), reason="requires a disposable PostgreSQL database"
)


def make_container():
    return build_postgres_container(os.environ["MICA_TEST_DATABASE_URL"], default_provider="stub")


def claim_until(container, runtime_id: str, worker: str, target_id: str, max_rounds: int = 32):
    """Claim repeatedly until the target task surfaces; the shared test
    database can hold queued leftovers from other tests, so non-target claims
    are completed to keep the loop monotonic. A chat task fenced by an active
    session run must never surface."""
    for _ in range(max_rounds):
        claimed = container.tasks.claim_next(runtime_id, worker, 60)
        if claimed is None:
            continue
        if claimed.id == target_id:
            return claimed
        assert claimed.chat_session_id is None
        claimed.status = TaskStatus.COMPLETED
        container.tasks.save(claimed)
    raise AssertionError(f"task {target_id} never became claimable")


def test_chat_repository_contract_and_binding_race():
    container = make_container()
    # Unique per run: the test database persists across invocations, and a
    # leftover binding would make both threads take the "existing session"
    # path instead of racing on creation.
    conversation_id = f"oc-integration-{uuid4().hex[:12]}"
    sessions = []
    errors = []

    def ensure() -> None:
        try:
            session, created = container.chats.ensure_session(
                workspace_id="integration",
                agent_id="local-assistant",
                creator_user_id="integration-user",
                channel="feishu",
                tenant_key="integration",
                conversation_id=conversation_id,
                thread_id="omt-1",
            )
            sessions.append((session.id, created))
        except Exception as exc:  # pragma: no cover - surfaced via assertions
            errors.append(exc)

    # Two concurrent first messages race on the same binding: exactly one
    # session wins, both threads converge on it.
    threads = [threading.Thread(target=ensure) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(sessions) == 2
    assert len({session_id for session_id, _ in sessions}) == 1
    assert sorted(created for _, created in sessions) == [False, True]

    session_id = sessions[0][0]
    message, stored = container.chats.append_message(
        ChatMessage(
            chat_session_id=session_id,
            role="user",
            body="第一条",
            external_message_id="om-int-1",
            channel_ingested=True,
        )
    )
    assert stored is True
    _, duplicate_stored = container.chats.append_message(
        ChatMessage(
            chat_session_id=session_id,
            role="user",
            body="第一条",
            external_message_id="om-int-1",
            channel_ingested=True,
        )
    )
    assert duplicate_stored is False

    # The binding thread survives as the isolation key half.
    binding = container.chats.binding_for_session(session_id)
    assert binding.thread_id == "omt-1"
    assert binding.conversation_id == conversation_id


def test_claim_serializes_chat_tasks_and_coexists_with_issue_tasks():
    container = make_container()
    runtime_id = container.daemon.runtime_id
    session_id = f"chat-int-{uuid4().hex[:12]}"
    with container.database.connection() as connection:
        connection.execute(
            """INSERT INTO chat_session (id, workspace_id, agent_id, creator_user_id, created_at, updated_at)
            VALUES (%s, 'integration', 'local-assistant', 'integration-user', now(), now())""",
            (session_id,),
        )

    def add_chat_task(suffix: str, status: TaskStatus = TaskStatus.QUEUED) -> Task:
        task = Task(
            issue_id=None,
            agent_id="local-assistant",
            runtime_id=runtime_id,
            trigger_type="chat.message",
            trigger_id=session_id,
            chat_session_id=session_id,
            chat_input_task_id=suffix,
            status=status,
        )
        container.tasks.add(task)
        return task

    running = add_chat_task("running-batch", TaskStatus.RUNNING)
    queued = add_chat_task("queued-batch")

    # A running chat task on the session blocks the queued one; the queue is
    # drained only when every candidate is fenced by an active chat run.
    for _ in range(4):
        assert container.tasks.claim_next(runtime_id, "worker-a", 60) is None

    # ...but an issue task on the same agent is never blocked by a chat run.
    issue = container.issues.add(
        Issue(
            title="integration issue",
            body="body",
            workspace_id="integration",
            assignee_type=AssigneeType.AGENT,
            assignee_id="local-assistant",
        )
    )
    issue_task = container.tasks.add(
        Task(
            issue_id=issue.id,
            agent_id="local-assistant",
            runtime_id=runtime_id,
            trigger_type="issue.created",
            trigger_id=issue.id,
        )
    )
    claim_until(container, runtime_id, "worker-b", issue_task.id)

    # Once the running chat task reaches a terminal state, the queued
    # successor becomes claimable.
    active = container.tasks.get(issue_task.id)
    active.status = TaskStatus.COMPLETED
    container.tasks.save(active)
    finished = container.tasks.get(running.id)
    finished.status = TaskStatus.COMPLETED
    container.tasks.save(finished)
    successor = claim_until(container, runtime_id, "worker-c", queued.id)
    assert successor.chat_session_id == session_id
