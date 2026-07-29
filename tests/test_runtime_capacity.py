import time

from sessions.zh.s14_agents import (
    Daemon,
    IssueStatus,
    IssueStore,
    RecordingRunner,
    TaskStatus,
)
from sessions.zh.s15_daemon_loop import WakeupTaskStore, wait_until
from sessions.zh.s17_runtime_capacity import (
    RuntimeSlotPool,
    SlotDaemonWorker,
    SlowRecordingRunner,
)


def _enqueue_agent_task(issue_store, task_store, title: str):
    issue = issue_store.create("ws-1", title, assignee_type="agent",
                               assignee_id="agent-1")
    task = task_store.enqueue(issue.id, "agent", "agent-1")
    return issue, task


def test_slot_pool_acquire_bind_release():
    slots = RuntimeSlotPool("rt-1", max_slots=2)

    slot0 = slots.acquire()
    slot1 = slots.acquire()
    slot2 = slots.acquire()

    assert slot0 is not None and slot0.id == 0
    assert slot1 is not None and slot1.id == 1
    assert slot2 is None
    assert slots.available() == 0

    bound = slots.bind_task(slot0.id, "task-1")
    assert bound.task_id == "task-1"
    assert slots.occupied()[0].task_id == "task-1"

    slots.release(slot0.id)
    assert slots.available() == 1
    assert 0 not in slots.occupied()


def test_worker_does_not_claim_more_than_available_slots():
    issue_store = IssueStore()
    task_store = WakeupTaskStore()
    runner = SlowRecordingRunner(delay=0.2)
    daemon = Daemon(issue_store, task_store, runner, runtime_id="rt-1")
    slots = RuntimeSlotPool("rt-1", max_slots=2)
    worker = SlotDaemonWorker(daemon, slots, poll_interval=60.0)
    unsubscribe = task_store.subscribe_wakeup(worker.wakeup)

    worker.start()
    try:
        _issue_a, task_a = _enqueue_agent_task(issue_store, task_store, "a")
        _issue_b, task_b = _enqueue_agent_task(issue_store, task_store, "b")
        _issue_c, task_c = _enqueue_agent_task(issue_store, task_store, "c")

        assert wait_until(
            lambda: len([
                t for t in [task_store.get(task_a.id),
                            task_store.get(task_b.id),
                            task_store.get(task_c.id)]
                if t.status == TaskStatus.RUNNING
            ]) == 2,
            timeout=1.0,
        )
        statuses = {t.id: task_store.get(t.id).status for t in [task_a, task_b, task_c]}
        assert list(statuses.values()).count(TaskStatus.RUNNING) == 2
        assert list(statuses.values()).count(TaskStatus.QUEUED) == 1
        assert runner.max_active <= 2
    finally:
        unsubscribe()
        worker.stop()


def test_released_slot_wakes_worker_and_claims_queued_task():
    issue_store = IssueStore()
    task_store = WakeupTaskStore()
    runner = SlowRecordingRunner(delay=0.05)
    daemon = Daemon(issue_store, task_store, runner, runtime_id="rt-1")
    slots = RuntimeSlotPool("rt-1", max_slots=2)
    worker = SlotDaemonWorker(daemon, slots, poll_interval=60.0)
    unsubscribe = task_store.subscribe_wakeup(worker.wakeup)

    worker.start()
    try:
        tasks = [
            _enqueue_agent_task(issue_store, task_store, title)[1]
            for title in ["a", "b", "c"]
        ]

        assert wait_until(
            lambda: all(task_store.get(t.id).status == TaskStatus.COMPLETED
                        for t in tasks),
            timeout=2.0,
        )
        assert worker.claimed_count == 3
        assert worker.completed_count == 3
        assert slots.available() == 2
        assert runner.max_active <= 2
        assert all(issue_store.get(t.issue_id).status == IssueStatus.IN_REVIEW
                   for t in tasks)
    finally:
        unsubscribe()
        worker.stop()


def test_worker_releases_slot_when_no_task_is_claimable():
    issue_store = IssueStore()
    task_store = WakeupTaskStore()
    runner = RecordingRunner()
    daemon = Daemon(issue_store, task_store, runner, runtime_id="rt-1")
    slots = RuntimeSlotPool("rt-1", max_slots=1)
    worker = SlotDaemonWorker(daemon, slots, poll_interval=0.01)

    worker.start()
    try:
        time.sleep(0.05)
        assert slots.available() == 1
        assert slots.occupied() == {}
        assert worker.claimed_count == 0
    finally:
        worker.stop()
