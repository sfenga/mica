"""s02 — Task 实体 + Daemon 认领 task + StubRunner

对应 multiCa:task lifecycle / claim task / resume from task.session_id。
在 s01 issue 7 态状态机之上叠加 Task 实体(TaskStatus:queued→dispatched→running→
completed|failed|cancelled)与内存 TaskStore(CAS 认领 queued→dispatched)。
Daemon 认领 TaskStore 中派给本 agent 的 queued task,通过 AgentRunner 执行,
用 task 生命周期驱动 issue 状态(running→in_progress, completed→in_review,
failed→blocked+error)。
本章 runner 为 StubRunner(合成事件、不调模型);s03 接 claude CLI,s05 加 resume。
"""
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol, TypedDict


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class IssueStatus(str, Enum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


# 合法状态转移:键=当前状态,值=可转到的状态集合。其它转移一律拒绝。
VALID_TRANSITIONS = {
    IssueStatus.BACKLOG: {IssueStatus.TODO},
    IssueStatus.TODO: {IssueStatus.IN_PROGRESS},
    IssueStatus.IN_PROGRESS: {IssueStatus.IN_REVIEW, IssueStatus.DONE, IssueStatus.BLOCKED},
    IssueStatus.IN_REVIEW: {IssueStatus.DONE, IssueStatus.IN_PROGRESS, IssueStatus.BLOCKED},
    IssueStatus.BLOCKED: {IssueStatus.TODO, IssueStatus.CANCELLED},
    IssueStatus.DONE: set(),
    IssueStatus.CANCELLED: set(),
}


@dataclass
class Workspace:
    id: str
    slug: str
    name: str


@dataclass
class Agent:
    id: str
    name: str
    cli: str = "claude"
    runtime_id: str = "local"


@dataclass
class Member:
    id: str
    name: str


@dataclass
class Issue:
    id: str
    workspace_id: str
    title: str
    body: str = ""
    status: IssueStatus = IssueStatus.TODO
    assignee_type: str = ""       # "" | "member" | "agent"
    assignee_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    error: str = ""


class IllegalTransition(Exception):
    pass


class IssueStore:
    """内存 issue 存储 + 状态机校验。s04 加 JSONL 后端。"""

    def __init__(self) -> None:
        self._issues: dict[str, Issue] = {}

    def create(self, workspace_id: str, title: str, body: str = "",
               assignee_type: str = "", assignee_id: str = "") -> Issue:
        issue = Issue(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            title=title,
            body=body,
            assignee_type=assignee_type,
            assignee_id=assignee_id,
        )
        self._issues[issue.id] = issue
        return issue

    def list(self, workspace_id: str | None = None) -> list[Issue]:
        return [
            i for i in self._issues.values()
            if workspace_id is None or i.workspace_id == workspace_id
        ]

    def get(self, issue_id: str) -> Issue | None:
        return self._issues.get(issue_id)

    def assign(self, issue_id: str, assignee_type: str, assignee_id: str) -> Issue:
        issue = self._require(issue_id)
        issue.assignee_type = assignee_type
        issue.assignee_id = assignee_id
        issue.updated_at = now_iso()
        return issue

    def transition(self, issue_id: str, new_status: IssueStatus) -> Issue:
        issue = self._require(issue_id)
        if new_status not in VALID_TRANSITIONS[issue.status]:
            raise IllegalTransition(
                f"非法转移:{issue.status.value} -> {new_status.value}"
            )
        issue.status = new_status
        issue.updated_at = now_iso()
        return issue

    def _require(self, issue_id: str) -> Issue:
        issue = self._issues.get(issue_id)
        if issue is None:
            raise KeyError(f"issue 不存在:{issue_id}")
        return issue


def render_board(issues: list[Issue]) -> str:
    """终端看板:一行一个 issue。"""
    if not issues:
        return "(空看板)"
    lines = ["ID            STATUS       ASSIGNEE                TITLE"]
    for i in issues:
        assignee = f"{i.assignee_type}:{i.assignee_id}" if i.assignee_type else "-"
        lines.append(f"{i.id[:12]:<13} {i.status.value:<12} {assignee:<23} {i.title}")
    return "\n".join(lines)


# --- s01 CLI(单进程内执行;s04 持久化后可跨进程)---
def cmd_main(argv: list[str]) -> int:
    store = IssueStore()
    ws_id = "ws-1"
    if not argv:
        # 无参:演示场景——一条 issue 走 todo→in_progress→in_review→done
        a = store.create(ws_id, "修登录 bug", assignee_type="agent", assignee_id="agent-1")
        store.transition(a.id, IssueStatus.IN_PROGRESS)
        store.transition(a.id, IssueStatus.IN_REVIEW)
        store.transition(a.id, IssueStatus.DONE)
        store.create(ws_id, "写文档", assignee_type="member", assignee_id="alice")
        print(render_board(store.list()))
        return 0
    cmd = argv[0]
    if cmd == "create":
        title = argv[1] if len(argv) > 1 else "(untitled)"
        at = argv[2] if len(argv) > 2 else ""
        aid = argv[3] if len(argv) > 3 else ""
        issue = store.create("ws-1", title, assignee_type=at, assignee_id=aid)
        print(f"created {issue.id} '{issue.title}'")
        return 0
    if cmd == "list":
        print(render_board(store.list()))
        return 0
    if cmd == "assign":
        store.assign(argv[1], argv[2], argv[3])
        print("assigned")
        return 0
    if cmd == "show":
        issue = store.get(argv[1])
        if issue is None:
            print(f"not found: {argv[1]}", file=sys.stderr)
            return 1
        print(f"{issue.id}  {issue.status.value}  "
              f"{issue.assignee_type}:{issue.assignee_id}  {issue.title}")
        return 0
    print(f"未知命令:{cmd}", file=sys.stderr)
    return 2


# ===========================================================================
# A. Task dataclass + TaskStatus
# ===========================================================================
class TaskStatus(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

TASK_TRANSITIONS = {
    TaskStatus.QUEUED: {TaskStatus.DISPATCHED, TaskStatus.CANCELLED},
    TaskStatus.DISPATCHED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}

@dataclass
class Task:
    id: str
    issue_id: str
    assignee_type: str = ""        # "agent" | "member"
    assignee_id: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    session_id: str = ""
    trigger_comment_id: str = ""
    rerun_of_task_id: str = ""
    prior_session_id: str = ""
    created_at: str = field(default_factory=now_iso)
    claimed_at: str = ""
    completed_at: str = ""
    error: str = ""


# ===========================================================================
# B. TaskStore(内存,带锁)
# ===========================================================================
class TaskStore:
    """内存 task 队列 + CAS 认领。s04 加 JSONL 后端。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def enqueue(self, issue_id: str, assignee_type: str, assignee_id: str,
                trigger_comment_id: str = "", rerun_of_task_id: str = "") -> Task:
        with self._lock:
            task = Task(
                id=str(uuid.uuid4()),
                issue_id=issue_id,
                assignee_type=assignee_type,
                assignee_id=assignee_id,
                trigger_comment_id=trigger_comment_id,
                rerun_of_task_id=rerun_of_task_id,
            )
            self._tasks[task.id] = task
            return task

    def list(self, issue_id: str | None = None) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values()
                    if issue_id is None or t.issue_id == issue_id]

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def claim(self, task_id: str) -> bool:
        """CAS 认领:仅当 status==queued 才置 dispatched。返回是否成功。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != TaskStatus.QUEUED:
                return False
            task.status = TaskStatus.DISPATCHED
            task.claimed_at = now_iso()
            return True

    def start(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != TaskStatus.DISPATCHED:
                return False
            task.status = TaskStatus.RUNNING
            return True

    def complete(self, task_id: str, session_id: str = "") -> None:
        with self._lock:
            task = self._require(task_id)
            task.status = TaskStatus.COMPLETED
            task.session_id = session_id or task.session_id
            task.completed_at = now_iso()

    def fail(self, task_id: str, error: str, session_id: str = "") -> None:
        with self._lock:
            task = self._require(task_id)
            task.status = TaskStatus.FAILED
            task.error = error
            task.session_id = session_id or task.session_id
            task.completed_at = now_iso()

    def set_session_id(self, task_id: str, session_id: str) -> None:
        with self._lock:
            task = self._require(task_id)
            task.session_id = session_id

    def _require(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"task 不存在:{task_id}")
        return task


# ===========================================================================
# C. AgentRunner Protocol + StubRunner
# ===========================================================================
class RunResult(TypedDict):
    status: str          # "done" | "failed"
    output: str
    exit_code: int
    session_id: str

class AgentRunner(Protocol):
    def run(self, task: Task, issue: Issue, context: list,
            on_event: Callable[[dict], None],
            resume_session_id: str | None = None) -> RunResult: ...

class StubRunner:
    """合成事件、无子进程、不调模型。"""
    def run(self, task, issue, context, on_event, resume_session_id=None):
        on_event({"type": "progress", "task_id": task.id,
                  "message": f"stub: start '{issue.title}'"})
        on_event({"type": "progress", "task_id": task.id, "message": "stub: working..."})
        on_event({"type": "done", "task_id": task.id,
                  "output": f"stub completed: {issue.title}"})
        return {"status": "done", "output": f"stub completed: {issue.title}",
                "exit_code": 0, "session_id": ""}

class FailingStubRunner:
    """失败变体:返回 failed。"""
    def run(self, task, issue, context, on_event, resume_session_id=None):
        on_event({"type": "fail", "task_id": task.id, "output": "stub failed: boom"})
        return {"status": "failed", "output": "stub failed: boom",
                "exit_code": 1, "session_id": ""}


# ===========================================================================
# D. Daemon(认领 task,驱动 issue 状态)
# ===========================================================================
class Daemon:
    def __init__(self, issue_store: IssueStore, task_store: TaskStore,
                 agent: Agent, runner: AgentRunner,
                 poll_interval: float = 2.0) -> None:
        self.issue_store = issue_store
        self.task_store = task_store
        self.agent = agent
        self.runner = runner
        self.poll_interval = poll_interval
        self._stop = False

    def claim_next(self) -> Task | None:
        """原子认领一条派给本 agent 的 queued task。"""
        candidates = [
            t for t in self.task_store.list()
            if t.assignee_type == "agent"
            and t.assignee_id == self.agent.id
            and t.status == TaskStatus.QUEUED
        ]
        for task in candidates:
            if self.task_store.claim(task.id):   # CAS
                return task
        return None

    def execute(self, task: Task) -> RunResult:
        issue = self.issue_store.get(task.issue_id)
        self.task_store.start(task.id)
        # task running → issue in_progress
        self._sync_issue_status(task, running=True)
        def on_event(ev: dict) -> None:
            print(f"[event] {ev}")
        result = self.runner.run(task, issue, [], on_event)
        if result["status"] == "done":
            self.task_store.complete(task.id, result.get("session_id", ""))
            self._sync_issue_status(task, completed=True)
        else:
            self.task_store.fail(task.id, result.get("output", ""), result.get("session_id", ""))
            self._sync_issue_status(task, failed=True)
        return result

    def _sync_issue_status(self, task: Task, running=False, completed=False, failed=False) -> None:
        """task 生命周期驱动 issue 状态(running→in_progress, completed→in_review, failed→blocked)。
        blocked issue 上新 task 重跑时,blocked→todo→in_progress 两步(状态机不允许 blocked→in_progress)。"""
        issue = self.issue_store.get(task.issue_id)
        if issue is None:
            return
        if running:
            if issue.status == IssueStatus.BLOCKED:
                self.issue_store.transition(issue.id, IssueStatus.TODO)
                self.issue_store.transition(issue.id, IssueStatus.IN_PROGRESS)
            elif issue.status in (IssueStatus.TODO, IssueStatus.IN_REVIEW):
                self.issue_store.transition(issue.id, IssueStatus.IN_PROGRESS)
        elif completed and issue.status == IssueStatus.IN_PROGRESS:
            self.issue_store.transition(issue.id, IssueStatus.IN_REVIEW)
        elif failed and issue.status == IssueStatus.IN_PROGRESS:
            issue.error = task.error
            self.issue_store.transition(issue.id, IssueStatus.BLOCKED)

    def tick(self) -> bool:
        task = self.claim_next()
        if task is None:
            return False
        print(f"[daemon] claimed task {task.id} for issue {task.issue_id}")
        self.execute(task)
        return True

    def run_once(self) -> None:
        while self.tick():
            pass

    def loop(self) -> None:
        while not self._stop:
            self.tick()
            time.sleep(self.poll_interval)


# ===========================================================================
# E. main()(demo:为 issue 入队 task → daemon 认领 → task completed + issue in_review)
# ===========================================================================
def main() -> int:
    issue_store = IssueStore()
    task_store = TaskStore()
    agent = Agent(id="agent-1", name="Claude")
    daemon = Daemon(issue_store, task_store, agent, StubRunner())
    issue = issue_store.create("ws-1", "修登录 bug",
                               assignee_type="agent", assignee_id="agent-1")
    task_store.enqueue(issue.id, "agent", "agent-1")
    print("--- 派活前 ---")
    print(render_board(issue_store.list()))
    daemon.run_once()
    print("--- 认领执行后 ---")
    print(render_board(issue_store.list()))
    return 0

if __name__ == "__main__":
    sys.exit(main())
