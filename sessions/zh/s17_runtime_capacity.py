"""s17 — Runtime Capacity / Slot(slot-before-claim 调度)

s15 的 DaemonWorker 已经能后台自动执行 queued task,但它没有表达 runtime
容量:本机同时能跑几个 task。multiCa 的 daemon 在 claim 前会先确认本机有
空闲 slot,避免把 task 从 server 队列里拿走后却没有能力立刻执行。

s17 的教学目标:

- RuntimeSlotPool:管理一个 runtime 的本地执行 slot;
- SlotDaemonWorker:先 acquire slot,再 claim task,然后在线程里 execute;
- task 结束后 release slot 并 wakeup poller,让 queued task 尽快接上。

本章不接真实 CLI 并发,只用 SlowRecordingRunner 模拟长任务,把调度不变量讲清楚。
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sessions.zh.s14_agents import (
    Daemon,
    IssueStatus,
    IssueStore,
    RunResult,
    Task,
    TaskStatus,
    TaskStore,
    render_board,
)
from sessions.zh.s15_daemon_loop import WakeupTaskStore, wait_until


@dataclass(frozen=True)
class RuntimeSlot:
    runtime_id: str
    id: int
    task_id: str = ""


class RuntimeSlotPool:
    """管理某个 runtime 的本地执行容量。

    acquire() 会预留 slot。预留发生在 claim 之前,所以 slot 可能短暂没有
    task_id;claim 成功后再 bind_task()。如果 claim 不到 task,必须 release。
    """

    def __init__(self, runtime_id: str, max_slots: int) -> None:
        if max_slots <= 0:
            raise ValueError("max_slots must be positive")
        self.runtime_id = runtime_id
        self.max_slots = max_slots
        self._available: list[int] = list(range(max_slots))
        self._occupied: dict[int, RuntimeSlot] = {}
        self._lock = threading.Lock()

    def acquire(self) -> RuntimeSlot | None:
        with self._lock:
            if not self._available:
                return None
            slot_id = self._available.pop(0)
            slot = RuntimeSlot(self.runtime_id, slot_id)
            self._occupied[slot_id] = slot
            return slot

    def bind_task(self, slot_id: int, task_id: str) -> RuntimeSlot:
        with self._lock:
            if slot_id not in self._occupied:
                raise KeyError(f"slot not occupied: {slot_id}")
            slot = RuntimeSlot(self.runtime_id, slot_id, task_id)
            self._occupied[slot_id] = slot
            return slot

    def release(self, slot_id: int) -> None:
        with self._lock:
            if slot_id not in self._occupied:
                return
            del self._occupied[slot_id]
            if slot_id not in self._available:
                self._available.append(slot_id)
                self._available.sort()

    def available(self) -> int:
        with self._lock:
            return len(self._available)

    def occupied(self) -> dict[int, RuntimeSlot]:
        with self._lock:
            return dict(self._occupied)


class SlotDaemonWorker:
    """带 runtime capacity 的后台 worker。

    和 s15 的 DaemonWorker 相比,这里多了一个不变量:

    先 acquire slot,再 claim task。

    这样没有本地执行容量时,worker 不会把 queued task 提前 claim 成 dispatched。
    """

    def __init__(self, daemon: Daemon, slot_pool: RuntimeSlotPool,
                 poll_interval: float = 2.0) -> None:
        self.daemon = daemon
        self.slot_pool = slot_pool
        self.poll_interval = poll_interval
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task_threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.claimed_count = 0
        self.completed_count = 0

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="mica-slot-daemon-worker",
                daemon=True,
            )
            self._thread.start()
        self.wakeup()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self.wakeup()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        deadline = time.time() + timeout
        for task_thread in list(self._task_threads):
            remaining = max(0.0, deadline - time.time())
            task_thread.join(timeout=remaining)

    def wakeup(self) -> None:
        self._wakeup.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            dispatched = False
            while not self._stop.is_set():
                slot = self.slot_pool.acquire()
                if slot is None:
                    break

                task = self.daemon.claim_next()
                if task is None:
                    self.slot_pool.release(slot.id)
                    break

                bound_slot = self.slot_pool.bind_task(slot.id, task.id)
                self.claimed_count += 1
                dispatched = True
                self._dispatch(task, bound_slot)

            if dispatched:
                continue

            self._wakeup.wait(self.poll_interval)
            self._wakeup.clear()

    def _dispatch(self, task: Task, slot: RuntimeSlot) -> None:
        thread = threading.Thread(
            target=self._run_in_slot,
            args=(task, slot),
            name=f"mica-slot-{slot.id}",
            daemon=True,
        )
        self._task_threads.append(thread)
        thread.start()

    def _run_in_slot(self, task: Task, slot: RuntimeSlot) -> None:
        try:
            self.daemon._record(task.id, {
                "type": "slot_acquired",
                "task_id": task.id,
                "runtime_id": slot.runtime_id,
                "slot_id": slot.id,
            })
            self.daemon.execute(task)
        finally:
            self.slot_pool.release(slot.id)
            self.completed_count += 1
            self.daemon._record(task.id, {
                "type": "slot_released",
                "task_id": task.id,
                "runtime_id": slot.runtime_id,
                "slot_id": slot.id,
            })
            self.wakeup()


class SlowRecordingRunner:
    """测试/演示用 runner:用 sleep 模拟长任务,记录最大并发数。"""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def run(self, task: Task, issue, context: list,
            on_event: Callable[[dict], None],
            resume_session_id: str | None = None,
            append_system_prompt: str | None = None,
            skill_body: str | None = None,
            cwd: str | None = None) -> RunResult:
        with self._lock:
            self.calls.append(task.id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        on_event({"type": "progress", "task_id": task.id, "message": "slow start"})
        time.sleep(self.delay)
        on_event({"type": "done", "task_id": task.id, "output": "slow done"})
        with self._lock:
            self.active -= 1
        return {"status": "done", "output": "slow done",
                "exit_code": 0, "session_id": ""}


def main() -> int:
    issue_store = IssueStore()
    task_store = WakeupTaskStore()
    runner = SlowRecordingRunner(delay=0.1)
    daemon = Daemon(issue_store, task_store, runner, runtime_id="rt-1")
    slots = RuntimeSlotPool(runtime_id="rt-1", max_slots=2)
    worker = SlotDaemonWorker(daemon, slots, poll_interval=5.0)
    unsubscribe = task_store.subscribe_wakeup(worker.wakeup)

    worker.start()
    try:
        tasks: list[Task] = []
        for title in ["task A", "task B", "task C"]:
            issue = issue_store.create("ws-1", title, assignee_type="agent",
                                       assignee_id="agent-1")
            tasks.append(task_store.enqueue(issue.id, "agent", "agent-1"))

        ok = wait_until(
            lambda: all(task_store.get(t.id).status == TaskStatus.COMPLETED
                        for t in tasks),
            timeout=3.0,
        )
        print(f"all completed: {ok}")
        print(f"max concurrent runner calls: {runner.max_active}")
        print(render_board(issue_store.list()))
        assert ok
        assert runner.max_active <= 2
        assert all(issue_store.get(t.issue_id).status == IssueStatus.IN_REVIEW
                   for t in tasks)
        return 0
    finally:
        unsubscribe()
        worker.stop()


if __name__ == "__main__":
    raise SystemExit(main())
