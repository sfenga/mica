"""Chat debounce batching: window merge, sealing, drain, recovery."""

import time

from mica.application.chat import ChatBatcher
from mica.bootstrap import build_memory_container
from tests.test_chat_ingress import make_command


def fast_batcher(container, window=0.05):
    batcher = ChatBatcher(container.chat_ingress.flush_session, window_seconds=window)
    container.chat_ingress.bind_batcher(batcher)
    return batcher


def test_window_merges_a_burst_into_exactly_one_task():
    container = build_memory_container()
    batcher = fast_batcher(container)
    session_id = container.chat_ingress.submit(make_command("om-1", thread_id="omt-9"))
    container.chat_ingress.submit(make_command("om-2", "还有青岛的", thread_id="omt-9"))
    time.sleep(0.3)

    tasks = [task for task in container.tasks.list() if task.chat_session_id == session_id]

    assert len(tasks) == 1
    task = tasks[0]
    assert task.chat_input_task_id == task.id
    sealed = container.chats.input_messages(task.id)
    assert [message.body for message in sealed] == ["解释一下这个接口", "还有青岛的"]
    # The reply target freezes onto the task at flush: last message of the
    # batch, inside the topic.
    assert task.reply_to_message_id == "om-2"
    assert task.reply_in_thread is True
    assert batcher.pending_sessions() == []


def test_flush_without_unowned_messages_creates_nothing():
    container = build_memory_container()
    fast_batcher(container)
    session_id = container.chat_ingress.submit(make_command("om-1"))
    time.sleep(0.3)
    assert container.tasks.list()  # the window already flushed the batch

    assert container.chat_ingress.flush_session(session_id) is None
    assert len(container.tasks.list()) == 1


def test_drain_flushes_every_pending_window_immediately():
    container = build_memory_container()
    batcher = fast_batcher(container, window=60)
    container.chat_ingress.submit(make_command("om-1"))
    container.chat_ingress.submit(
        make_command("om-2", "第二条", thread_id="omt-b", chat_type="p2p")
    )

    drained = batcher.drain()

    assert len(drained) == 2
    assert len(container.tasks.list()) == 2
    assert batcher.pending_sessions() == []


def test_recovery_rebuilds_windows_lost_to_a_crash():
    container = build_memory_container()
    # Window never fires (simulating a crash between append and flush).
    stale = ChatBatcher(container.chat_ingress.flush_session, window_seconds=60)
    container.chat_ingress.bind_batcher(stale)
    container.chat_ingress.submit(make_command("om-1"))
    assert container.tasks.list() == []

    # A fresh process wires new services over the same durable stores.
    fresh_ingress = container.chat_ingress
    fresh_batcher = ChatBatcher(fresh_ingress.flush_session, window_seconds=0.05)
    fresh_ingress.bind_batcher(fresh_batcher)
    fresh_ingress.recover_pending()
    time.sleep(0.3)

    assert len(container.tasks.list()) == 1


def test_archived_session_flush_is_refused():
    container = build_memory_container()
    fast_batcher(container)
    session_id = container.chat_ingress.submit(make_command("om-1"))
    session = container.chats.get_session(session_id)
    session.status = "archived"
    container.chats.save_session(session)

    assert container.chat_ingress.flush_session(session_id) is None
    assert container.tasks.list() == []
