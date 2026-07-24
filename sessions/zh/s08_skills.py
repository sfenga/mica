"""s08 — markdown 技能注入

在 s07(FastAPI 单页看板)之上叠加:
- Skill dataclass + SkillLoader:扫 workspace/skills/**/SKILL.md,解析 frontmatter
  (---name/description/match---),用 match 正则测 issue.title+body,首个命中即选中。
  无 match 字段的不自动选中;空目录返回空列表。
- ClaudeCliRunner.run 加 append_system_prompt 参数,透传给 _cmd(拼 --append-system-prompt)。
- AgentRunner Protocol + StubRunner 同步加 append_system_prompt(StubRunner 忽略)。
- Daemon(..., skill_loader=None):execute 里若 skill_loader 已注入,先 select(issue)
  得到 skill,把 skill.body 当 append_system_prompt 透传给 runner.run。
- main():用 StubRunner + 示例 skill,演示技能被选中 + daemon 执行。
"""
import asyncio
import glob
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol, TypedDict

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===========================================================================
# RealtimeHub — 纯 Python pub/sub(task/issue id → 订阅者集合)
# 纯逻辑、不依赖 WS,可单测。queue.Queue + threading.Lock(线程安全)。
# ===========================================================================
class RealtimeHub:
    """task/issue id → 订阅者集合。纯逻辑,不依赖 WS。

    订阅者收到一个 queue.Queue(无界、线程安全),daemon 的同步 on_event 可直接
    调 publish 推事件,WS server 在 executor 线程里 get。
    """

    def __init__(self):
        self._subs: dict[str, set] = {}   # id -> set of queue.Queue
        self._lock = threading.Lock()

    def subscribe(self, key: str) -> "queue.Queue":
        """订阅某 key(task_id 或 issue_id),返回一个 Queue 接收事件。"""
        q = queue.Queue()
        with self._lock:
            self._subs.setdefault(key, set()).add(q)
        return q

    def unsubscribe(self, key: str, q) -> None:
        with self._lock:
            if key in self._subs:
                self._subs[key].discard(q)
                if not self._subs[key]:
                    del self._subs[key]

    def publish(self, key: str, event: dict) -> None:
        """推事件给 key 的所有订阅者。非阻塞(Queue 默认无界)。"""
        with self._lock:
            subs = list(self._subs.get(key, set()))
        for q in subs:
            q.put(event)

    def subscriber_count(self, key: str) -> int:
        with self._lock:
            return len(self._subs.get(key, set()))


# ===========================================================================
# s08 — Skill + SkillLoader(扫 workspace/skills/**/SKILL.md,frontmatter.match 正则选技能)
# ===========================================================================
@dataclass
class Skill:
    name: str
    description: str
    match: str          # 正则,作用于 issue.title + issue.body;空串=不自动选中
    body: str           # markdown 正文(skill 内容)
    path: str = ""      # SKILL.md 文件路径


class SkillLoader:
    """扫 workspace/skills/**/SKILL.md,按 frontmatter.match 正则选技能。

    扫描顺序:sorted glob(recursive)。首个命中即选中(无 match 字段的不自动选中)。
    """

    def __init__(self, skills_dir: str):
        self._dir = skills_dir
        self._skills: list[Skill] = []
        self._load()

    def _load(self):
        """扫描目录,解析 frontmatter,加载技能列表。"""
        if not os.path.isdir(self._dir):
            return
        for path in sorted(glob.glob(os.path.join(self._dir, "**", "SKILL.md"), recursive=True)):
            skill = self._parse(path)
            if skill:
                self._skills.append(skill)

    def _parse(self, path: str) -> Skill | None:
        """解析 SKILL.md:---name/description/match--- + 正文。"""
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # 解析 frontmatter(--- 之间的 key: value)
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        fm = parts[1].strip()
        body = parts[2].strip()
        fields = {}
        for line in fm.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.strip()] = v.strip()
        return Skill(
            name=fields.get("name", ""),
            description=fields.get("description", ""),
            match=fields.get("match", ""),
            body=body,
            path=path,
        )

    def select(self, issue) -> Skill | None:
        """用 match 正则测 issue.title+body,取首个命中。无 match 字段的不选。"""
        text = issue.title + "\n" + (issue.body or "")
        for skill in self._skills:
            if skill.match and re.search(skill.match, text):
                return skill
        return None

    def list(self) -> list[Skill]:
        return list(self._skills)


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
            resume_session_id: str | None = None,
            append_system_prompt: str | None = None) -> RunResult: ...

class StubRunner:
    """合成事件、无子进程、不调模型。append_system_prompt 被忽略。"""
    def run(self, task, issue, context, on_event, resume_session_id=None,
            append_system_prompt=None):
        on_event({"type": "progress", "task_id": task.id,
                  "message": f"stub: start '{issue.title}'"})
        on_event({"type": "progress", "task_id": task.id, "message": "stub: working..."})
        on_event({"type": "done", "task_id": task.id,
                  "output": f"stub completed: {issue.title}"})
        return {"status": "done", "output": f"stub completed: {issue.title}",
                "exit_code": 0, "session_id": ""}

class FailingStubRunner:
    """失败变体:返回 failed。append_system_prompt 被忽略。"""
    def run(self, task, issue, context, on_event, resume_session_id=None,
            append_system_prompt=None):
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
                 session_store: "SessionStore | None" = None,
                 hub: "RealtimeHub | None" = None,
                 skill_loader: "SkillLoader | None" = None) -> None:
        self.issue_store = issue_store
        self.task_store = task_store
        self.agent = agent
        self.runner = runner
        self.poll_interval = poll_interval
        self.session_store = session_store
        self.hub = hub
        self.skill_loader = skill_loader
        self._stop = False

    def _record(self, task_id: str, event: dict) -> None:
        if self.session_store is not None:
            self.session_store.append(task_id, event)

    def _publish(self, task_id: str, event: dict, issue_id: str | None = None) -> None:
        """把事件推给 hub 订阅者(若 hub 已注入)。
        key=task_id;s07 额外按 issue_id 推(订阅 issue 即可收其所有 task 事件)。
        """
        if self.hub is not None:
            self.hub.publish(task_id, event)
            if issue_id:
                self.hub.publish(issue_id, event)

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
            daemon._publish(task.id, ev, issue_id=task.issue_id)
        self._record(task.id, {"type": "claim", "task_id": task.id,
                               "issue_id": task.issue_id,
                               "agent_id": self.agent.id})
        skill = None
        if self.skill_loader:
            skill = self.skill_loader.select(issue)
        result = self.runner.run(task, issue, [], on_event,
                                 resume_session_id=prior_sid or None,
                                 append_system_prompt=skill.body if skill else None)
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
            resume_session_id: str | None = None,
            append_system_prompt: str | None = None) -> RunResult:
        prompt = issue.title + (f"\n\n{issue.body}" if issue.body else "")
        if resume_session_id and task.trigger_comment_id:
            prompt += f"\n\n[Follow-up] {task.trigger_comment_id}"
        try:
            proc = subprocess.Popen(
                self._cmd(prompt, append_system_prompt=append_system_prompt,
                          resume_session_id=resume_session_id),
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
# H. WS server(asyncio + websockets,薄胶水)+ 终端客户端 watch_task
# ===========================================================================
import websockets


async def _ws_handler(websocket, hub: RealtimeHub) -> None:
    """从 URL path 解析 task_id,订阅 hub,把事件 JSON 推给 WS 客户端。

    path 格式:/watch/<task_id>
    """
    path = websocket.request.path
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "watch":
        await websocket.close(code=4000, reason="path must be /watch/<task_id>")
        return
    task_id = parts[1]
    q = hub.subscribe(task_id)
    try:
        loop = asyncio.get_running_loop()
        while True:
            try:
                # 在 executor 线程里阻塞 get(短超时,便于连接断开时退出循环)
                event = await loop.run_in_executor(None, lambda: q.get(timeout=0.5))
            except Exception:
                continue  # queue.Empty 超时,回到循环检查连接
            if event is None:  # 哨兵,退出
                break
            await websocket.send(json.dumps(event, ensure_ascii=False))
    except websockets.ConnectionClosed:
        pass
    except Exception:
        pass
    finally:
        hub.unsubscribe(task_id, q)


async def _ws_server(hub: RealtimeHub, port: int) -> None:
    async with websockets.serve(
        lambda ws: _ws_handler(ws, hub), "0.0.0.0", port
    ):
        await asyncio.Future()  # 永久运行


def run_ws_server(hub: RealtimeHub, port: int) -> None:
    """同步入口:启动 WS server(阻塞)。"""
    asyncio.run(_ws_server(hub, port))


def watch_task(host: str, port: int, task_id: str) -> None:
    """连 WS 订阅 task,实时打印事件。"""
    url = f"ws://{host}:{port}/watch/{task_id}"
    print(f"watching {task_id} at {url} ...")

    async def _watch() -> None:
        async with websockets.connect(url) as ws:
            while True:
                msg = await ws.recv()
                print(msg)

    asyncio.run(_watch())


# ===========================================================================
# s07 — FastAPI 单页看板
# ===========================================================================
HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>mica board</title></head>
<body>
<h1>mica board</h1>
<form id="createForm">
  <input name="title" placeholder="title" required>
  <input name="assignee_id" placeholder="agent id" value="agent-1">
  <button type="submit">Create</button>
</form>
<table id="board" border="1">
  <tr><th>ID</th><th>Status</th><th>Assignee</th><th>Title</th><th>Action</th></tr>
</table>
<div id="progress"></div>
<script>
async function refresh() {
  const res = await fetch('/api/issues');
  const issues = await res.json();
  const tbody = document.querySelector('#board');
  tbody.innerHTML = '<tr><th>ID</th><th>Status</th><th>Assignee</th><th>Title</th><th>Action</th></tr>';
  issues.forEach(i => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${i.id.slice(0,8)}</td><td>${i.status}</td>
      <td>${i.assignee_type}:${i.assignee_id||'-'}</td><td>${i.title}</td>
      <td><button onclick="watch('${i.id}')">watch</button>
      <select onchange="transition('${i.id}', this.value)">
        <option value="">change status...</option>
        <option value="in_progress">in_progress</option>
        <option value="in_review">in_review</option>
        <option value="done">done</option>
        <option value="blocked">blocked</option>
      </select></td>`;
    tbody.appendChild(tr);
  });
}
document.getElementById('createForm').onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  await fetch('/api/issues?title=' + encodeURIComponent(f.get('title'))
    + '&assignee_type=agent&assignee_id=' + encodeURIComponent(f.get('assignee_id')),
    {method:'POST'});
  refresh();
};
async function transition(id, status) {
  if (!status) return;
  await fetch('/api/issues/' + id + '/status?status=' + status, {method:'PATCH'});
  refresh();
}
function watch(id) {
  const ws = new WebSocket('ws://' + location.host + '/ws/issues/' + id);
  ws.onmessage = (e) => {
    document.getElementById('progress').textContent += e.data + '\\n';
  };
}
refresh();
</script>
</body></html>
"""


def create_app(issue_store: IssueStore, task_store: TaskStore, hub: RealtimeHub) -> FastAPI:
    """构造 FastAPI app;路由闭包持有传入的 stores + hub。"""
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML_PAGE  # 内联 HTML+JS

    @app.get("/api/issues")
    def list_issues():
        return [_issue_to_dict(i) for i in issue_store.list()]

    @app.post("/api/issues")
    def create_issue(title: str = "", assignee_type: str = "agent", assignee_id: str = ""):
        issue = issue_store.create("ws-1", title or "(untitled)",
                                   assignee_type=assignee_type, assignee_id=assignee_id)
        if assignee_type == "agent" and assignee_id:
            task_store.enqueue(issue.id, assignee_type, assignee_id)
        return _issue_to_dict(issue)

    @app.patch("/api/issues/{issue_id}/status")
    def update_status(issue_id: str, status: str):
        try:
            issue_store.transition(issue_id, IssueStatus(status))
        except (KeyError, IllegalTransition, ValueError) as e:
            return {"error": str(e)}
        return _issue_to_dict(issue_store.get(issue_id))

    @app.post("/api/issues/{issue_id}/comments")
    def add_comment(issue_id: str, text: str = ""):
        issue = issue_store.comment(issue_id, text)
        task = task_store.enqueue(issue_id, issue.assignee_type, issue.assignee_id,
                                  trigger_comment_id=text)
        return {"issue": _issue_to_dict(issue), "task_id": task.id}

    @app.websocket("/ws/issues/{issue_id}")
    async def ws_issues(websocket: WebSocket, issue_id: str):
        await websocket.accept()
        q = hub.subscribe(issue_id)
        try:
            while True:
                event = q.get(timeout=0.5)
                if event:
                    await websocket.send_json(event)
        except Exception:
            pass
        finally:
            hub.unsubscribe(issue_id, q)

    return app


# 示例 skill(供 main demo 用)
EXAMPLE_SKILL_MD = """---
name: code-review
description: 审查代码变更,关注安全/正确性/可维护性
match: review|审.*查|PR
---

You are reviewing code. Focus on:
1. Security: injection, auth bypass, data leak
2. Correctness: edge cases, error handling
3. Maintainability: naming, DRY, complexity
Report findings as a structured list.
"""


def main(data_dir: str = "./workspace") -> int:
    os.makedirs(data_dir, exist_ok=True)
    # 创建示例 skill
    skills_dir = os.path.join(data_dir, "skills", "code-review")
    os.makedirs(skills_dir, exist_ok=True)
    with open(os.path.join(skills_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(EXAMPLE_SKILL_MD)
    issue_store = IssueStore(data_dir=data_dir)
    task_store = TaskStore(data_dir=data_dir)
    sessions = SessionStore(data_dir)
    hub = RealtimeHub()
    skill_loader = SkillLoader(os.path.join(data_dir, "skills"))
    agent = Agent(id="agent-1", name="Claude")
    daemon = Daemon(issue_store, task_store, agent, StubRunner(),
                    session_store=sessions, hub=hub, skill_loader=skill_loader)
    # 创建一条命中 review 技能的 issue
    issue = issue_store.create("ws-1", "review the auth module",
                               assignee_type="agent", assignee_id="agent-1")
    task_store.enqueue(issue.id, "agent", "agent-1")
    skill = skill_loader.select(issue)
    print(f"selected skill: {skill.name if skill else 'none'}")
    daemon.run_once()
    print(render_board(issue_store.list()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
