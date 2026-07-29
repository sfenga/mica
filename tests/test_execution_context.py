import json
from pathlib import Path

from sessions.zh.s14_agents import (
    AgentStore,
    IssueStore,
    ProjectStore,
    SkillLoader,
    TaskStatus,
    TaskStore,
)
from sessions.zh.s16_execution_context import (
    ExecutionContextBuilder,
    render_agent_identity,
    safe_name,
)


def _write_skill(root: Path, name: str, body: str = "body") -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} desc\n---\n{body}\n",
        encoding="utf-8",
    )


def _fixture(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    _write_skill(tmp_path, "code-review", "review body")
    _write_skill(tmp_path, "react", "react body")

    issue_store = IssueStore()
    task_store = TaskStore()
    project_store = ProjectStore()
    agent_store = AgentStore()
    skill_loader = SkillLoader(str(tmp_path / "skills"))

    project = project_store.create("ws-1", "Repo", local_path=str(workdir))
    agent_store.create(
        "agent-review",
        "Review Agent",
        system_prompt="Be precise.",
        skill_names=["code-review", "missing", "code-review"],
    )
    issue = issue_store.create(
        "ws-1",
        "review auth",
        body="Check auth redirects.",
        assignee_type="agent",
        assignee_id="agent-review",
        project_id=project.id,
    )
    issue_store.comment(issue.id, "Include session edge cases.")
    task = task_store.enqueue(issue.id, "agent", "agent-review")
    builder = ExecutionContextBuilder(
        issue_store,
        task_store,
        agent_store=agent_store,
        skill_loader=skill_loader,
        project_store=project_store,
    )
    return builder, workdir, issue, task


def test_build_writes_execution_context_files(tmp_path):
    builder, workdir, issue, task = _fixture(tmp_path)

    ctx = builder.build(task)
    context_dir = Path(ctx.context_dir)

    assert Path(ctx.workdir) == workdir.resolve()
    assert context_dir == workdir.resolve() / ".mica" / "tasks" / task.id
    assert (context_dir / "agent-identity.md").exists()
    assert (context_dir / "task-context.md").exists()
    assert (context_dir / "skills" / "code-review" / "SKILL.md").exists()
    assert (context_dir / "manifest.json").exists()
    assert task.status == TaskStatus.QUEUED

    task_context = (context_dir / "task-context.md").read_text(encoding="utf-8")
    assert "review auth" in task_context
    assert "Check auth redirects." in task_context
    assert "Include session edge cases." in task_context
    assert issue.id in task_context


def test_manifest_lists_written_files(tmp_path):
    builder, _workdir, _issue, task = _fixture(tmp_path)

    ctx = builder.build(task)
    manifest = json.loads(Path(ctx.manifest_path).read_text(encoding="utf-8"))

    assert manifest["task_id"] == task.id
    assert manifest["agent_id"] == "agent-review"
    assert manifest["files"] == [
        "agent-identity.md",
        "task-context.md",
        "skills/code-review/SKILL.md",
        "manifest.json",
    ]
    assert ctx.files == manifest["files"]


def test_rebuild_clears_stale_files(tmp_path):
    builder, _workdir, _issue, task = _fixture(tmp_path)

    ctx = builder.build(task)
    stale = Path(ctx.context_dir) / "stale.txt"
    stale.write_text("old", encoding="utf-8")

    builder.build(task)

    assert not stale.exists()


def test_unconfigured_agent_still_writes_minimal_identity(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    issue_store = IssueStore()
    task_store = TaskStore()
    project_store = ProjectStore()
    project = project_store.create("ws-1", "Repo", local_path=str(workdir))
    issue = issue_store.create(
        "ws-1",
        "plain task",
        assignee_type="agent",
        assignee_id="agent-derived",
        project_id=project.id,
    )
    task = task_store.enqueue(issue.id, "agent", "agent-derived")
    builder = ExecutionContextBuilder(
        issue_store,
        task_store,
        agent_store=AgentStore(),
        project_store=project_store,
    )

    ctx = builder.build(task)

    identity = (Path(ctx.context_dir) / "agent-identity.md").read_text(encoding="utf-8")
    assert "Agent ID: agent-derived" in identity
    assert "Profile: not configured" in identity
    assert "skills" not in ctx.files


def test_safe_name_and_render_agent_identity():
    assert safe_name("code review!") == "code-review-"

    identity = render_agent_identity(None, type("T", (), {"assignee_id": "agent-x"})())
    assert "Profile: not configured" in identity
