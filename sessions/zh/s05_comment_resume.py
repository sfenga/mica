"""s05 — 评论建新 task + `--resume` 上一 task.session_id + resume failover

在 s04(JSONL 持久化 + orphan 恢复 + 截断 summarize)之上叠加:
- Issue 加 `comments: list` 字段;IssueStore.comment(issue_id, text) 追加评论、不改 issue 状态
- comment_and_enqueue(issue_store, task_store, issue_id, text):评论 + 为 issue assignee 入队新 queued task
- Daemon._find_prior_session_id(task):查同 (issue, agent) 上一条 completed task 的 session_id
- Daemon.execute:传 resume_session_id 给 runner + 覆盖 task.session_id(命中=same id no-op;失效=新 id 覆盖)
- IssueStore.rerun_task(issue_id, task_store):手动重跑,建新 task 指向源 task 的 rerun_of_task_id
- main():2 次真 claude 演示(首次捕获 session_id;comment -> 入队新 task;带 --resume 重跑)
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
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
    comments: list = field(default_factory=list)   # s05: 评论列表


class IllegalTransition(Exception):
    pass


# ---------------------------------------------------------------------------
# A. issue dict helpers(snapshot 序列化/反序列化)
# ---------------------------------------------------------------------------
def _issue_to_dict(issue: Issue) -> dict:
    d = asdict(issue)
    d["status"] = issue.status.value
    return d


def _issue_from_dict(d: dict) -> Issue:
    return Issue(
        id=d["id"],
        workspace_id=d["workspace_id"],
        title=d["title"],
        body=d.get("body", ""),
        status=IssueStatus(d["status"]),
        assignee_type=d.get("assignee_type", ""),
        assignee_id=d.get("assignee_id", ""),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
        error=d.get("error", ""),
        comments=d.get("comments", []),
    )


class IssueStore:
    """内存 issue 存储 + 状态机校验。data_dir 给定则 JSONL snapshot-append 后端。"""

    def __init__(self, data_dir: str | None = None) -> None:
        self._issues: dict[str, Issue] = {}
        self._lock = threading.Lock()
        self._path: str | None = None
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self._path = os.path.join(data_dir, "issues.jsonl")
            self._load()

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                issue = _issue_from_dict(rec)
                self._issues[issue.id] = issue   # last-wins

    def _persist(self, issue: Issue) -> None:
        if not self._path:
            return
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_issue_to_dict(issue), ensure_ascii=False) + "\n")

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
        self._persist(issue)
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
        self._persist(issue)
        return issue

    def transition(self, issue_id: str, new_status: IssueStatus) -> Issue:
        issue = self._require(issue_id)
        if new_status not in VALID_TRANSITIONS[issue.status]:
            raise IllegalTransition(
                f"非法转移:{issue.status.value} -> {new_status.value}"
            )
        issue.status = new_status
        issue.updated_at = now_iso()
        self._persist(issue)
        return issue

    def comment(self, issue_id: str, text: str) -> Issue:
        """追加评论;不改 issue 状态。"""
        with self._lock:
            issue = self._require(issue_id)
            issue.comments.append(text)
            issue.updated_at = now_iso()
            self._persist(issue)
            return issue

    def rerun_task(self, issue_id: str, task_store: "TaskStore") -> "Task":
        """手动重跑:为 issue 的 assignee 入队新 task(rerun_of_task_id 指向源 task)。"""
        with self._lock:
            issue = self._require(issue_id)
            tasks = task_store.list(issue_id)
            source_task_id = tasks[-1].id if tasks else ""
            return task_store.enqueue(issue_id, issue.assignee_type, issue.assignee_id,
                                      rerun_of_task_id=source_task_id)

    def _require(self, issue_id: str) -> Issue:
        issue = self._issues.get(issue_id)
        if issue is None:
            raise KeyError(f"issue 不存在:{issue_id}")
        return issue


# ---------------------------------------------------------------------------
# comment_and_enqueue:模块级 helper(评论 + 入队新 queued task;issue 状态不变)
# ---------------------------------------------------------------------------
def comment_and_enqueue(issue_store: IssueStore, task_store: "TaskStore",
                        issue_id: str, text: str) -> "Task":
    """评论 + 为 issue assignee 入队新 queued task。issue 状态不变。"""
    issue = issue_store.comment(issue_id, text)
    return task_store.enqueue(issue_id, issue.assignee_type, issue.assignee_id,
                              trigger_comment_id=text)


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


# ---------------------------------------------------------------------------
# task dict helpers
# ---------------------------------------------------------------------------
def _task_to_dict(task: Task) -> dict:
    d = asdict(task)
    d["status"] = task.status.value
    return d


def _task_from_dict(d: dict) -> Task:
    return Task(
        id=d["id"],
        issue_id=d["issue_id"],
        assignee_type=d.get("assignee_type", ""),
        assignee_id=d.get("assignee_id", ""),
        status=TaskStatus(d["status"]),
        session_id=d.get("session_id", ""),
        trigger_comment_id=d.get("trigger_comment_id", ""),
        rerun_of_task_id=d.get("rerun_of_task_id", ""),
        prior_session_id=d.get("prior_session_id", ""),
        created_at=d.get("created_at", ""),
        claimed_at=d.get("claimed_at", ""),
        completed_at=d.get("completed_at", ""),
        error=d.get("error", ""),
    )


# ===========================================================================
# B. TaskStore(内存,带锁)+ JSONL 后端 + orphan 恢复
# ===========================================================================
class TaskStore:
    """内存 task 队列 + CAS 认领。data_dir 给定则 JSONL snapshot-append 后端。"""

    def __init__(self, data_dir: str | None = None) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._path: str | None = None
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self._path = os.path.join(data_dir, "tasks.jsonl")
            self._load()
            self.recover_orphans()

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                task = _task_from_dict(rec)
                self._tasks[task.id] = task   # last-wins

    def _persist(self, task: Task) -> None:
        if not self._path:
            return
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_task_to_dict(task), ensure_ascii=False) + "\n")

    def recover_orphans(self) -> int:
        """重放后:running/dispatched task → failed + "interrupted by restart"。
        幂等:failed 状态不动。返回被恢复的数量。"""
        n = 0
        for task in list(self._tasks.values()):
            if task.status in (TaskStatus.RUNNING, TaskStatus.DISPATCHED):
                task.status = TaskStatus.FAILED
                task.error = "interrupted by restart"
                task.completed_at = now_iso()
                self._persist(task)
                n += 1
        return n

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
            self._persist(task)
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
            self._persist(task)
            return True

    def start(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != TaskStatus.DISPATCHED:
                return False
            task.status = TaskStatus.RUNNING
            self._persist(task)
            return True

    def complete(self, task_id: str, session_id: str = "") -> None:
        with self._lock:
            task = self._require(task_id)
            task.status = TaskStatus.COMPLETED
            task.session_id = session_id or task.session_id
            task.completed_at = now_iso()
            self._persist(task)

    def fail(self, task_id: str, error: str, session_id: str = "") -> None:
        with self._lock:
            task = self._require(task_id)
            task.status = TaskStatus.FAILED
            task.error = error
            task.session_id = session_id or task.session_id
            task.completed_at = now_iso()
            self._persist(task)

    def set_session_id(self, task_id: str, session_id: str) -> None:
        with self._lock:
            task = self._require(task_id)
            task.session_id = session_id
            self._persist(task)

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
# D. Daemon(认领 task,驱动 issue 状态,事件落 SessionStore)
# ===========================================================================
class Daemon:
    def __init__(self, issue_store: IssueStore, task_store: TaskStore,
                 agent: Agent, runner: AgentRunner,
                 poll_interval: float = 2.0,
                 session_store: "SessionStore | None" = None) -> None:
        self.issue_store = issue_store
        self.task_store = task_store
        self.agent = agent
        self.runner = runner
        self.poll_interval = poll_interval
        self.session_store = session_store
        self._stop = False

    def _record(self, task_id: str, event: dict) -> None:
        if self.session_store is not None:
            self.session_store.append(task_id, event)

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

    def _find_prior_session_id(self, task: Task) -> str:
        """查同 (issue, agent) 的上一条 completed task 的 session_id。
        返回最近一条 completed task 的 session_id;无则 ""。"""
        all_tasks = self.task_store.list(task.issue_id)
        prior = [t for t in all_tasks
                 if t.id != task.id
                 and t.assignee_type == task.assignee_type
                 and t.assignee_id == task.assignee_id
                 and t.status == TaskStatus.COMPLETED
                 and t.session_id]
        if prior:
            return prior[-1].session_id   # 最近一条 completed
        return ""

    def execute(self, task: Task) -> RunResult:
        issue = self.issue_store.get(task.issue_id)
        # 查同 (issue, agent) 上一 task 的 session_id
        prior_sid = self._find_prior_session_id(task)
        task.prior_session_id = prior_sid   # 记录用于审计
        if prior_sid:
            self.task_store._persist(task)   # 持久化 prior_session_id
        self.task_store.start(task.id)
        self._sync_issue_status(task, running=True)
        daemon = self
        def on_event(ev: dict) -> None:
            print(f"[event] {ev}")
            daemon._record(task.id, ev)
        self._record(task.id, {"type": "claim", "task_id": task.id,
                               "issue_id": task.issue_id,
                               "agent_id": self.agent.id})
        result = self.runner.run(task, issue, [], on_event,
                                 resume_session_id=prior_sid or None)
        # 覆盖 task.session_id(命中=同 id no-op;失效=新 id 覆盖)
        if result.get("session_id"):
            self.task_store.set_session_id(task.id, result["session_id"])
        if result["status"] == "done":
            self.task_store.complete(task.id, result.get("session_id", ""))
            self._sync_issue_status(task, completed=True)
        else:
            self.task_store.fail(task.id, result.get("output", ""), result.get("session_id", ""))
            self._sync_issue_status(task, failed=True)
        # 终态事件落盘
        self._record(task.id, {"type": "result", "task_id": task.id,
                               "status": result["status"],
                               "output": result.get("output", ""),
                               "session_id": result.get("session_id", "")})
        # 截断式 summarize(无 LLM)
        if self.session_store is not None:
            self.session_store.summarize_if_needed(task.id)
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
# E. parse_stream_line(纯函数 — 解析 claude stream-json 一行)
# ===========================================================================
def parse_stream_line(line: str) -> tuple[list[dict], dict | None]:
    s = line.strip()
    if not s:
        return [], None
    try:
        evt = json.loads(s)
    except json.JSONDecodeError:
        return [], None
    events: list[dict] = []
    terminal: dict | None = None
    t = evt.get("type")
    if t == "system":
        if evt.get("subtype") in ("thinking_tokens", "hook_started", "hook_response"):
            pass
        else:
            events.append({"kind": "system", "session_id": evt.get("session_id", "")})
    elif t == "assistant":
        for block in evt.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "text":
                events.append({"kind": "text", "text": block.get("text", "")})
            elif bt == "thinking":
                events.append({"kind": "thinking", "text": block.get("text", "")})
            elif bt == "tool_use":
                events.append({"kind": "tool_use", "tool": block.get("name", "")})
    elif t == "user":
        for block in evt.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                events.append({"kind": "tool_result",
                               "tool_use_id": block.get("tool_use_id", ""),
                               "is_error": bool(block.get("is_error", False))})
    elif t == "result":
        is_error = bool(evt.get("is_error")) or evt.get("subtype") == "error"
        terminal = {"status": "failed" if is_error else "done",
                    "output": evt.get("result", ""), "is_error": is_error,
                    "session_id": evt.get("session_id", "")}
    elif t == "log":
        events.append({"kind": "log", "message": evt.get("log", {}).get("message", "")})
    return events, terminal


# ===========================================================================
# F. ClaudeCliRunner(spawn claude CLI,逐行解析、转发、fail-closed)
# ===========================================================================
class ClaudeCliRunner:
    """spawn `claude -p --verbose --output-format stream-json`,逐行解析、转发、fail-closed。"""

    def __init__(self, cli: str = "claude") -> None:
        self.cli = cli

    def _cmd(self, prompt: str, append_system_prompt: str | None = None,
             resume_session_id: str | None = None,
             permission_mode: str = "bypassPermissions") -> list[str]:
        cmd = [self.cli, "-p", "--verbose", "--output-format", "stream-json", prompt]
        if permission_mode:
            cmd += ["--permission-mode", permission_mode]
        if append_system_prompt:
            cmd += ["--append-system-prompt", append_system_prompt]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        return cmd   # SHALL NOT 含 --no-session-persistence

    def run(self, task, issue, context: list, on_event,
            resume_session_id: str | None = None) -> RunResult:
        if resume_session_id and task.trigger_comment_id:
            prompt = task.trigger_comment_id   # resume:评论文本作新 user turn
        else:
            prompt = issue.title + (f"\n\n{issue.body}" if issue.body else "")
        try:
            proc = subprocess.Popen(
                self._cmd(prompt, resume_session_id=resume_session_id),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        except FileNotFoundError:
            return {"status": "failed", "output": f"CLI not found: {self.cli}",
                    "exit_code": 127, "session_id": ""}
        terminal: dict | None = None
        captured_sid = ""
        for line in proc.stdout:
            events, term = parse_stream_line(line)
            for ev in events:
                if ev.get("kind") == "system" and ev.get("session_id"):
                    captured_sid = ev["session_id"]
                ev["task_id"] = task.id
                on_event(ev)
            if term is not None:
                terminal = term
                if term.get("session_id"):
                    captured_sid = term["session_id"]
        proc.stdout.close()
        stderr = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait()
        if terminal is None:
            return {"status": "failed",
                    "output": stderr or f"stream ended without result event (exit {rc})",
                    "exit_code": rc, "session_id": captured_sid}
        if terminal["is_error"]:
            return {"status": "failed",
                    "output": terminal["output"] or stderr or "result is_error",
                    "exit_code": rc, "session_id": captured_sid}
        if rc != 0:
            return {"status": "failed", "output": stderr or f"exit {rc}",
                    "exit_code": rc, "session_id": captured_sid}
        return {"status": "done", "output": terminal["output"],
                "exit_code": rc, "session_id": captured_sid}


# ===========================================================================
# G. SessionStore(task_id 键,JSONL append + 截断 summarize)
# ===========================================================================
class SessionStore:
    """按 task_id 分文件的事件流存储。append-only JSONL + 截断式 summarize(无 LLM)。
    summarize_if_needed:当事件数 > threshold 时,丢弃旧事件,保留近 keep 条 + 1 条 summary。
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = os.path.join(data_dir, "sessions")
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, task_id: str) -> str:
        return os.path.join(self._dir, f"{task_id}.jsonl")

    def append(self, task_id: str, event: dict) -> None:
        ev = dict(event)
        ev.setdefault("ts", now_iso())
        with open(self._path(task_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def load(self, task_id: str) -> list[dict]:
        path = self._path(task_id)
        if not os.path.exists(path):
            return []
        events: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                events.append(json.loads(line))
        return events

    def summarize_if_needed(self, task_id: str, threshold: int = 100,
                            keep: int = 20) -> int:
        """截断式 summarize。返回丢弃的事件数(0 表示未触发)。
        超过 threshold 时:留近 keep 条 + 1 条 summary(共 keep+1 行)。
        """
        if keep <= 0:
            raise ValueError("keep must be positive")
        events = self.load(task_id)
        if len(events) <= threshold:
            return 0
        dropped = len(events) - keep
        recent = events[-keep:]
        summary = {
            "type": "summary",
            "task_id": task_id,
            "dropped_count": dropped,
            "kept": keep,
            "ts": now_iso(),
            "message": f"truncated {dropped} earlier events (keep last {keep})",
        }
        self._rewrite(task_id, [summary] + recent)
        return dropped

    def _rewrite(self, task_id: str, events: list[dict]) -> None:
        """原子重写:写 tmp 后 os.replace,失败清理 tmp。"""
        path = self._path(task_id)
        tmp = path + ".tmp"
        tmp_ok = False
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            tmp_ok = True
            os.replace(tmp, path)
        finally:
            if tmp_ok is False and os.path.exists(tmp):
                os.remove(tmp)


# ===========================================================================
# H. main()(demo:2 次真 claude;首次捕获 session_id,comment -> 入队新 task,带 --resume 重跑)
# ===========================================================================
def main(data_dir: str = "./workspace") -> int:
    os.makedirs(data_dir, exist_ok=True)
    issue_store = IssueStore(data_dir=data_dir)
    task_store = TaskStore(data_dir=data_dir)
    sessions = SessionStore(data_dir)
    agent = Agent(id="agent-1", name="Claude")
    daemon = Daemon(issue_store, task_store, agent, ClaudeCliRunner(),
                    session_store=sessions)
    issue = issue_store.create("ws-1", "Reply with exactly: mica s05 first",
                               assignee_type="agent", assignee_id="agent-1")
    task_store.enqueue(issue.id, "agent", "agent-1")
    print("--- 第 1 次跑(首次,捕获 session_id)---")
    daemon.run_once()
    print(render_board(issue_store.list()))
    print(f"session_id = {task_store.list(issue.id)[-1].session_id}")
    print("--- 评论 -> 入队新 task -> 带 --resume 重跑 ---")
    comment_and_enqueue(issue_store, task_store, issue.id,
                        "now reply with exactly: mica s05 second")
    daemon.run_once()
    print(render_board(issue_store.list()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
