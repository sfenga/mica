"""s16 — Execution Context(把本次执行上下文物化到 workdir)

s15 让 daemon 变成后台自动消费队列的 worker。s16 继续补 multiCa 的另一个
核心动作:在启动 runtime/CLI 前,daemon 先把 agent、issue、task、project、skill
整理成 runtime 可见的文件。

教学版输出:

project workdir/
  .mica/
    tasks/<task_id>/
      agent-identity.md
      task-context.md
      skills/<skill-name>/SKILL.md
      manifest.json

这不是替代 runner.run(),而是执行前的 prepare 阶段。
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sessions.zh.s14_agents import (
    Agent,
    AgentStore,
    EXAMPLE_SKILL_MD,
    Issue,
    IssueStore,
    Project,
    ProjectStore,
    Skill,
    SkillLoader,
    Task,
    TaskStore,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExecutionContext:
    task_id: str
    issue_id: str
    agent_id: str
    workdir: str
    context_dir: str
    files: list[str] = field(default_factory=list)
    manifest_path: str = ""


class ExecutionContextBuilder:
    """为一次 task 执行准备 runtime 可见文件。

    Builder 只做 prepare:
    - 解析 issue -> project -> workdir
    - 解析 task.assignee_id -> Agent
    - 解析 Agent.skill_names -> Skill
    - 写 agent/task/skills/manifest 文件

    它不 claim task,不调用 runner,也不改变 task 状态。
    """

    def __init__(self, issue_store: IssueStore, task_store: TaskStore,
                 agent_store: AgentStore | None = None,
                 skill_loader: SkillLoader | None = None,
                 project_store: ProjectStore | None = None,
                 context_root_name: str = ".mica") -> None:
        self.issue_store = issue_store
        self.task_store = task_store
        self.agent_store = agent_store
        self.skill_loader = skill_loader
        self.project_store = project_store
        self.context_root_name = context_root_name

    def build(self, task: Task) -> ExecutionContext:
        issue = self.issue_store.get(task.issue_id)
        if issue is None:
            raise KeyError(f"issue not found: {task.issue_id}")

        agent = self.agent_store.get(task.assignee_id) if self.agent_store else None
        project = self._resolve_project(issue)
        workdir = self._resolve_workdir(project)
        context_dir = workdir / self.context_root_name / "tasks" / task.id

        if context_dir.exists():
            shutil.rmtree(context_dir)
        (context_dir / "skills").mkdir(parents=True, exist_ok=True)

        files: list[str] = []
        files.append(self._write_text(
            context_dir,
            "agent-identity.md",
            render_agent_identity(agent, task),
        ))
        files.append(self._write_text(
            context_dir,
            "task-context.md",
            render_task_context(issue, task, project),
        ))

        bound_skills = self._load_agent_skills(agent)
        for skill in bound_skills:
            skill_rel = Path("skills") / safe_name(skill.name) / "SKILL.md"
            files.append(self._write_text(
                context_dir,
                str(skill_rel),
                render_skill_file(skill),
            ))

        manifest_rel = "manifest.json"
        manifest_files = files + [manifest_rel]
        manifest = {
            "created_at": now_iso(),
            "task_id": task.id,
            "issue_id": issue.id,
            "workspace_id": issue.workspace_id,
            "agent_id": task.assignee_id,
            "project_id": issue.project_id,
            "workdir": str(workdir),
            "files": manifest_files,
        }
        self._write_text(
            context_dir,
            manifest_rel,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        files = manifest_files

        return ExecutionContext(
            task_id=task.id,
            issue_id=issue.id,
            agent_id=task.assignee_id,
            workdir=str(workdir),
            context_dir=str(context_dir),
            files=files,
            manifest_path=str(context_dir / "manifest.json"),
        )

    def _resolve_project(self, issue: Issue) -> Project | None:
        if not issue.project_id or self.project_store is None:
            return None
        return self.project_store.get(issue.project_id)

    def _resolve_workdir(self, project: Project | None) -> Path:
        if project and project.local_path:
            workdir = Path(project.local_path)
        else:
            workdir = Path.cwd()
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir.resolve()

    def _load_agent_skills(self, agent: Agent | None) -> list[Skill]:
        if agent is None or self.skill_loader is None:
            return []
        skills: list[Skill] = []
        seen: set[str] = set()
        for name in agent.skill_names:
            skill = self.skill_loader.get(name)
            if skill is None:
                continue
            key = skill.name.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            skills.append(skill)
        return skills

    def _write_text(self, root: Path, rel_path: str, content: str) -> str:
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return rel_path


def safe_name(name: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in ("-", "_", ".") else "-"
        for ch in name.strip()
    )
    return cleaned or "skill"


def render_agent_identity(agent: Agent | None, task: Task) -> str:
    if agent is None:
        return (
            "# Agent Identity\n\n"
            f"- Agent ID: {task.assignee_id}\n"
            "- Profile: not configured\n"
        )

    instructions = agent.system_prompt.strip() or "(none)"
    skills = ", ".join(agent.skill_names) if agent.skill_names else "(none)"
    return (
        "# Agent Identity\n\n"
        f"- Agent ID: {agent.id}\n"
        f"- Name: {agent.name}\n"
        f"- CLI: {agent.cli}\n"
        f"- Runtime: {agent.runtime_id}\n"
        f"- Bound Skills: {skills}\n\n"
        "## Instructions\n\n"
        f"{instructions}\n"
    )


def render_task_context(issue: Issue, task: Task, project: Project | None) -> str:
    comments = "\n".join(f"- {c}" for c in issue.comments) or "- (none)"
    project_block = (
        f"- Project ID: {project.id}\n"
        f"- Project Name: {project.name}\n"
        f"- Local Path: {project.local_path or '(inherit cwd)'}\n"
        f"- Repo URL: {project.repo_url or '(none)'}\n"
        if project else
        "- Project: (none)\n"
    )
    return (
        "# Task Context\n\n"
        "## Issue\n\n"
        f"- Issue ID: {issue.id}\n"
        f"- Workspace ID: {issue.workspace_id}\n"
        f"- Title: {issue.title}\n"
        f"- Status: {issue.status.value}\n"
        f"- Assignee: {issue.assignee_type}:{issue.assignee_id}\n\n"
        "### Body\n\n"
        f"{issue.body or '(empty)'}\n\n"
        "### Comments\n\n"
        f"{comments}\n\n"
        "## Task\n\n"
        f"- Task ID: {task.id}\n"
        f"- Status: {task.status.value}\n"
        f"- Trigger Comment: {task.trigger_comment_id or '(none)'}\n"
        f"- Rerun Of: {task.rerun_of_task_id or '(none)'}\n"
        f"- Handoff Note: {task.handoff_note or '(none)'}\n\n"
        "## Project\n\n"
        f"{project_block}"
    )


def render_skill_file(skill: Skill) -> str:
    return (
        "---\n"
        f"name: {skill.name}\n"
        f"description: {skill.description}\n"
        "---\n\n"
        f"{skill.body.strip()}\n"
    )


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workdir = root / "demo-project"
        skills_dir = root / "skills" / "code-review"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(EXAMPLE_SKILL_MD, encoding="utf-8")

        issue_store = IssueStore()
        task_store = TaskStore()
        project_store = ProjectStore()
        agent_store = AgentStore()
        skill_loader = SkillLoader(str(root / "skills"))

        project = project_store.create("ws-1", "Demo Project", local_path=str(workdir))
        agent_store.create(
            "agent-review",
            "Review Agent",
            system_prompt="Review carefully before proposing changes.",
            skill_names=["code-review"],
        )
        issue = issue_store.create(
            "ws-1",
            "review auth changes",
            body="Check the login redirect and session handling.",
            assignee_type="agent",
            assignee_id="agent-review",
            project_id=project.id,
        )
        issue_store.comment(issue.id, "Focus on auth bypass edge cases.")
        task = task_store.enqueue(issue.id, "agent", "agent-review")

        builder = ExecutionContextBuilder(
            issue_store,
            task_store,
            agent_store=agent_store,
            skill_loader=skill_loader,
            project_store=project_store,
        )
        ctx = builder.build(task)
        print(f"context_dir: {ctx.context_dir}")
        for rel in ctx.files:
            print(f"- {rel}")
        print((Path(ctx.context_dir) / "task-context.md").read_text(encoding="utf-8").splitlines()[0])
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
