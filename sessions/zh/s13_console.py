"""s12 — Squads leader-first 协调模型

本文件是 s01-s12 的累积教学代码。s12 在 s11 的 workspace/member 基础上,
加入一个教学版 squad 编排器:
- squad 可以成为 issue assignee;
- 系统先唤醒 leader agent,而不是直接哈希选择 worker;
- leader task 携带 is_leader_task + squad_id,用于注入 briefing;
- leader 可委派 worker,worker 回报和 child stage 完成会再次唤醒 leader。
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol, TypedDict

from croniter import croniter
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===========================================================================
# s10 — PathDetector / Runtime / MultiCliRunner(多 CLI 探测 + runner 注册表)
# ===========================================================================
class PathDetector:
    """探测 PATH 上可用的 agent CLI(shutil.which)。"""
    KNOWN_CLIS = ["claude", "codex", "copilot", "opencode", "cursor-agent"]

    @staticmethod
    def detect() -> list[str]:
        """返回 PATH 上找到的 CLI 名列表(按 KNOWN_CLIS 顺序)。"""
        return [cli for cli in PathDetector.KNOWN_CLIS if shutil.which(cli)]

    @staticmethod
    def is_available(cli: str) -> bool:
        return shutil.which(cli) is not None


@dataclass
class Runtime:
    """一个运行时进程:持有 id/name 和探测到的可用 CLI 列表。"""
    id: str
    name: str
    clis: list = field(default_factory=list)   # 可用 CLI 列表


class MultiCliRunner:
    """按 cli 名注册 runner 的简单注册表(s10 的 daemon 不实际调用它,
    它是未来多 CLI 分派的建筑块)。"""
    def __init__(self):
        self._runners: dict = {}   # cli_name -> runner

    def register(self, cli: str, runner) -> None:
        self._runners[cli] = runner

    def has(self, cli: str) -> bool:
        return cli in self._runners

    def available(self) -> list[str]:
        return list(self._runners.keys())


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


# ===========================================================================
# s11 — MemberStore(members.jsonl:workspace_id -> {member_id}) + WorkspaceScope
# ===========================================================================
class MemberStore:
    """members.jsonl:workspace_id -> {member_id} 集合。

    同 IssueStore/TaskStore 的 JSONL snapshot-append + last-wins 模式。
    每条记录为 {"workspace_id": ..., "members": [...]} 快照,重放时整体覆盖。
    """

    def __init__(self, data_dir: str | None = None) -> None:
        self._members: dict[str, set[str]] = {}   # workspace_id -> {member_ids}
        self._lock = threading.Lock()
        self._path: str | None = None
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self._path = os.path.join(data_dir, "members.jsonl")
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
                ws_id = rec["workspace_id"]
                # last-wins:后写覆盖前写
                self._members[ws_id] = set(rec.get("members", []))

    def _persist(self, ws_id: str, member_ids: set[str]) -> None:
        if not self._path:
            return
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"workspace_id": ws_id, "members": sorted(member_ids)},
                ensure_ascii=False) + "\n")

    def add_member(self, ws_id: str, member_id: str) -> None:
        with self._lock:
            members = self._members.setdefault(ws_id, set())
            members.add(member_id)
            self._persist(ws_id, members)

    def remove_member(self, ws_id: str, member_id: str) -> None:
        with self._lock:
            members = self._members.get(ws_id)
            if members is None:
                return
            members.discard(member_id)
            self._persist(ws_id, members)

    def is_member(self, ws_id: str, member_id: str) -> bool:
        with self._lock:
            return member_id in self._members.get(ws_id, set())

    def list_members(self, ws_id: str) -> list[str]:
        with self._lock:
            return sorted(self._members.get(ws_id, set()))


class WorkspaceScope:
    """在 IssueStore/TaskStore 查询时注入 workspace_id 过滤 + 成员校验。

    薄封装:实际成员校验由 IssueStore.create/list 内部完成(直接调 member_store);
    本类仅作为概念/入口对象存在,聚合 workspace_id/member_id/member_store。
    """

    def __init__(self, workspace_id: str, member_id: str,
                 member_store: MemberStore) -> None:
        self.workspace_id = workspace_id
        self.member_id = member_id
        self.member_store = member_store

    def check_access(self) -> None:
        """校验当前 member 属于该 workspace;非成员抛 PermissionError。"""
        if not self.member_store.is_member(self.workspace_id, self.member_id):
            raise PermissionError(
                f"member {self.member_id} not in workspace {self.workspace_id}")


@dataclass
class Issue:
    id: str
    workspace_id: str
    title: str
    body: str = ""
    status: IssueStatus = IssueStatus.TODO
    assignee_type: str = ""       # "" | "member" | "agent" | "squad"
    assignee_id: str = ""
    parent_issue_id: str = ""     # s12:子 issue 指向父 issue
    stage: int | None = None      # s12:阶段号;None 表示不参加显式 stage
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
        parent_issue_id=d.get("parent_issue_id", ""),
        stage=d.get("stage"),
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
               assignee_type: str = "", assignee_id: str = "",
               parent_issue_id: str = "", stage: int | None = None,
               status: IssueStatus = IssueStatus.TODO,
               member_id: str | None = None,
               member_store: "MemberStore | None" = None) -> Issue:
        if member_id and member_store:
            if not member_store.is_member(workspace_id, member_id):
                raise PermissionError(
                    f"member {member_id} not in workspace {workspace_id}")
        issue = Issue(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            title=title,
            body=body,
            status=status,
            assignee_type=assignee_type,
            assignee_id=assignee_id,
            parent_issue_id=parent_issue_id,
            stage=stage,
        )
        self._issues[issue.id] = issue
        self._persist(issue)
        return issue

    def list(self, workspace_id: str | None = None,
             member_id: str | None = None,
             member_store: "MemberStore | None" = None) -> list[Issue]:
        if member_id and member_store:
            if workspace_id is None:
                raise PermissionError(
                    "member_id requires workspace_id for membership check")
            if not member_store.is_member(workspace_id, member_id):
                raise PermissionError(
                    f"member {member_id} not in workspace {workspace_id}")
        return [
            i for i in self._issues.values()
            if workspace_id is None or i.workspace_id == workspace_id
        ]

    def get(self, issue_id: str) -> Issue | None:
        return self._issues.get(issue_id)

    def children(self, parent_issue_id: str) -> list[Issue]:
        return [
            i for i in self._issues.values()
            if i.parent_issue_id == parent_issue_id
        ]

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
    is_leader_task: bool = False  # s12:leader 角色 task,claim 时注入 squad briefing
    squad_id: str = ""            # s12:leader/worker task 属于哪个 squad
    handoff_note: str = ""        # s12:leader 委派或系统唤醒原因
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
        is_leader_task=d.get("is_leader_task", False),
        squad_id=d.get("squad_id", ""),
        handoff_note=d.get("handoff_note", ""),
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
                trigger_comment_id: str = "", rerun_of_task_id: str = "",
                is_leader_task: bool = False, squad_id: str = "",
                handoff_note: str = "") -> Task:
        with self._lock:
            task = Task(
                id=str(uuid.uuid4()),
                issue_id=issue_id,
                assignee_type=assignee_type,
                assignee_id=assignee_id,
                trigger_comment_id=trigger_comment_id,
                rerun_of_task_id=rerun_of_task_id,
                is_leader_task=is_leader_task,
                squad_id=squad_id,
                handoff_note=handoff_note,
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

    def has_pending(self, issue_id: str, assignee_id: str,
                    is_leader_task: bool | None = None) -> bool:
        pending = {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.RUNNING}
        with self._lock:
            for task in self._tasks.values():
                if task.issue_id != issue_id:
                    continue
                if task.assignee_id != assignee_id:
                    continue
                if task.status not in pending:
                    continue
                if is_leader_task is not None and task.is_leader_task != is_leader_task:
                    continue
                return True
        return False

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
                 runner: AgentRunner,
                 runtime_id: str = "local",
                 agent_id: str | None = None,
                 poll_interval: float = 2.0,
                 session_store: "SessionStore | None" = None,
                 hub: "RealtimeHub | None" = None,
                 skill_loader: "SkillLoader | None" = None) -> None:
        # s10:1:N daemon。runtime_id 标识本 daemon(托管本 runtime 上所有 agent);
        # agent_id 仅为兼容 s02 的 1:1 模式保留,claim_next 不再按它过滤。
        self.issue_store = issue_store
        self.task_store = task_store
        self.runner = runner
        self.runtime_id = runtime_id
        self.agent_id = agent_id
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
        """s10 1:N:认领所有 assignee_type=="agent" 且 queued 的 task
        (本 daemon 托管本 runtime 上所有 agent,不再按 agent_id 过滤)。"""
        candidates = [
            t for t in self.task_store.list()
            if t.assignee_type == "agent"
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
                               "agent_id": task.assignee_id})
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


# ===========================================================================
# s09 — Autopilot + AutopilotStore + Scheduler(cron 调度 → 建 issue+task)
# ===========================================================================
@dataclass
class Autopilot:
    id: str
    cron: str               # 标准 5 段 cron 表达式
    agent_id: str
    title: str
    template: str = ""       # issue body 模板
    next_run: str = ""       # ISO-8601 下次运行时间
    fail_count: int = 0
    enabled: bool = True
    timezone: str = "UTC"


def _autopilot_to_dict(ap: Autopilot) -> dict:
    return asdict(ap)


def _autopilot_from_dict(d: dict) -> Autopilot:
    return Autopilot(
        id=d["id"],
        cron=d["cron"],
        agent_id=d["agent_id"],
        title=d["title"],
        template=d.get("template", ""),
        next_run=d.get("next_run", ""),
        fail_count=d.get("fail_count", 0),
        enabled=d.get("enabled", True),
        timezone=d.get("timezone", "UTC"),
    )


class AutopilotStore:
    """autopilots.jsonl 持久化:加载/保存/更新 autopilot。

    同 IssueStore/TaskStore 的 JSONL snapshot-append + last-wins 模式。
    """

    def __init__(self, data_dir: str | None = None) -> None:
        self._aps: dict[str, Autopilot] = {}
        self._lock = threading.Lock()
        self._path: str | None = None
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self._path = os.path.join(data_dir, "autopilots.jsonl")
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
                ap = _autopilot_from_dict(rec)
                self._aps[ap.id] = ap   # last-wins

    def _persist(self, ap: Autopilot) -> None:
        if not self._path:
            return
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_autopilot_to_dict(ap), ensure_ascii=False) + "\n")

    def list(self) -> list[Autopilot]:
        with self._lock:
            return list(self._aps.values())

    def get(self, ap_id: str) -> Autopilot | None:
        with self._lock:
            return self._aps.get(ap_id)

    def save(self, ap: Autopilot) -> Autopilot:
        """新增或更新 + _persist。"""
        with self._lock:
            self._aps[ap.id] = ap
            self._persist(ap)
        return ap

    def update(self, ap_id: str, **fields) -> Autopilot | None:
        with self._lock:
            ap = self._aps.get(ap_id)
            if ap is None:
                return None
            for k, v in fields.items():
                if hasattr(ap, k):
                    setattr(ap, k, v)
            self._persist(ap)
            return ap


class Scheduler:
    """cron 调度器:tick 检查到期 → 建 issue+task → 推进 next_run。

    tick(now=None) 接受可选 now 参数便于测试注入固定时间;None 时用
    datetime.datetime.now(timezone.utc)。
    """

    def __init__(self, autopilot_store: AutopilotStore, issue_store: IssueStore,
                 task_store: TaskStore, agent_id: str = "agent-1") -> None:
        self.ap_store = autopilot_store
        self.issue_store = issue_store
        self.task_store = task_store
        self.agent_id = agent_id

    def tick(self, now=None) -> int:
        """检查所有 enabled 的 autopilot,到期的触发。返回触发数。"""
        import datetime
        if now is None:
            now = datetime.datetime.now(datetime.timezone.utc)
        triggered = 0
        for ap in self.ap_store.list():
            if not ap.enabled:
                continue
            if not ap.next_run:
                # 初始化 next_run
                self._compute_next_run(ap, now)
                continue
            next_run = self._parse_iso(ap.next_run)
            if next_run <= now:
                self._fire(ap, now)
                triggered += 1
        return triggered

    def _fire(self, ap: Autopilot, now) -> None:
        """到期触发:建 issue+task,推进 next_run,清零 fail_count。"""
        # 建 issue
        issue = self.issue_store.create("ws-1", ap.title, body=ap.template,
                                        assignee_type="agent", assignee_id=ap.agent_id)
        # 入队 task
        self.task_store.enqueue(issue.id, "agent", ap.agent_id)
        # 推进 next_run(无论"执行"成功失败,触发即推进)
        self._compute_next_run(ap, now)
        # fail_count 清零(触发成功;实际"执行"结果由 daemon 异步返回)
        ap.fail_count = 0
        self.ap_store.save(ap)

    def _compute_next_run(self, ap: Autopilot, now) -> None:
        """用 croniter 算 next_run。"""
        import datetime
        tz = self._get_tz(ap.timezone)
        local_now = now.astimezone(tz) if tz else now
        cron = croniter(ap.cron, local_now)
        next_dt = cron.get_next(datetime.datetime)
        ap.next_run = next_dt.isoformat()
        self.ap_store.save(ap)

    def _parse_iso(self, iso_str: str):
        import datetime
        return datetime.datetime.fromisoformat(iso_str)

    def _get_tz(self, tz_name: str):
        import datetime
        if tz_name == "UTC" or not tz_name:
            return datetime.timezone.utc
        try:
            import zoneinfo
            return zoneinfo.ZoneInfo(tz_name)
        except Exception:
            return datetime.timezone.utc

    def mark_fail(self, ap_id: str) -> None:
        """daemon 执行失败时调:fail_count+1,连续 5 次禁用。"""
        ap = self.ap_store.get(ap_id)
        if not ap:
            return
        ap.fail_count += 1
        if ap.fail_count >= 5:
            ap.enabled = False
        self.ap_store.save(ap)

    def mark_success(self, ap_id: str) -> None:
        """daemon 执行成功时调:fail_count 清零。"""
        ap = self.ap_store.get(ap_id)
        if not ap:
            return
        ap.fail_count = 0
        self.ap_store.save(ap)


# ===========================================================================
# s12 — Squads:leader-first 协调模型(教学版)
# multiCa 的 squad 不是负载均衡队列,而是一个可分配对象:
# issue 派给 squad -> 系统唤醒 leader -> leader 根据 roster/skills 委派 worker。
# ===========================================================================
@dataclass
class SquadMember:
    member_type: str            # "agent" | "member"
    member_id: str
    role: str = ""
    skills: list[str] = field(default_factory=list)


@dataclass
class Squad:
    id: str
    name: str
    leader_agent_id: str
    description: str = ""
    instructions: str = ""
    members: list[SquadMember] = field(default_factory=list)
    # 兼容早期 s12 测试/示例:传 member_agent_ids 时自动转成 agent members。
    member_agent_ids: list[str] = field(default_factory=list)

    def __post_init__(self):
        existing = {(m.member_type, m.member_id) for m in self.members}
        for agent_id in self.member_agent_ids:
            key = ("agent", agent_id)
            if key not in existing:
                self.members.append(SquadMember("agent", agent_id))
                existing.add(key)
        leader_key = ("agent", self.leader_agent_id)
        if leader_key not in existing:
            self.members.insert(0, SquadMember("agent", self.leader_agent_id, "leader"))


@dataclass
class SquadActivity:
    issue_id: str
    squad_id: str
    actor_id: str
    outcome: str                 # "action" | "no_action" | "failed"
    reason: str
    created_at: str = field(default_factory=now_iso)


class SquadRouter:
    """教学版 squad 编排器。

    它覆盖 multiCa squads 的核心路径:
    - squad 可以成为 issue assignee;
    - 派给 squad 时先入队 leader task;
    - leader task 通过 is_leader_task+squad_id 获得 squad briefing;
    - leader 再委派 worker agent;
    - worker 回报、child stage 完成都会重新唤醒 leader。
    """

    def __init__(self, issue_store: IssueStore, task_store: TaskStore):
        self.issue_store = issue_store
        self.task_store = task_store
        self._squads: dict[str, Squad] = {}
        self._activities: list[SquadActivity] = []

    def register(self, squad: Squad) -> None:
        self._squads[squad.id] = squad

    def get(self, squad_id: str) -> Squad | None:
        return self._squads.get(squad_id)

    def list_squads(self) -> list[Squad]:
        return list(self._squads.values())

    def assign_issue_to_squad(self, issue_id: str, squad_id: str,
                              handoff_note: str = "issue assigned to squad") -> Task | None:
        """把 issue 派给 squad,但真正入队的是 leader task。"""
        squad = self._squads.get(squad_id)
        if not squad:
            return None
        issue = self.issue_store.assign(issue_id, "squad", squad_id)
        return self._enqueue_leader(issue, squad, handoff_note=handoff_note)

    def route(self, issue_id: str, squad_id: str) -> str | None:
        """向后兼容旧 s12 名称:route 现在表示 route to leader。"""
        task = self.assign_issue_to_squad(issue_id, squad_id)
        return task.assignee_id if task else None

    def build_leader_briefing(self, squad_id: str) -> str:
        """生成 leader claim 时应追加到 instructions 的 squad briefing。"""
        squad = self._require_squad(squad_id)
        lines = [
            "## Squad Operating Protocol",
            "",
            "You are the squad leader. Coordinate the work; do not silently do every task yourself.",
            "Choose the right member from the roster, delegate with a mention-style instruction,",
            "record the decision, then stop and wait for the next trigger.",
            "",
            "## Squad Roster",
            "",
        ]
        for member in squad.members:
            name = member.member_id
            role = f', role: "{member.role}"' if member.role else ""
            skills = f" — skills: {', '.join(member.skills)}" if member.skills else ""
            leader = " (leader)" if member.member_type == "agent" and member.member_id == squad.leader_agent_id else ""
            mention = f"[@{name}](mention://{member.member_type}/{member.member_id})"
            lines.append(f"- {name}{leader} — {member.member_type}{role}{skills} — `{mention}`")
        if squad.instructions.strip():
            lines += ["", f"## Squad Instructions ({squad.name})", "", squad.instructions.strip()]
        return "\n".join(lines)

    def delegate(self, issue_id: str, squad_id: str, member_agent_id: str,
                 note: str) -> Task:
        """leader 把当前 issue 委派给 squad 内某个 agent 成员。"""
        squad = self._require_squad(squad_id)
        self._require_agent_member(squad, member_agent_id)
        mention = f"[@{member_agent_id}](mention://agent/{member_agent_id})"
        self.issue_store.comment(issue_id, f"{mention} {note}".strip())
        return self.task_store.enqueue(
            issue_id, "agent", member_agent_id,
            trigger_comment_id=note,
            is_leader_task=False,
            squad_id=squad.id,
            handoff_note=note,
        )

    def worker_report(self, issue_id: str, squad_id: str, worker_agent_id: str,
                      text: str) -> Task | None:
        """worker 评论后唤醒 leader;已有 pending leader task 时去重。"""
        squad = self._require_squad(squad_id)
        self._require_agent_member(squad, worker_agent_id)
        self.issue_store.comment(issue_id, f"agent:{worker_agent_id}: {text}")
        if self.task_store.has_pending(issue_id, squad.leader_agent_id, is_leader_task=True):
            return None
        issue = self.issue_store.get(issue_id)
        if issue is None:
            raise KeyError(f"issue 不存在:{issue_id}")
        return self._enqueue_leader(issue, squad, handoff_note=f"worker {worker_agent_id} reported")

    def record_activity(self, issue_id: str, squad_id: str, actor_id: str,
                        outcome: str, reason: str) -> SquadActivity:
        if outcome not in {"action", "no_action", "failed"}:
            raise ValueError("outcome must be action/no_action/failed")
        activity = SquadActivity(issue_id, squad_id, actor_id, outcome, reason)
        self._activities.append(activity)
        self.issue_store.comment(issue_id, f"squad activity: {outcome} — {reason}")
        return activity

    def list_activities(self, issue_id: str | None = None) -> list[SquadActivity]:
        return [
            a for a in self._activities
            if issue_id is None or a.issue_id == issue_id
        ]

    def create_child_issue(self, parent_issue_id: str, title: str,
                           assignee_type: str, assignee_id: str,
                           stage: int | None = None,
                           status: IssueStatus = IssueStatus.TODO) -> Issue:
        parent = self.issue_store.get(parent_issue_id)
        if parent is None:
            raise KeyError(f"parent issue 不存在:{parent_issue_id}")
        child = self.issue_store.create(
            parent.workspace_id, title,
            assignee_type=assignee_type,
            assignee_id=assignee_id,
            parent_issue_id=parent_issue_id,
            stage=stage,
            status=status,
        )
        # multiCa 中,一个 todo child issue 如果直接 assign 给 agent,会触发该 agent。
        # s12 用一条普通 worker task 表达这件事;如果 child 只是 backlog,则先不派发。
        if assignee_type == "agent" and status == IssueStatus.TODO:
            self.task_store.enqueue(
                child.id,
                "agent",
                assignee_id,
                is_leader_task=False,
                squad_id=parent.assignee_id if parent.assignee_type == "squad" else "",
                handoff_note=f"child issue stage {stage}: {title}" if stage is not None else f"child issue: {title}",
            )
        return child

    def notify_child_done(self, child_issue_id: str) -> Task | None:
        """child issue 进入 terminal 后,若 stage barrier 关闭,唤醒父 issue 的 leader。"""
        child = self.issue_store.get(child_issue_id)
        if child is None or not child.parent_issue_id:
            return None
        if not self._is_terminal(child.status):
            return None
        parent = self.issue_store.get(child.parent_issue_id)
        if parent is None:
            return None
        if parent.status in {IssueStatus.DONE, IssueStatus.CANCELLED, IssueStatus.BACKLOG}:
            return None
        if parent.assignee_type != "squad":
            return None
        squad = self._squads.get(parent.assignee_id)
        if not squad or not self._stage_barrier_closed(parent.id, child):
            return None
        if self.task_store.has_pending(parent.id, squad.leader_agent_id, is_leader_task=True):
            return None
        self.issue_store.comment(parent.id, f"system: child stage complete after '{child.title}'")
        return self._enqueue_leader(parent, squad, handoff_note="child stage completed")

    def member_status(self, squad_id: str) -> list[dict]:
        """教学版成员状态:从 active task 推导 working/idle/human。"""
        squad = self._require_squad(squad_id)
        rows = []
        for member in squad.members:
            if member.member_type != "agent":
                rows.append({"member_id": member.member_id, "member_type": "member",
                             "status": "human", "active_issues": []})
                continue
            active = [
                t.issue_id for t in self.task_store.list()
                if t.assignee_id == member.member_id
                and t.status in {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.RUNNING}
            ]
            rows.append({
                "member_id": member.member_id,
                "member_type": "agent",
                "status": "working" if active else "idle",
                "active_issues": active,
            })
        return rows

    def _enqueue_leader(self, issue: Issue, squad: Squad, handoff_note: str) -> Task:
        return self.task_store.enqueue(
            issue.id,
            "agent",
            squad.leader_agent_id,
            is_leader_task=True,
            squad_id=squad.id,
            handoff_note=handoff_note,
        )

    def _require_squad(self, squad_id: str) -> Squad:
        squad = self._squads.get(squad_id)
        if not squad:
            raise KeyError(f"squad 不存在:{squad_id}")
        return squad

    def _require_agent_member(self, squad: Squad, agent_id: str) -> None:
        for member in squad.members:
            if member.member_type == "agent" and member.member_id == agent_id:
                return
        raise PermissionError(f"agent {agent_id} is not in squad {squad.id}")

    def _is_terminal(self, status: IssueStatus) -> bool:
        return status in {IssueStatus.DONE, IssueStatus.CANCELLED}

    def _stage_barrier_closed(self, parent_issue_id: str, completed: Issue) -> bool:
        children = self.issue_store.children(parent_issue_id)
        if not children:
            return False
        staged = any(c.stage is not None for c in children)
        if not staged:
            return all(self._is_terminal(c.status) for c in children)
        if completed.stage is None:
            return False
        for child in children:
            if child.stage is None:
                continue
            if child.stage <= completed.stage and not self._is_terminal(child.status):
                return False
        return True


def _short_task(task: Task | None) -> str:
    if task is None:
        return "none"
    role = "leader" if task.is_leader_task else "worker"
    return f"{task.assignee_id} ({role}, {task.status.value}, note={task.handoff_note})"


def _complete_issue_work(issue_store: IssueStore, task_store: TaskStore, issue: Issue) -> None:
    for task in task_store.list(issue.id):
        if task.status in {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.RUNNING}:
            task_store.complete(task.id)
    if issue.status == IssueStatus.TODO:
        issue_store.transition(issue.id, IssueStatus.IN_PROGRESS)
    if issue.status == IssueStatus.IN_PROGRESS:
        issue_store.transition(issue.id, IssueStatus.DONE)


def _print_member_status(router: SquadRouter, squad_id: str) -> None:
    print("member status:")
    for row in router.member_status(squad_id):
        active = ",".join(i[:8] for i in row["active_issues"]) or "-"
        print(f"  - {row['member_id']}: {row['status']} active={active}")


def main(data_dir=None) -> int:
    # Demo 使用内存 store,保证每次运行输出同一条完整流程,不受 workspace/*.jsonl 历史影响。
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    is_ = IssueStore(data_dir=data_dir) if data_dir else IssueStore()
    ts = TaskStore(data_dir=data_dir) if data_dir else TaskStore()
    router = SquadRouter(is_, ts)
    squad = Squad(
        id="sq-1",
        name="Frontend Team",
        leader_agent_id="agent-lead",
        instructions="Prefer accessibility fixes before visual polish.",
        members=[
            SquadMember("agent", "agent-lead", "leader", ["planning", "review"]),
            SquadMember("agent", "agent-ui", "frontend", ["react", "css"]),
            SquadMember("agent", "agent-test", "qa", ["playwright"]),
        ],
    )
    router.register(squad)

    print("=== s12 demo: squad leader lifecycle ===")
    parent = is_.create("ws-1", "ship accessible landing page")
    first_leader = router.assign_issue_to_squad(parent.id, "sq-1")
    print(f"1) assign parent to squad: issue.assignee=squad:{parent.assignee_id}")
    print(f"   leader wake: {_short_task(first_leader)}")
    print("   briefing roster:")
    for line in router.build_leader_briefing("sq-1").splitlines():
        if line.startswith("- "):
            print(f"     {line}")

    # Leader 本轮只规划和派发,然后结束等待 worker / child 结果。
    router.record_activity(parent.id, "sq-1", "agent-lead", "action", "created Stage 1 child issues")
    ts.complete(first_leader.id)
    stage1_ui = router.create_child_issue(parent.id, "Stage 1: implement hero", "agent", "agent-ui", stage=1)
    stage1_test = router.create_child_issue(parent.id, "Stage 1: add smoke test", "agent", "agent-test", stage=1)
    stage2_polish = router.create_child_issue(
        parent.id,
        "Stage 2: accessibility polish after smoke test",
        "agent",
        "agent-ui",
        stage=2,
        status=IssueStatus.BACKLOG,
    )
    print("2) leader creates Stage 1 children and stops")
    for child in [stage1_ui, stage1_test, stage2_polish]:
        print(f"   child stage={child.stage} status={child.status.value} assignee={child.assignee_id}: {child.title}")
    _print_member_status(router, "sq-1")

    _complete_issue_work(is_, ts, stage1_ui)
    wake = router.notify_child_done(stage1_ui.id)
    print("3) agent-ui completes first Stage 1 child")
    print(f"   stage barrier: open, leader wake={_short_task(wake)}")

    _complete_issue_work(is_, ts, stage1_test)
    wake = router.notify_child_done(stage1_test.id)
    print("4) agent-test completes second Stage 1 child")
    print(f"   stage barrier: closed, leader wake={_short_task(wake)}")

    if wake:
        ts.complete(wake.id)
        router.record_activity(parent.id, "sq-1", "agent-lead", "action", "reviewed Stage 1 and promoted Stage 2")
        # 教学版把 Stage 2 从 backlog 推到 todo,再派发 worker task。
        is_.transition(stage2_polish.id, IssueStatus.TODO)
        ts.enqueue(
            stage2_polish.id,
            "agent",
            stage2_polish.assignee_id,
            squad_id="sq-1",
            handoff_note="promoted Stage 2 after Stage 1 closed",
        )
    print("5) leader reviews Stage 1 results and promotes Stage 2")
    print(f"   Stage 2 status={stage2_polish.status.value}, worker task={_short_task(ts.list(stage2_polish.id)[-1])}")
    _print_member_status(router, "sq-1")

    print("6) parent timeline comments:")
    for comment in is_.get(parent.id).comments:
        print(f"   - {comment}")
    return 0



# ===========================================================================
# s13 — Learning Console (Mission Control UI + 25 API endpoints)
# ===========================================================================
# 本节在 s12 verbatim 之上加入:
# - 序列化 helpers(_squad_to_dict / _skill_to_dict / _runtime_to_dict / _member_to_dict)
# - create_console_app():闭包持有全部 stores/services,FastAPI 路由 ~25 个端点
# - CONSOLE_HTML:内联 dark + amber 主题 / CSS Grid / 10 个页面段 / vanilla JS
# - main():内存装配(IssueStore/TaskStore/SessionStore/MemberStore/AutopilotStore
#           + RealtimeHub + SkillLoader + Scheduler + SquadRouter + Daemon(StubRunner)
#           + PathDetector → Runtime + agents)→ uvicorn.run(port=8000)
# s07 的 create_app / HTML_PAGE 保留为内部 helper,不再作为主入口。
# ===========================================================================


def _squad_to_dict(s: "Squad") -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "leader_agent_id": s.leader_agent_id,
        "member_agent_ids": s.member_agent_ids,
        "members": [
            {"member_type": m.member_type, "member_id": m.member_id,
             "role": m.role, "skills": list(m.skills)}
            for m in s.members
        ],
        "description": s.description,
        "instructions": s.instructions,
    }


def _skill_to_dict(s: "Skill") -> dict:
    return {
        "name": s.name,
        "description": s.description,
        "match": s.match,
        "keywords": [s.match] if s.match else [],
        "path": s.path,
        "body": (s.body or "")[:200],
    }


def _runtime_to_dict(r: "Runtime") -> dict:
    return {"id": r.id, "name": r.name, "clis": list(r.clis)}


def _member_to_dict(m: "Member") -> dict:
    return {"id": m.id, "name": m.name}


def _agent_to_dict(a: "Agent") -> dict:
    return {"id": a.id, "name": a.name, "cli": a.cli, "runtime_id": a.runtime_id}


CONSOLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mica — Mission Control</title>
<style>
  :root{
    --bg:#0d1117; --surface:#161b22; --surface2:#1c2230; --border:#30363d;
    --text:#e6edf3; --muted:#8b949e; --accent:#f0a050;
    --ok:#3fb950; --warn:#d29922; --err:#f85149; --info:#58a6ff;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    --sans:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
  body{display:grid;grid-template-rows:48px 1fr 44px;grid-template-areas:"top" "mid" "bottom"}
  .top{grid-area:top;display:flex;align-items:center;gap:12px;padding:0 16px;background:var(--surface);border-bottom:1px solid var(--border)}
  .top .brand{font-weight:700;color:var(--accent);letter-spacing:.5px}
  .top select,.top .pill{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:6px;font-size:12px}
  .top .pill{font-family:var(--mono)}
  .top .ws-dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:4px}
  .top .ws-dot.off{background:var(--err)}
  .mid{grid-area:mid;display:grid;grid-template-columns:160px 1fr 320px;overflow:hidden}
  .nav{background:var(--surface);border-right:1px solid var(--border);padding:8px 0;overflow:auto}
  .nav .item{padding:8px 16px;cursor:pointer;color:var(--muted);border-left:3px solid transparent;font-size:13px}
  .nav .item:hover{background:var(--surface2);color:var(--text)}
  .nav .item.active{color:var(--accent);border-left-color:var(--accent);background:var(--surface2)}
  .main{display:grid;grid-template-rows:1fr 220px;overflow:hidden}
  .split{display:grid;grid-template-columns:40% 60%;overflow:hidden;border-right:1px solid var(--border)}
  .panel{padding:12px 16px;overflow:auto;background:var(--bg)}
  .panel h2{font-size:13px;color:var(--accent);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;border-bottom:1px solid var(--border);padding-bottom:6px}
  .list-row{display:grid;grid-template-columns:90px 80px 100px 1fr 90px;gap:8px;padding:6px 8px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:12px;cursor:pointer}
  .list-row:hover{background:var(--surface2)}
  .list-row.head{color:var(--muted);cursor:default;font-weight:600;background:var(--surface)}
  .list-row.head:hover{background:var(--surface)}
  .badge{display:inline-block;padding:1px 6px;border-radius:8px;font-size:10px;font-family:var(--mono)}
  .b-todo,.b-backlog{background:rgba(210,153,34,.18);color:var(--warn)}
  .b-in_progress{background:rgba(88,166,255,.18);color:var(--info)}
  .b-in_review{background:rgba(88,166,255,.12);color:var(--info)}
  .b-done{background:rgba(63,185,80,.18);color:var(--ok)}
  .b-blocked,.b-failed{background:rgba(248,81,73,.18);color:var(--err)}
  .b-queued{background:rgba(139,148,158,.18);color:var(--muted)}
  .b-running,.b-dispatched{background:rgba(88,166,255,.18);color:var(--info)}
  .b-completed{background:rgba(63,185,80,.18);color:var(--ok)}
  .detail{font-family:var(--mono);font-size:12px;line-height:1.6;color:var(--text)}
  .detail h3{font-family:var(--sans);font-size:14px;color:var(--accent);margin:8px 0 4px}
  .detail pre{background:var(--surface);border:1px solid var(--border);padding:8px;overflow:auto;border-radius:4px;font-size:11px;color:var(--text)}
  .detail .k{color:var(--info)} .detail .v{color:var(--text)}
  .timeline{padding:8px 0;overflow:auto;background:var(--surface)}
  .timeline h2{margin-left:16px}
  .tl-track{display:flex;align-items:center;gap:0;padding:12px 16px;overflow-x:auto}
  .tl-node{flex:0 0 auto;min-width:90px;text-align:center;font-family:var(--mono);font-size:10px}
  .tl-dot{width:12px;height:12px;border-radius:50%;background:var(--accent);margin:0 auto 4px;border:2px solid var(--bg)}
  .tl-dot.done{background:var(--ok)} .tl-dot.run{background:var(--info)} .tl-dot.err{background:var(--err)}
  .tl-line{flex:0 0 24px;height:2px;background:var(--border);align-self:center}
  .inspector{background:var(--surface);border-left:1px solid var(--border);padding:12px 16px;overflow:auto}
  .inspector h2{font-size:13px;color:var(--accent);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
  .inspector pre{background:var(--bg);border:1px solid var(--border);padding:8px;border-radius:4px;font-family:var(--mono);font-size:11px;overflow:auto;max-height:50%}
  .inspector .rel{margin-top:8px;font-size:12px;font-family:var(--mono)}
  .inspector .rel a{color:var(--info);cursor:pointer;text-decoration:none;display:block;padding:2px 0}
  .inspector .rel a:hover{color:var(--accent)}
  .bottom{grid-area:bottom;display:flex;align-items:center;gap:8px;padding:0 16px;background:var(--surface);border-top:1px solid var(--border)}
  .btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-family:var(--sans)}
  .btn:hover{border-color:var(--accent);color:var(--accent)}
  .btn.primary{background:rgba(240,160,80,.15);border-color:var(--accent);color:var(--accent)}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}
  .card{background:var(--surface);border:1px solid var(--border);padding:12px;border-radius:6px}
  .card .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  .card .v{font-family:var(--mono);font-size:24px;color:var(--accent);margin:4px 0}
  .card .sub{font-size:11px;color:var(--muted);font-family:var(--mono)}
  .feed{background:var(--surface);border:1px solid var(--border);padding:8px;border-radius:6px;font-family:var(--mono);font-size:11px}
  .feed .row{padding:2px 0;color:var(--muted)} .feed .row b{color:var(--text)}
  form.inline{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
  form.inline input,form.inline select{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-size:12px;font-family:var(--mono)}
  .roster{font-family:var(--mono);font-size:12px;line-height:1.8}
  .roster .m{color:var(--info)} .roster .lead{color:var(--accent)}
  .empty{color:var(--muted);font-style:italic;padding:8px 0}
</style>
</head>
<body>
<div class="top">
  <span class="brand">◆ mica</span>
  <select id="ws" title="workspace"></select>
  <select id="member" title="member"></select>
  <span class="pill" id="rt-pill">rt-1</span>
  <span class="pill"><span class="ws-dot" id="ws-dot"></span><span id="ws-state">disconnected</span></span>
</div>
<div class="mid">
  <div class="nav" id="nav">
    <div class="item active" data-v="overview">Overview</div>
    <div class="item" data-v="issues">Issues</div>
    <div class="item" data-v="tasks">Tasks</div>
    <div class="item" data-v="trace">Trace</div>
    <div class="item" data-v="skills">Skills</div>
    <div class="item" data-v="autopilot">Autopilot</div>
    <div class="item" data-v="runtimes">Runtime</div>
    <div class="item" data-v="workspaces">Workspaces</div>
    <div class="item" data-v="squads">Squads</div>
  </div>
  <div class="main">
    <div class="split">
      <div class="panel" id="list-panel"><h2 id="list-title">Overview</h2><div id="list"></div></div>
      <div class="panel" id="detail-panel"><h2 id="detail-title">Detail</h2><div class="detail" id="detail"><p class="empty">select an object</p></div></div>
    </div>
    <div class="panel timeline" id="tl-panel"><h2>Issue Lifecycle Timeline</h2><div class="tl-track" id="tl-track"><p class="empty">select an issue to see its lifecycle</p></div></div>
  </div>
  <div class="inspector">
    <h2>Inspector</h2>
    <pre id="ins-json">{}</pre>
    <div class="rel" id="ins-rel"></div>
  </div>
</div>
<div class="bottom">
  <button class="btn primary" onclick="runDaemonOnce()">▶ Run daemon once</button>
  <button class="btn" onclick="autopilotTick()">⏰ Autopilot tick</button>
  <button class="btn" onclick="seedDemo()">📋 Seed demo</button>
  <button class="btn" onclick="resetDemo()">↺ Reset</button>
  <span style="flex:1"></span>
  <span class="pill" id="status-pill" style="color:var(--muted)">ready</span>
</div>
<script>
const state = {view:'overview', selType:null, selId:null, ws:'ws-1', member:'alice', wsSocket:null};
function setStatus(msg, color){const el=document.getElementById('status-pill');el.textContent=msg;el.style.color=color||'var(--muted)';}
async function api(path, opts){const r=await fetch(path, opts||{});return r.json();}
function el(id){return document.getElementById(id);}
function badge(s){const cls='b-'+s;return '<span class="badge '+cls+'">'+s+'</span>';}
function short(id){return id?id.slice(0,8):'-';}

async function loadView(v){
  state.view=v||state.view;
  document.querySelectorAll('.nav .item').forEach(n=>n.classList.toggle('active', n.dataset.v===state.view));
  const list=el('list'); const title=el('list-title');
  title.textContent=state.view.charAt(0).toUpperCase()+state.view.slice(1);
  list.innerHTML='<p class="empty">loading...</p>';
  if(state.view==='overview'){
    const d=await api('/api/overview');
    let cards='<div class="cards">';
    cards+=card('Issues', d.issues, JSON.stringify(d.issue_counts||{}));
    cards+=card('Tasks', d.tasks, JSON.stringify(d.task_counts||{}));
    cards+=card('Runtimes', d.runtimes, 'local');
    cards+=card('Autopilots', d.autopilots, 'enabled');
    cards+='</div>';
    let feed='<div class="feed"><b>Recent Activity</b>';
    (d.recent_activity||[]).forEach(r=>{feed+='<div class="row"><b>'+r.ts.slice(11,19)+'</b> '+r.type+' '+short(r.id)+' '+r.status+' ('+short(r.issue_id)+')</div>';});
    if(!d.recent_activity||!d.recent_activity.length) feed+='<div class="empty">no activity</div>';
    feed+='</div>';
    list.innerHTML=cards+feed;
  } else if(state.view==='issues'){
    const d=await api('/api/issues');
    list.innerHTML='<form class="inline" onsubmit="createIssue(event)"><input name="title" placeholder="title" required><select name="assignee_type"><option value="agent">agent</option><option value="squad">squad</option><option value="member">member</option></select><input name="assignee_id" placeholder="agent-1"><button class="btn primary">+ New</button></form>'+issuesTable(d);
  } else if(state.view==='tasks'){
    const d=await api('/api/tasks');
    list.innerHTML=tasksTable(d)+'<p style="margin-top:8px"><button class="btn" onclick="runDaemonOnce()">▶ Run daemon once</button></p>';
  } else if(state.view==='trace'){
    list.innerHTML='<p class="empty">select a task from Tasks view</p>';
  } else if(state.view==='skills'){
    const d=await api('/api/skills');
    let h='<form class="inline" onsubmit="previewSkill(event)"><input name="title" placeholder="issue title"><input name="body" placeholder="body"><button class="btn primary">Preview</button></form>';
    d.forEach(s=>{h+='<div style="border:1px solid var(--border);padding:8px;margin-bottom:8px;border-radius:4px"><b style="color:var(--accent)">'+s.name+'</b> — '+s.description+'<br><small style="color:var(--muted)">'+(s.keywords||[]).join(', ')+' · '+s.path+'</small><pre>'+s.body+'</pre></div>';});
    list.innerHTML=h||'<p class="empty">no skills</p>';
  } else if(state.view==='autopilot'){
    const d=await api('/api/autopilots');
    let h='<form class="inline" onsubmit="createAutopilot(event)"><input name="cron" placeholder="0 9 * * *" required><input name="agent_id" placeholder="agent-1" value="agent-1"><input name="title" placeholder="title" required><button class="btn primary">+ New</button></form>';
    h+='<div class="list-row head"><span>ID</span><span>Cron</span><span>Agent</span><span>Title</span><span>Next</span></div>';
    d.forEach(a=>{h+='<div class="list-row" onclick="selectObject(\'autopilot\',\''+a.id+'\')"><span>'+short(a.id)+'</span><span>'+a.cron+'</span><span>'+a.agent_id+'</span><span>'+a.title+'</span><span>'+(a.next_run||'').slice(11,16)+'</span></div>';});
    list.innerHTML=h+'<p style="margin-top:8px"><button class="btn" onclick="autopilotTick()">⏰ Tick now</button></p>';
  } else if(state.view==='runtimes'){
    const d=await api('/api/runtimes');
    let h='<div class="cards">';
    d.forEach(r=>{h+=card(r.name, r.id, r.clis.join(', '));});
    h+='</div>';
    const a=await api('/api/agents');
    h+='<div class="list-row head"><span>Agent ID</span><span>Name</span><span>CLI</span><span>Tasks</span><span></span></div>';
    a.forEach(x=>{h+='<div class="list-row"><span>'+x.id+'</span><span>'+x.name+'</span><span>'+x.cli+'</span><span>'+x.runtime_id+'</span><span></span></div>';});
    list.innerHTML=h;
  } else if(state.view==='workspaces'){
    const d=await api('/api/workspaces');
    let h='<div class="list-row head"><span>WS ID</span><span>Slug</span><span>Members</span><span>Issues</span><span></span></div>';
    d.forEach(w=>{h+='<div class="list-row"><span>'+w.id+'</span><span>'+w.slug+'</span><span>'+(w.members||[]).join(', ')+'</span><span>'+w.issue_count+'</span><span></span></div>';});
    h+='<form class="inline" onsubmit="addMember(event)" style="margin-top:8px"><input name="member_id" placeholder="member id"><button class="btn">+ Add member</button></form>';
    list.innerHTML=h;
  } else if(state.view==='squads'){
    const d=await api('/api/squads');
    let h='<form class="inline" onsubmit="createSquad(event)"><input name="name" placeholder="squad name" required><input name="leader_agent_id" placeholder="agent-1" value="agent-1"><input name="member_agent_ids" placeholder="agent-2,agent-3"><button class="btn primary">+ New</button></form>';
    d.forEach(s=>{h+='<div style="border:1px solid var(--border);padding:8px;margin-bottom:8px;border-radius:4px"><b style="color:var(--accent)">'+s.name+'</b> ('+short(s.id)+') — leader: '+s.leader_agent_id+'<div class="roster">';
      (s.members||[]).forEach(m=>{h+='<div class="'+(m.member_id===s.leader_agent_id?'lead':'m')+'">@'+m.member_id+' ('+m.member_type+(m.role?', '+m.role:'')+')'+(m.skills&&m.skills.length?' ['+m.skills.join(',')+']':'')+'</div>';});
      h+='</div></div>';});
    list.innerHTML=h||'<p class="empty">no squads</p>';
  }
}
function card(k,v,sub){return '<div class="card"><div class="k">'+k+'</div><div class="v">'+v+'</div><div class="sub">'+(sub||'')+'</div></div>';}
function issuesTable(d){
  let h='<div class="list-row head"><span>ID</span><span>Status</span><span>Assignee</span><span>Title</span><span>Tasks</span></div>';
  d.forEach(i=>{h+='<div class="list-row" onclick="selectObject(\'issue\',\''+i.id+'\')"><span>'+short(i.id)+'</span><span>'+badge(i.status)+'</span><span>'+(i.assignee_type||'-')+':'+(i.assignee_id||'-')+'</span><span>'+i.title+'</span><span>'+(i.task_count||0)+'/'+(i.comment_count||0)+'</span></div>';});
  return h||'<p class="empty">no issues</p>';
}
function tasksTable(d){
  let h='<div class="list-row head" style="grid-template-columns:90px 90px 90px 80px 90px 1fr"><span>ID</span><span>Status</span><span>Issue</span><span>Agent</span><span>Session</span><span>Error</span></div>';
  d.forEach(t=>{h+='<div class="list-row" style="grid-template-columns:90px 90px 90px 80px 90px 1fr" onclick="selectObject(\'task\',\''+t.id+'\')"><span>'+short(t.id)+'</span><span>'+badge(t.status)+'</span><span>'+short(t.issue_id)+'</span><span>'+(t.assignee_id||'-')+'</span><span>'+short(t.session_id)+'</span><span>'+(t.error||'')+'</span></div>';});
  return h||'<p class="empty">no tasks</p>';
}

async function selectObject(type, id){
  state.selType=type; state.selId=id;
  let url='/api/'+type+'s/'+id; let data;
  if(type==='issue'){data=await api('/api/issues/'+id); renderTimeline(data);}
  else if(type==='task'){data=await api('/api/tasks/'+id); }
  else if(type==='autopilot'){data=await api('/api/autopilots'); data=(data||[]).find(x=>x.id===id)||{id};}
  else data={id};
  el('ins-json').textContent=JSON.stringify(data, null, 2);
  let rel='<h2 style="margin-top:8px">Related</h2>';
  if(type==='issue' && data){(data.tasks||[]).forEach(t=>rel+='<a onclick="selectObject(\'task\',\''+t.id+'\')">task '+short(t.id)+' '+t.status+'</a>');(data.children||[]).forEach(c=>rel+='<a onclick="selectObject(\'issue\',\''+c.id+'\')">child '+short(c.id)+' '+c.title+'</a>');}
  el('ins-rel').innerHTML=rel;
  el('detail').innerHTML='<pre>'+JSON.stringify(data, null, 2)+'</pre>';
  if(type==='issue') renderTrace(data);
}
function renderTimeline(issue){
  const track=el('tl-track');
  if(!issue){track.innerHTML='<p class="empty">no issue selected</p>';return;}
  const evs=[{label:'created', t:issue.created_at, cls:'done'}];
  if((issue.tasks||[]).length){evs.push({label:'task queued', cls:'run'}); evs.push({label:'claimed', cls:'run'});}
  const run=(issue.tasks||[]).find(t=>t.status==='running'||t.status==='completed'||t.status==='dispatched');
  if(run) evs.push({label:'running', cls:run.status==='completed'?'done':'run'});
  const cm=issue.status;
  if(cm==='in_review'||cm==='done') evs.push({label:cm, cls:'done'});
  if(cm==='blocked') evs.push({label:'blocked', cls:'err'});
  if((issue.comments||[]).length) evs.push({label:'comment', cls:'done'});
  let h=''; evs.forEach((e,i)=>{h+='<div class="tl-node"><div class="tl-dot '+e.cls+'"></div>'+e.label+'<br><small style="color:var(--muted)">'+(e.t?e.t.slice(11,16):'')+'</small></div>'; if(i<evs.length-1) h+='<div class="tl-line"></div>';});
  track.innerHTML=h;
}
function renderTrace(issue){el('tl-panel').querySelector('h2').textContent='Lifecycle — '+short(issue.id);}

async function createIssue(e){e.preventDefault();const f=new FormData(e.target);await fetch('/api/issues?'+new URLSearchParams({title:f.get('title'),assignee_type:f.get('assignee_type'),assignee_id:f.get('assignee_id')||'agent-1'}),{method:'POST'});loadView();}
async function createAutopilot(e){e.preventDefault();const f=new FormData(e.target);await fetch('/api/autopilots?'+new URLSearchParams({cron:f.get('cron'),agent_id:f.get('agent_id'),title:f.get('title')}),{method:'POST'});loadView();}
async function createSquad(e){e.preventDefault();const f=new FormData(e.target);await fetch('/api/squads?'+new URLSearchParams({name:f.get('name'),leader_agent_id:f.get('leader_agent_id'),member_agent_ids:f.get('member_agent_ids')}),{method:'POST'});loadView();}
async function previewSkill(e){e.preventDefault();const f=new FormData(e.target);const d=await fetch('/api/skills/preview?'+new URLSearchParams({title:f.get('title')||'',body:f.get('body')||''}),{method:'POST'}).then(r=>r.json());alert('Matched: '+(d.matched?d.matched.name:'none')+'\\n\\n'+(d.injected_prompt||''));}
async function addMember(e){e.preventDefault();const f=new FormData(e.target);await fetch('/api/workspaces/'+state.ws+'/members?member_id='+encodeURIComponent(f.get('member_id')),{method:'POST'});loadView();}
async function runDaemonOnce(){setStatus('running daemon...','var(--info)');const d=await api('/api/daemon/run-once',{method:'POST'});setStatus('daemon ran: '+d.run+' task(s)','var(--ok)');loadView();}
async function autopilotTick(){const d=await api('/api/autopilots/tick',{method:'POST'});setStatus('autopilot tick: '+d.triggered,'var(--ok)');loadView();}
async function seedDemo(){const d=await api('/api/demo/seed',{method:'POST'});setStatus('seeded: '+d.issues+' issues','var(--ok)');loadView();}
async function resetDemo(){const d=await api('/api/demo/reset',{method:'POST'});setStatus('reset','var(--warn)');loadView();}
function watchIssue(id){if(state.wsSocket) state.wsSocket.close();state.wsSocket=new WebSocket('ws://'+location.host+'/ws/issues/'+id);state.wsSocket.onopen=()=>{el('ws-dot').classList.remove('off');el('ws-state').textContent='connected';};state.wsSocket.onclose=()=>{el('ws-dot').classList.add('off');el('ws-state').textContent='disconnected';};state.wsSocket.onmessage=(e)=>{el('detail').innerHTML+='<div style="color:var(--info)">'+e.data+'</div>';};}
function switchWorkspace(w){state.ws=w;loadView();}
function switchMember(m){state.member=m;}
document.querySelectorAll('.nav .item').forEach(n=>n.addEventListener('click',()=>loadView(n.dataset.v)));
loadView('overview');
</script>
</body>
</html>
"""


def create_console_app(issue_store: IssueStore, task_store: TaskStore,
                       session_store, hub: RealtimeHub,
                       skill_loader=None, autopilot_store=None,
                       scheduler=None, member_store=None,
                       squad_router=None, daemon=None,
                       runtime=None) -> FastAPI:
    """构造 Mission Control app;路由闭包持有全部 stores/services。"""
    app = FastAPI(title="mica Mission Control")

    # --- 派生 agents(从 runtime.clis 映射)---
    _CLI_AGENT_MAP = {
        "claude": ("agent-1", "Claude"),
        "codex": ("agent-2", "Codex"),
        "copilot": ("agent-3", "Copilot"),
        "opencode": ("agent-4", "OpenCode"),
        "cursor-agent": ("agent-5", "Cursor"),
    }
    _agents: list[Agent] = []
    if runtime is not None:
        for cli in runtime.clis:
            aid, name = _CLI_AGENT_MAP.get(cli, (f"agent-{cli}", cli))
            _agents.append(Agent(id=aid, name=name, cli=cli, runtime_id=runtime.id))

    # --- 内部 workspaces 注册表(教学用,内存)---
    _workspaces: dict[str, Workspace] = {
        "ws-1": Workspace(id="ws-1", slug="default", name="Default"),
    }
    if member_store is not None:
        for m in ["alice", "bob"]:
            member_store.add_member("ws-1", m)

    # --- 派生 scheduler(若未注入)---
    if scheduler is None and autopilot_store is not None:
        scheduler = Scheduler(autopilot_store, issue_store, task_store)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return CONSOLE_HTML

    # --- Overview ---
    @app.get("/api/overview")
    def overview():
        issues = issue_store.list()
        tasks = task_store.list()
        ic: dict = {}
        for i in issues:
            ic[i.status.value] = ic.get(i.status.value, 0) + 1
        tc: dict = {}
        for t in tasks:
            tc[t.status.value] = tc.get(t.status.value, 0) + 1
        aps = autopilot_store.list() if autopilot_store else []
        recent = [
            {"ts": t.created_at, "type": "task", "id": t.id,
             "status": t.status.value, "issue_id": t.issue_id}
            for t in sorted(tasks, key=lambda x: x.created_at or "", reverse=True)[:8]
        ]
        return {
            "issues": len(issues), "issue_counts": ic,
            "tasks": len(tasks), "task_counts": tc,
            "autopilots": len(aps),
            "runtimes": 1 if runtime else 0,
            "agents": len(_agents),
            "recent_activity": recent,
        }

    # --- Issues ---
    @app.get("/api/issues")
    def list_issues():
        out = []
        for i in issue_store.list():
            d = _issue_to_dict(i)
            d["child_count"] = len(issue_store.children(i.id))
            d["task_count"] = len(task_store.list(i.id))
            d["comment_count"] = len(i.comments)
            latest = task_store.list(i.id)
            d["latest_task"] = _task_to_dict(latest[-1]) if latest else None
            out.append(d)
        return out

    @app.post("/api/issues")
    def create_issue(title: str = "", assignee_type: str = "agent",
                     assignee_id: str = "", body: str = "",
                     workspace_id: str = "ws-1"):
        issue = issue_store.create(
            workspace_id, title or "(untitled)", body=body,
            assignee_type=assignee_type, assignee_id=assignee_id)
        if assignee_type == "agent" and assignee_id:
            task_store.enqueue(issue.id, assignee_type, assignee_id)
        elif assignee_type == "squad" and assignee_id and squad_router:
            squad_router.assign_issue_to_squad(issue.id, assignee_id)
        return _issue_to_dict(issue)

    @app.get("/api/issues/{issue_id}")
    def get_issue(issue_id: str):
        issue = issue_store.get(issue_id)
        if issue is None:
            return {"error": f"issue not found: {issue_id}"}
        d = _issue_to_dict(issue)
        d["tasks"] = [_task_to_dict(t) for t in task_store.list(issue_id)]
        d["children"] = [_issue_to_dict(c) for c in issue_store.children(issue_id)]
        return d

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
        task = task_store.enqueue(
            issue_id, issue.assignee_type, issue.assignee_id,
            trigger_comment_id=text)
        return {"issue": _issue_to_dict(issue), "task_id": task.id}

    @app.post("/api/issues/{issue_id}/rerun")
    def rerun_issue(issue_id: str):
        try:
            task = issue_store.rerun_task(issue_id, task_store)
        except KeyError as e:
            return {"error": str(e)}
        return {"task_id": task.id}

    # --- Tasks ---
    @app.get("/api/tasks")
    def list_tasks(status: str | None = None):
        tasks = task_store.list()
        if status:
            tasks = [t for t in tasks if t.status.value == status]
        return [_task_to_dict(t) for t in tasks]

    @app.post("/api/daemon/run-once")
    def daemon_run_once():
        if daemon is None:
            return {"error": "daemon not configured", "run": 0}
        before = len([t for t in task_store.list()
                      if t.status.value == "completed"])
        daemon.run_once()
        after = len([t for t in task_store.list()
                     if t.status.value == "completed"])
        return {"run": after - before, "completed": after}

    @app.get("/api/tasks/{task_id}/events")
    def task_events(task_id: str):
        if session_store is None:
            return []
        return session_store.load(task_id)

    # --- Realtime ---
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

    # --- Skills ---
    @app.get("/api/skills")
    def list_skills():
        if skill_loader is None:
            return []
        return [_skill_to_dict(s) for s in skill_loader.list()]

    @app.post("/api/skills/preview")
    def skills_preview(title: str = "", body: str = ""):
        if skill_loader is None:
            return {"matched": None, "injected_prompt": "",
                    "reason": "no skill loader configured"}
        # 构造一个临时 issue 用于 select
        tmp = Issue(id="preview", workspace_id="ws-1", title=title, body=body)
        skill = skill_loader.select(tmp)
        if skill is None:
            return {"matched": None, "injected_prompt": title + "\n\n" + (body or ""),
                    "reason": "no skill matched"}
        injected = title + (f"\n\n{body}" if body else "") + "\n\n[Skill]\n" + skill.body
        return {"matched": _skill_to_dict(skill), "injected_prompt": injected}

    # --- Autopilot ---
    @app.get("/api/autopilots")
    def list_autopilots():
        if autopilot_store is None:
            return []
        return [_autopilot_to_dict(a) for a in autopilot_store.list()]

    @app.post("/api/autopilots")
    def create_autopilot(cron: str, agent_id: str, title: str,
                         template: str = "", next_run: str = "",
                         timezone: str = "UTC"):
        if autopilot_store is None:
            return {"error": "autopilot store not configured"}
        ap = Autopilot(
            id=str(uuid.uuid4()), cron=cron, agent_id=agent_id,
            title=title, template=template, next_run=next_run,
            timezone=timezone)
        autopilot_store.save(ap)
        return _autopilot_to_dict(ap)

    @app.post("/api/autopilots/tick")
    def autopilot_tick():
        if scheduler is None:
            return {"error": "scheduler not configured", "triggered": 0}
        n = scheduler.tick()
        return {"triggered": n}

    # --- Runtimes & Agents ---
    @app.get("/api/runtimes")
    def list_runtimes():
        if runtime is None:
            return []
        return [_runtime_to_dict(runtime)]

    @app.get("/api/agents")
    def list_agents():
        return [_agent_to_dict(a) for a in _agents]

    # --- Workspaces ---
    @app.get("/api/workspaces")
    def list_workspaces():
        out = []
        for ws in _workspaces.values():
            members = member_store.list_members(ws.id) if member_store else []
            n = len([i for i in issue_store.list() if i.workspace_id == ws.id])
            out.append({
                "id": ws.id, "slug": ws.slug, "name": ws.name,
                "members": members, "issue_count": n,
            })
        return out

    @app.post("/api/workspaces/{ws_id}/members")
    def add_workspace_member(ws_id: str, member_id: str):
        if member_store is None:
            return {"error": "member store not configured"}
        if ws_id not in _workspaces:
            return {"error": f"workspace not found: {ws_id}"}
        member_store.add_member(ws_id, member_id)
        return {"workspace_id": ws_id, "member_id": member_id,
                "members": member_store.list_members(ws_id)}

    # --- Squads ---
    @app.get("/api/squads")
    def list_squads():
        if squad_router is None:
            return []
        return [_squad_to_dict(s) for s in squad_router.list_squads()]

    @app.post("/api/squads")
    def create_squad(name: str, leader_agent_id: str,
                     member_agent_ids: str = "", description: str = "",
                     instructions: str = ""):
        if squad_router is None:
            return {"error": "squad router not configured"}
        ids = [s.strip() for s in member_agent_ids.split(",") if s.strip()] if member_agent_ids else []
        squad = Squad(
            id=str(uuid.uuid4()), name=name,
            leader_agent_id=leader_agent_id,
            description=description, instructions=instructions,
            member_agent_ids=ids,
        )
        squad_router.register(squad)
        return _squad_to_dict(squad)

    @app.post("/api/issues/{issue_id}/squad/children")
    def create_child_issue(issue_id: str, title: str,
                            assignee_type: str = "agent",
                            assignee_id: str = "", stage: int | None = None):
        if squad_router is None:
            return {"error": "squad router not configured"}
        try:
            child = squad_router.create_child_issue(
                issue_id, title, assignee_type, assignee_id, stage=stage)
        except KeyError as e:
            return {"error": str(e)}
        return _issue_to_dict(child)

    @app.post("/api/squads/{squad_id}/worker-report")
    def squad_worker_report(squad_id: str, issue_id: str,
                            worker_agent_id: str, text: str = ""):
        if squad_router is None:
            return {"error": "squad router not configured"}
        try:
            task = squad_router.worker_report(
                issue_id, squad_id, worker_agent_id, text)
        except (KeyError, PermissionError) as e:
            return {"error": str(e), "task_id": ""}
        return {"task_id": task.id if task else "",
                "reawakened": task is not None}

    # --- Demo ---
    @app.post("/api/demo/seed")
    def demo_seed():
        # 1 squad
        squad = Squad(
            id="sq-demo", name="Demo Team",
            leader_agent_id="agent-1",
            instructions="Ship the demo feature.",
            member_agent_ids=["agent-2"],
        )
        if squad_router:
            squad_router.register(squad)
        # 2-3 issues
        i1 = issue_store.create("ws-1", "fix login redirect bug",
                                assignee_type="agent", assignee_id="agent-1")
        task_store.enqueue(i1.id, "agent", "agent-1")
        i2 = issue_store.create("ws-1", "review the auth module",
                                assignee_type="agent", assignee_id="agent-2")
        task_store.enqueue(i2.id, "agent", "agent-2")
        i3 = issue_store.create("ws-1", "ship landing page",
                                assignee_type="squad", assignee_id="sq-demo")
        if squad_router:
            squad_router.assign_issue_to_squad(i3.id, "sq-demo")
        # 1 autopilot (next_run 过去 → 立即可 tick)
        if autopilot_store:
            past = now_iso()
            ap = Autopilot(id="ap-demo", cron="0 9 * * *", agent_id="agent-1",
                           title="daily standup review", next_run=past)
            autopilot_store.save(ap)
        return {
            "issues": len(issue_store.list()),
            "tasks": len(task_store.list()),
            "squads": len(squad_router.list_squads()) if squad_router else 0,
            "autopilots": len(autopilot_store.list()) if autopilot_store else 0,
        }

    @app.post("/api/demo/reset")
    def demo_reset():
        issue_store._issues.clear()
        task_store._tasks.clear()
        if autopilot_store is not None:
            autopilot_store._aps.clear()
        if member_store is not None:
            member_store._members.clear()
            member_store.add_member("ws-1", "alice")
            member_store.add_member("ws-1", "bob")
        if squad_router is not None:
            squad_router._squads.clear()
            squad_router._activities.clear()
        return {"issues": 0, "tasks": 0, "squads": 0, "autopilots": 0}

    return app


def main(data_dir: str | None = None) -> int:
    """Wire all in-memory stores + StubRunner + uvicorn on :8000."""
    import uvicorn
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    is_ = IssueStore(data_dir=data_dir) if data_dir else IssueStore()
    ts = TaskStore(data_dir=data_dir) if data_dir else TaskStore()
    ss = SessionStore(data_dir or "./workspace")
    hub = RealtimeHub()
    member_store = MemberStore(data_dir=data_dir) if data_dir else MemberStore()
    ap_store = AutopilotStore(data_dir=data_dir) if data_dir else AutopilotStore()
    skills_dir = os.path.join("workspace", "skills", "code-review")
    os.makedirs(skills_dir, exist_ok=True)
    skill_md = os.path.join(skills_dir, "SKILL.md")
    if not os.path.exists(skill_md):
        with open(skill_md, "w", encoding="utf-8") as f:
            f.write(EXAMPLE_SKILL_MD)
    skill_loader = SkillLoader(os.path.join("workspace", "skills"))
    scheduler = Scheduler(ap_store, is_, ts, agent_id="agent-1")
    squad_router = SquadRouter(is_, ts)
    daemon = Daemon(is_, ts, StubRunner(), runtime_id="rt-1",
                    session_store=ss, hub=hub, skill_loader=skill_loader)
    clis = PathDetector.detect() or ["claude", "codex"]
    runtime = Runtime(id="rt-1", name="local", clis=clis)
    app = create_console_app(
        is_, ts, ss, hub,
        skill_loader=skill_loader,
        autopilot_store=ap_store,
        scheduler=scheduler,
        member_store=member_store,
        squad_router=squad_router,
        daemon=daemon,
        runtime=runtime,
    )
    print("mica Mission Control → http://localhost:8000/")
    uvicorn.run(app, host="0.0.0.0", port=8000)
    return 0


if __name__ == "__main__":
    sys.exit(main())
