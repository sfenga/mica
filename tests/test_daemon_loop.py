import time

from sessions.zh.s15_daemon_loop import DaemonWorker, WakeupTaskStore, wait_until
from sessions.zh.s14_agents import (
    Daemon,
    IssueStatus,
    IssueStore,
    RecordingRunner,
    TaskStatus,
    TaskStore,
)


def _new_worker(task_store, poll_interval=5.0):
    issue_store = IssueStore()
    runner = RecordingRunner()
    daemon = Daemon(issue_store, task_store, runner, runtime_id="rt-1")
    worker = DaemonWorker(daemon, poll_interval=poll_interval)
    return issue_store, runner, worker


def test_worker_executes_enqueued_task_without_run_once():
    task_store = WakeupTaskStore()
    issue_store, runner, worker = _new_worker(task_store, poll_interval=60.0)
    unsubscribe = task_store.subscribe_wakeup(worker.wakeup)
    worker.start()
    try:
        issue = issue_store.create("ws-1", "auto run", assignee_type="agent",
                                   assignee_id="agent-1")
        task = task_store.enqueue(issue.id, "agent", "agent-1")

        assert wait_until(lambda: task_store.get(task.id).status == TaskStatus.COMPLETED)
        assert issue_store.get(issue.id).status == IssueStatus.IN_REVIEW
        assert runner.calls[-1]["task_id"] == task.id
        assert worker.claimed_count == 1
    finally:
        unsubscribe()
        worker.stop()


def test_worker_drains_multiple_queued_tasks_after_one_wakeup():
    task_store = WakeupTaskStore()
    issue_store, runner, worker = _new_worker(task_store, poll_interval=60.0)
    unsubscribe = task_store.subscribe_wakeup(worker.wakeup)
    worker.start()
    try:
        issue_a = issue_store.create("ws-1", "a", assignee_type="agent",
                                     assignee_id="agent-1")
        issue_b = issue_store.create("ws-1", "b", assignee_type="agent",
                                     assignee_id="agent-1")
        task_a = task_store.enqueue(issue_a.id, "agent", "agent-1")
        task_b = task_store.enqueue(issue_b.id, "agent", "agent-1")

        assert wait_until(
            lambda: task_store.get(task_a.id).status == TaskStatus.COMPLETED
            and task_store.get(task_b.id).status == TaskStatus.COMPLETED
        )
        assert [call["task_id"] for call in runner.calls] == [task_a.id, task_b.id]
        assert worker.claimed_count == 2
    finally:
        unsubscribe()
        worker.stop()


def test_poll_interval_catches_task_without_wakeup_subscription():
    task_store = TaskStore()
    issue_store, runner, worker = _new_worker(task_store, poll_interval=0.01)
    worker.start()
    try:
        issue = issue_store.create("ws-1", "poll fallback", assignee_type="agent",
                                   assignee_id="agent-1")
        task = task_store.enqueue(issue.id, "agent", "agent-1")

        assert wait_until(lambda: task_store.get(task.id).status == TaskStatus.COMPLETED)
        assert runner.calls[-1]["task_id"] == task.id
    finally:
        worker.stop()


def test_worker_start_stop_are_idempotent():
    task_store = WakeupTaskStore()
    _issue_store, _runner, worker = _new_worker(task_store, poll_interval=0.01)

    worker.start()
    first_thread = worker._thread
    worker.start()
    assert worker._thread is first_thread
    assert worker.running

    worker.stop()
    worker.stop()
    time.sleep(0.01)
    assert not worker.running
