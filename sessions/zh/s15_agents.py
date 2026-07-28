"""s15 — Agent Profile(可配置执行主体)

本文件是 s01-s15 的累积教学代码。s15 在 s13 的 project/workdir 基础上,把 agent
从"runtime 派生的不可配置对象"升级为"可配置执行主体":

- Agent 增加 system_prompt(身份/边界/工作方式)和 skill_names(绑定的技能名);
- AgentStore(agents.jsonl 持久化)支持 create/get/list/update/bind_skill/unbind_skill;
- SkillLoader 使用 keywords 关键词召回,并新增 get(name) 按名查找,供 agent 绑定解析;
- merge_skills:合并 agent 绑定 skill + issue 自动匹配 skill,按 name 去重;
- Daemon.execute 按 task.assignee_id 查 agent,注入:
    agent.system_prompt  -> runner.run(append_system_prompt=...)  (--append-system-prompt)
    agent 绑定 skill + issue 匹配 skill  -> runner.run(skill_body=...)  (拼入 user prompt)
  两层:agent 绑定 skill = 稳定能力;issue 匹配 skill = 临时补充。
- 未配置 agent_store 时(agent_store=None)兼容旧逻辑:只走 issue 匹配 skill。
"""
# 上游(s13):project/workdir — issue 关联 project,daemon 解析 project.local_path
# 作为 task.workdir 传给 runner 的 cwd。
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
# s08 — Skill + SkillLoader(扫 workspace/skills/**/SKILL.md,frontmatter.keywords 关键词召回)
# s15:使用 keywords 关键词召回,新增 get(name) 供 agent 绑定解析。
# ===========================================================================
@dataclass
class Skill:
    name: str
    description: str
    keywords: str = ""     # 逗号分隔关键词;空串=不自动选中
    body: str = ""         # markdown 正文(skill 内容)
    path: str = ""         # SKILL.md 文件路径


class SkillLoader:
    """扫 workspace/skills/**/SKILL.md,按 frontmatter.keywords 关键词召回。

    扫描顺序:sorted glob(recursive)。首个命中即选中(无 keywords 字段的不自动选中)。
    s15 新增 get(name):按 name 查找(大小写不敏感),供 agent 绑定解析。
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
        """解析 SKILL.md:---name/description/keywords--- + 正文。"""
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
            keywords=fields.get("keywords", ""),
            body=body,
            path=path,
        )

    def select(self, issue) -> Skill | None:
        """用 keywords 关键词子串匹配 issue.title+body,取首个命中。无 keywords 字段的不选。
        大小写不敏感(text 与 keyword 都 lower)。"""
        text = (issue.title + "\n" + (issue.body or "")).lower()
        for skill in self._skills:
            if skill.keywords:
                for kw in skill.keywords.split(","):
                    if kw.strip().lower() in text:
                        return skill
        return None

    def get(self, name: str) -> Skill | None:
        """按 name 查找技能(大小写不敏感)。供 agent.skill_names 绑定解析。"""
        if not name:
            return None
        target = name.strip().lower()
        for skill in self._skills:
            if skill.name.strip().lower() == target:
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
    # s15:agent 可配置执行主体 —— 身份提示词 + 绑定的技能名。
    system_prompt: str = ""          # agent 身份/边界/工作方式 -> --append-system-prompt
    skill_names: list[str] = field(default_factory=list)  # 绑定的 skill 名(按 SkillLoader.get 解析)


def _agent_to_dict(a: Agent) -> dict:
    d = asdict(a)
    return d


def _agent_from_dict(d: dict) -> Agent:
    return Agent(
        id=d["id"],
        name=d.get("name", ""),
        cli=d.get("cli", "claude"),
        runtime_id=d.get("runtime_id", "local"),
        system_prompt=d.get("system_prompt", ""),
        skill_names=list(d.get("skill_names", [])),
    )


# ===========================================================================
# s15 — AgentStore(agents.jsonl snapshot-append,与 ProjectStore 同模式)
# ===========================================================================
class AgentStore:
    """可配置 agent 持久化。data_dir 给定则 agents.jsonl snapshot-append(last-wins 重放)。
    id 由调用方给(允许 agent-ui 这种语义 id),不强制 uuid。"""

    def __init__(self, data_dir: str | None = None) -> None:
        self._agents: dict[str, Agent] = {}
        self._lock = threading.Lock()
        self._path: str | None = None
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self._path = os.path.join(data_dir, "agents.jsonl")
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
                agent = _agent_from_dict(rec)
                self._agents[agent.id] = agent   # last-wins

    def _persist(self, agent: Agent) -> None:
        if not self._path:
            return
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(agent), ensure_ascii=False) + "\n")

    def create(self, id: str, name: str, cli: str = "claude",
               runtime_id: str = "local", system_prompt: str = "",
               skill_names: list[str] | None = None) -> Agent:
        with self._lock:
            if id in self._agents:
                raise KeyError(f"agent already exists: {id}")
            agent = Agent(
                id=id, name=name, cli=cli, runtime_id=runtime_id,
                system_prompt=system_prompt,
                skill_names=list(skill_names) if skill_names else [],
            )
            self._agents[agent.id] = agent
            self._persist(agent)
            return agent

    def get(self, agent_id: str) -> Agent | None:
        with self._lock:
            return self._agents.get(agent_id)

    def list(self) -> list[Agent]:
        with self._lock:
            return list(self._agents.values())

    def update(self, agent_id: str, **fields) -> Agent:
        """更新 name/cli/runtime_id/system_prompt/skill_names。"""
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(f"agent not found: {agent_id}")
            for k in ("name", "cli", "runtime_id", "system_prompt", "skill_names"):
                if k in fields and fields[k] is not None:
                    setattr(agent, k, list(fields[k]) if k == "skill_names" else fields[k])
            self._persist(agent)
            return agent

    def bind_skill(self, agent_id: str, skill_name: str) -> Agent:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(f"agent not found: {agent_id}")
            if skill_name and skill_name not in agent.skill_names:
                agent.skill_names.append(skill_name)
            self._persist(agent)
            return agent

    def unbind_skill(self, agent_id: str, skill_name: str) -> Agent:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(f"agent not found: {agent_id}")
            if skill_name in agent.skill_names:
                agent.skill_names.remove(skill_name)
            self._persist(agent)
            return agent


def merge_skills(bound: list[Skill], matched: "Skill | None") -> str:
    """合并 agent 绑定 skill + issue 自动匹配 skill,按 name 去重。

    每个 skill 拼 '## Skill: {name}\\n{body}',用空行分隔。返回拼好的字符串;
    全空则返回 ""(调用方据此传 None)。
    不同 agent 的绑定 skill 互相隔离 —— 各自的 skill_names 列表决定 bound,
    与其它 agent 无关。
    """
    seen: set[str] = set()
    parts: list[str] = []
    for skill in list(bound) + ([matched] if matched else []):
        if skill is None:
            continue
        key = skill.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"## Skill: {skill.name}\n{skill.body}")
    return "\n\n".join(parts)


@dataclass
class Member:
    id: str
    name: str


# ===========================================================================
# s13 — Project dataclass + ProjectStore(内存,与 IssueStore 同模式)
# 每个 project 绑定一个本地目录(local_path);issue 通过 project_id 关联。
# ===========================================================================
@dataclass
class Project:
    id: str
    workspace_id: str
    name: str
    local_path: str = ""    # 本地目录路径(空=继承 cwd)
    repo_url: str = ""


class ProjectStore:
    """内存 project 存储。data_dir 给定则 JSONL snapshot-append 后端。"""

    def __init__(self, data_dir: str | None = None) -> None:
        self._projects: dict[str, Project] = {}
        self._lock = threading.Lock()
        self._path: str | None = None
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self._path = os.path.join(data_dir, "projects.jsonl")
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
                project = Project(
                    id=rec["id"],
                    workspace_id=rec["workspace_id"],
                    name=rec.get("name", ""),
                    local_path=rec.get("local_path", ""),
                    repo_url=rec.get("repo_url", ""),
                )
                self._projects[project.id] = project   # last-wins

    def _persist(self, project: Project) -> None:
        if not self._path:
            return
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(project), ensure_ascii=False) + "\n")

    def create(self, workspace_id: str, name: str,
               local_path: str = "", repo_url: str = "") -> Project:
        with self._lock:
            project = Project(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                name=name,
                local_path=local_path,
                repo_url=repo_url,
            )
            self._projects[project.id] = project
            self._persist(project)
            return project

    def list(self, workspace_id: str | None = None) -> list[Project]:
        with self._lock:
            return [
                p for p in self._projects.values()
                if workspace_id is None or p.workspace_id == workspace_id
            ]

    def get(self, project_id: str) -> Project | None:
        with self._lock:
            return self._projects.get(project_id)


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
    project_id: str = ""          # s13:关联 project(空=无项目)
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
        project_id=d.get("project_id", ""),
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
               member_store: "MemberStore | None" = None,
               project_id: str = "") -> Issue:
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
            project_id=project_id,
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

    def set_project(self, issue_id: str, project_id: str) -> Issue:
        """s13:把 issue 关联到 project(project_id 可空,表示解除关联)。"""
        with self._lock:
            issue = self._require(issue_id)
            issue.project_id = project_id
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
    workdir: str = ""             # s13:执行该 task 的工作目录(空=继承 cwd)
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
        workdir=d.get("workdir", ""),
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
            append_system_prompt: str | None = None,
            skill_body: str | None = None,
            cwd: str | None = None) -> RunResult: ...

class StubRunner:
    """合成事件、无子进程、不调模型。append_system_prompt / skill_body / cwd 被忽略。"""
    def run(self, task, issue, context, on_event, resume_session_id=None,
            append_system_prompt=None, skill_body=None, cwd: str | None = None):
        on_event({"type": "progress", "task_id": task.id,
                  "message": f"stub: start '{issue.title}'"})
        on_event({"type": "progress", "task_id": task.id, "message": "stub: working..."})
        on_event({"type": "done", "task_id": task.id,
                  "output": f"stub completed: {issue.title}"})
        return {"status": "done", "output": f"stub completed: {issue.title}",
                "exit_code": 0, "session_id": ""}

class FailingStubRunner:
    """失败变体:返回 failed。append_system_prompt / skill_body / cwd 被忽略。"""
    def run(self, task, issue, context, on_event, resume_session_id=None,
            append_system_prompt=None, skill_body=None, cwd: str | None = None):
        on_event({"type": "fail", "task_id": task.id, "output": "stub failed: boom"})
        return {"status": "failed", "output": "stub failed: boom",
                "exit_code": 1, "session_id": ""}

class RecordingRunner:
    """记录 run() 收到的 kwargs(供测试断言 system_prompt / skill_body 注入)。
    默认返回 done;可选打印注入内容(demo)。"""
    def __init__(self, print_injection: bool = False) -> None:
        self.calls: list[dict] = []
        self.print_injection = print_injection

    def run(self, task, issue, context, on_event, resume_session_id=None,
            append_system_prompt=None, skill_body=None, cwd: str | None = None):
        call = {
            "task_id": task.id,
            "assignee_id": task.assignee_id,
            "append_system_prompt": append_system_prompt,
            "skill_body": skill_body,
            "cwd": cwd,
            "resume_session_id": resume_session_id,
        }
        self.calls.append(call)
        if self.print_injection:
            print(f"   [runner] agent={task.assignee_id}")
            print(f"   [runner] system_prompt(append-system-prompt)={append_system_prompt!r}")
            print(f"   [runner] skill_body(user prompt)=\n{skill_body}")
            print(f"   [runner] cwd={cwd!r}")
        on_event({"type": "progress", "task_id": task.id, "message": "recorded"})
        on_event({"type": "done", "task_id": task.id, "output": "recorded"})
        return {"status": "done", "output": "recorded",
                "exit_code": 0, "session_id": ""}


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
                 skill_loader: "SkillLoader | None" = None,
                 project_store: "ProjectStore | None" = None,
                 agent_store: "AgentStore | None" = None) -> None:
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
        self.project_store = project_store
        # s15:agent_store 让 daemon 按 task.assignee_id 查 agent 配置
        # (system_prompt + 绑定 skill)。None 时兼容旧逻辑(只 issue 匹配 skill)。
        self.agent_store = agent_store
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
        # s13:从 issue.project_id 解析 workdir(project.local_path)。
        workdir = ""
        if issue is not None and issue.project_id and self.project_store is not None:
            project = self.project_store.get(issue.project_id)
            if project:
                workdir = project.local_path
        task.workdir = workdir
        self.task_store._persist(task)
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
        # s15:按 task.assignee_id 查 agent 配置(system_prompt + 绑定 skill)。
        agent = self.agent_store.get(task.assignee_id) if self.agent_store else None
        system_prompt = agent.system_prompt if agent else ""
        bound: list[Skill] = []
        if agent and self.skill_loader:
            bound = [self.skill_loader.get(n) for n in agent.skill_names]
            bound = [s for s in bound if s is not None]
        # issue 自动匹配 skill(临时补充,与 agent 绑定 skill 合并去重)。
        matched = self.skill_loader.select(issue) if self.skill_loader else None
        skill_body = merge_skills(bound, matched)
        result = self.runner.run(task, issue, [], on_event,
                                 resume_session_id=prior_sid or None,
                                 append_system_prompt=system_prompt or None,
                                 skill_body=skill_body or None,
                                 cwd=workdir or None)
        # 覆盖 task.session_id(命中=同 id no-op;失效=新 id 覆盖)
        if result.get("session_id"):
            self.task_store.set_session_id(task.id, result["session_id"])
        if result["status"] == "done":
            self.task_store.complete(task.id, result.get("session_id", ""))
            self._sync_issue_status(task, completed=True)
            # agent 执行结果写回 issue 评论(人能看到 agent 干了什么)
            if issue is not None:
                self.issue_store.comment(issue.id, f"[agent] {result.get('output', '')}")
        else:
            self.task_store.fail(task.id, result.get("output", ""), result.get("session_id", ""))
            self._sync_issue_status(task, failed=True)
            if issue is not None:
                self.issue_store.comment(issue.id, f"[agent failed] {result.get('output', '')}")
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
            append_system_prompt: str | None = None,
            skill_body: str | None = None,
            cwd: str | None = None) -> RunResult:
        prompt = issue.title + (f"\n\n{issue.body}" if issue.body else "")
        # s15:技能正文拼入 user prompt(不进 --append-system-prompt,避免跨轮残留)。
        if skill_body:
            prompt += f"\n\n[Skill]\n{skill_body}"
        if resume_session_id and task.trigger_comment_id:
            prompt += f"\n\n[Follow-up] {task.trigger_comment_id}"
        try:
            proc = subprocess.Popen(
                self._cmd(prompt, append_system_prompt=append_system_prompt,
                          resume_session_id=resume_session_id),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=cwd,
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
keywords: review, 审查, PR, code review
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


def main_squad_demo(data_dir=None) -> int:
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


def main(data_dir="./workspace") -> int:
    """s15 demo:Agent Profile —— 可配置执行主体。

    演示两层 skill + agent 隔离:
    1. 装配 stores + SkillLoader(扫示例 skills)+ AgentStore;
    2. 建 2 个 agent:frontend-agent(绑 react skill)+ backend-agent(绑 pytest skill),
       各带 system_prompt;
    3. 建 issue A("review the auth module" -> 命中 code-review skill)派给 frontend-agent;
       建 issue B("refactor utils")派给 backend-agent;
    4. daemon 用 RecordingRunner 执行,打印每个 task 注入的 system_prompt + skill_body;
    5. 展示:frontend-agent 拿到 react(绑定)+ code-review(issue 匹配);
       backend-agent 拿到 pytest(绑定),不含 react —— agent 隔离。
    """
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    # 干净起跑:清掉 issue/task/agent/project 持久化(skills 保留)
    for fname in ("issues.jsonl", "tasks.jsonl", "agents.jsonl", "projects.jsonl"):
        p = os.path.join(data_dir, fname)
        if os.path.exists(p):
            os.remove(p)
    is_ = IssueStore(data_dir=data_dir)
    ts = TaskStore(data_dir=data_dir)
    ps = ProjectStore(data_dir=data_dir)
    ag = AgentStore(data_dir=data_dir)

    # 示例 skills(两个:react + code-review),code-review 也作为 issue 匹配来源。
    # 强制覆写示例 skill,保证 demo 使用当前 keywords frontmatter。
    skills_dir = os.path.join(data_dir, "skills")
    os.makedirs(os.path.join(skills_dir, "react"), exist_ok=True)
    os.makedirs(os.path.join(skills_dir, "code-review"), exist_ok=True)
    react_md = os.path.join(skills_dir, "react", "SKILL.md")
    review_md = os.path.join(skills_dir, "code-review", "SKILL.md")
    with open(react_md, "w", encoding="utf-8") as f:
        f.write("---\nname: react\ndescription: React 组件最佳实践\nkeywords: react, component, jsx\n---\nUse functional components, hooks, and proper key props.")
    with open(review_md, "w", encoding="utf-8") as f:
        f.write(EXAMPLE_SKILL_MD)
    sl = SkillLoader(skills_dir)

    runner = RecordingRunner(print_injection=True)
    daemon = Daemon(is_, ts, runner, project_store=ps, skill_loader=sl,
                    agent_store=ag)

    print("=== s15 demo: Agent Profile ===")
    fe = ag.create(
        "agent-fe", "Frontend Agent", cli="claude", runtime_id="rt-1",
        system_prompt="You are a frontend implementation agent. Focus on accessibility and UI consistency.",
        skill_names=["react"],
    )
    be = ag.create(
        "agent-be", "Backend Agent", cli="claude", runtime_id="rt-1",
        system_prompt="You are a backend agent. Focus on correctness, tests, and data integrity.",
        skill_names=[],  # 稍后演示 bind_skill
    )
    be = ag.bind_skill(be.id, "code-review")  # backend 绑 code-review(演示运行时绑定)
    print(f"1) agents: {fe.id}(skills={fe.skill_names}) {be.id}(skills={be.skill_names})")

    # issue A 命中 code-review skill(关键词 review)派给 frontend-agent
    issue_a = is_.create("ws-1", "review the auth module",
                         body="please review", assignee_type="agent",
                         assignee_id=fe.id)
    task_a = ts.enqueue(issue_a.id, "agent", fe.id)
    # issue B 无 skill 关键词,派给 backend-agent(只拿绑定 skill)
    issue_b = is_.create("ws-1", "refactor utils",
                         assignee_type="agent", assignee_id=be.id)
    task_b = ts.enqueue(issue_b.id, "agent", be.id)
    print(f"2) issues: A={issue_a.id}({issue_a.title}) B={issue_b.id}({issue_b.title})")

    print("3) daemon runs task A (frontend-agent):")
    daemon.run_once()
    print("4) daemon runs task B (backend-agent):")
    daemon.run_once()

    print("\n=== 结果分析 ===")
    call_a = next((c for c in runner.calls if c["task_id"] == task_a.id), None)
    call_b = next((c for c in runner.calls if c["task_id"] == task_b.id), None)
    print(f"task A agent={call_a['assignee_id']}")
    print(f"  system_prompt 含 frontend 身份: {'frontend' in (call_a['append_system_prompt'] or '')}")
    print(f"  skill_body 含 react(绑定): {'## Skill: react' in (call_a['skill_body'] or '')}")
    print(f"  skill_body 含 code-review(issue 匹配): {'## Skill: code-review' in (call_a['skill_body'] or '')}")
    print(f"task B agent={call_b['assignee_id']}")
    print(f"  system_prompt 含 backend 身份: {'backend' in (call_b['append_system_prompt'] or '')}")
    print(f"  skill_body 含 code-review(绑定): {'## Skill: code-review' in (call_b['skill_body'] or '')}")
    print(f"  skill_body 不含 react(agent 隔离): {'## Skill: react' not in (call_b['skill_body'] or '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
