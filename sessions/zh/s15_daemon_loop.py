"""s15 — Daemon Loop(后台自动认领 queued task)

s14 已经让 daemon 能按 assignee_id 加载 Agent Profile。s15 补上一个更接近
multiCa 的执行入口:daemon 不再只靠用户手动 run_once,而是由后台 worker 常驻。

核心模型:

- Daemon.tick():一次 claim + execute,已有的单步执行原语
- DaemonWorker:后台线程,反复调用 tick(),空闲时等待 wakeup 或 poll_interval
- WakeupTaskStore:enqueue 后通知订阅者,让 worker 尽快醒来

教学版仍是单进程内存/JSONL 存储;multiCa 生产版对应 daemon.Run()、pollLoop、
runBatchPoller、NotifyTaskEnqueued 和 websocket wakeup。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sessions.zh.s14_agents import (
    Daemon,
    IssueStatus,
    IssueStore,
    RecordingRunner,
    Task,
    TaskStatus,
    TaskStore,
    render_board,
)


WakeupCallback = Callable[[], None]


class WakeupTaskStore(TaskStore):
    """TaskStore + enqueue wakeup.

    TaskStore 仍负责创建 queued task;WakeupTaskStore 只在 enqueue 成功后通知
    订阅者。这个边界对应 multiCa 的 NotifyTaskEnqueued:入队本身和唤醒 daemon
    是两个动作,但必须在同一条创建路径里连起来。
    """

    def __init__(self, data_dir: str | None = None) -> None:
        super().__init__(data_dir=data_dir)
        self._wakeup_callbacks: list[WakeupCallback] = []
        self._wakeup_lock = threading.Lock()

    def subscribe_wakeup(self, callback: WakeupCallback) -> Callable[[], None]:
        """订阅 task-available 事件;返回 unsubscribe 函数。"""
        with self._wakeup_lock:
            self._wakeup_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._wakeup_lock:
                if callback in self._wakeup_callbacks:
                    self._wakeup_callbacks.remove(callback)

        return unsubscribe

    def _notify_task_available(self) -> None:
        with self._wakeup_lock:
            callbacks = list(self._wakeup_callbacks)
        for callback in callbacks:
            callback()

    def enqueue(self, issue_id: str, assignee_type: str, assignee_id: str,
                trigger_comment_id: str = "", rerun_of_task_id: str = "",
                is_leader_task: bool = False, squad_id: str = "",
                handoff_note: str = "") -> Task:
        task = super().enqueue(
            issue_id,
            assignee_type,
            assignee_id,
            trigger_comment_id=trigger_comment_id,
            rerun_of_task_id=rerun_of_task_id,
            is_leader_task=is_leader_task,
            squad_id=squad_id,
            handoff_note=handoff_note,
        )
        self._notify_task_available()
        return task


class DaemonWorker:
    """把 Daemon.tick() 包成后台常驻 loop。

    loop 策略:
    1. 有空闲 slot 时先持续 drain queued task;
    2. 如果没有 task,等待 wakeup;
    3. 如果 wakeup 丢失或没有订阅,用 poll_interval 兜底。

    这里不实现多 slot 并发。multiCa 的 runBatchPoller 会按本机空闲 capacity
    批量 claim;mica s15 先保留单 worker,把“自动唤醒 + 后台 claim”讲清楚。
    """

    def __init__(self, daemon: Daemon, poll_interval: float = 2.0) -> None:
        self.daemon = daemon
        self.poll_interval = poll_interval
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.claimed_count = 0

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
                name="mica-daemon-worker",
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

    def wakeup(self) -> None:
        self._wakeup.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            drained = False
            while not self._stop.is_set() and self.daemon.tick():
                self.claimed_count += 1
                drained = True

            if drained:
                continue

            self._wakeup.wait(self.poll_interval)
            self._wakeup.clear()


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0,
               interval: float = 0.01) -> bool:
    """demo/test 小工具:等待异步 worker 让某个条件成立。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def main() -> int:
    issue_store = IssueStore()
    task_store = WakeupTaskStore()
    runner = RecordingRunner(print_injection=True)
    daemon = Daemon(issue_store, task_store, runner, runtime_id="rt-1")
    worker = DaemonWorker(daemon, poll_interval=5.0)
    unsubscribe = task_store.subscribe_wakeup(worker.wakeup)

    worker.start()
    try:
        issue = issue_store.create(
            "ws-1",
            "background daemon should pick this up",
            assignee_type="agent",
            assignee_id="agent-1",
        )
        task = task_store.enqueue(issue.id, "agent", "agent-1")
        ok = wait_until(lambda: task_store.get(task.id).status == TaskStatus.COMPLETED)
        print(f"auto executed: {ok}")
        print(render_board(issue_store.list()))
        assert issue_store.get(issue.id).status == IssueStatus.IN_REVIEW
        return 0
    finally:
        unsubscribe()
        worker.stop()


if __name__ == "__main__":
    raise SystemExit(main())
