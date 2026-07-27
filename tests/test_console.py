"""s13 console API tests — TestClient against create_console_app.

Each test builds a fresh in-memory app via _client(tmp_path), so stores never
leak between tests. Pattern mirrors tests/test_web_panel.py.
"""
import datetime
import os
from datetime import timedelta, timezone

from fastapi.testclient import TestClient

from sessions.zh.s14_console import (
    AutopilotStore,
    Daemon,
    EXAMPLE_SKILL_MD,
    IssueStore,
    IssueStatus,
    MemberStore,
    ProjectStore,
    RealtimeHub,
    Runtime,
    SessionStore,
    SkillLoader,
    SquadRouter,
    StubRunner,
    TaskStore,
    create_console_app,
)


def _client(tmp_path):
    is_ = IssueStore()
    ts = TaskStore()
    ss = SessionStore(str(tmp_path))
    hub = RealtimeHub()
    member_store = MemberStore()
    ap_store = AutopilotStore()
    project_store = ProjectStore()
    squad_router = SquadRouter(is_, ts)
    daemon = Daemon(
        is_, ts, StubRunner(), runtime_id="rt-1",
        session_store=ss, hub=hub,
        project_store=project_store,
    )
    runtime = Runtime(id="rt-1", name="local", clis=["claude", "codex"])
    skills_dir = os.path.join(str(tmp_path), "skills", "code-review")
    os.makedirs(skills_dir, exist_ok=True)
    with open(os.path.join(skills_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(EXAMPLE_SKILL_MD)
    skill_loader = SkillLoader(os.path.join(str(tmp_path), "skills"))
    app = create_console_app(
        is_, ts, ss, hub,
        skill_loader=skill_loader,
        autopilot_store=ap_store,
        member_store=member_store,
        squad_router=squad_router,
        daemon=daemon,
        runtime=runtime,
        project_store=project_store,
    )
    return TestClient(app), is_, ts, ss, ap_store, squad_router, project_store


def test_overview(tmp_path):
    client, is_, ts, *_ = _client(tmp_path)
    is_.create("ws-1", "issue 1", assignee_type="agent", assignee_id="agent-1")
    res = client.get("/api/overview")
    data = res.json()
    assert data["issues"] == 1
    assert data["agents"] == 2
    assert data["runtimes"] == 1


def test_create_agent_issue(tmp_path):
    client, is_, ts, *_ = _client(tmp_path)
    res = client.post(
        "/api/issues",
        params={"title": "fix bug", "assignee_type": "agent", "assignee_id": "agent-1"},
    )
    data = res.json()
    assert data["title"] == "fix bug"
    assert data["assignee_id"] == "agent-1"
    tasks = ts.list(data["id"])
    assert len(tasks) == 1
    assert tasks[0].assignee_id == "agent-1"


def test_create_squad_issue(tmp_path):
    client, is_, ts, ss, ap, sr, _ps = _client(tmp_path)
    res = client.post(
        "/api/squads",
        params={"name": "Team", "leader_agent_id": "agent-1", "member_agent_ids": "agent-2"},
    )
    squad = res.json()
    res = client.post(
        "/api/issues",
        params={"title": "ship feature", "assignee_type": "squad", "assignee_id": squad["id"]},
    )
    issue = res.json()
    assert issue["assignee_type"] == "squad"
    tasks = ts.list(issue["id"])
    assert len(tasks) == 1
    assert tasks[0].is_leader_task is True
    assert tasks[0].assignee_id == "agent-1"


def test_comment_creates_resume_task(tmp_path):
    client, is_, ts, *_ = _client(tmp_path)
    res = client.post(
        "/api/issues",
        params={"title": "t", "assignee_type": "agent", "assignee_id": "agent-1"},
    )
    issue_id = res.json()["id"]
    res = client.post(
        f"/api/issues/{issue_id}/comments", params={"text": "fix typo"}
    )
    data = res.json()
    assert data["task_id"]
    tasks = ts.list(issue_id)
    assert len(tasks) == 2
    assert tasks[1].trigger_comment_id == "fix typo"


def test_daemon_run_once(tmp_path):
    client, is_, ts, *_ = _client(tmp_path)
    res = client.post(
        "/api/issues",
        params={"title": "do work", "assignee_type": "agent", "assignee_id": "agent-1"},
    )
    issue_id = res.json()["id"]
    client.post("/api/daemon/run-once")
    issue = is_.get(issue_id)
    assert issue.status == IssueStatus.IN_REVIEW
    tasks = ts.list(issue_id)
    assert any(t.status.value == "completed" for t in tasks)


def test_autopilot_tick(tmp_path):
    client, is_, ts, ss, ap, sr, _ps = _client(tmp_path)
    past = (datetime.datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    res = client.post(
        "/api/autopilots",
        params={"cron": "* * * * *", "agent_id": "agent-1",
                "title": "daily check", "next_run": past},
    )
    assert res.json()["id"]
    client.post("/api/autopilots/tick")
    issues = is_.list()
    assert any(i.title == "daily check" for i in issues)


def test_skills_preview(tmp_path):
    client, *_ = _client(tmp_path)
    res = client.post(
        "/api/skills/preview",
        params={"title": "review the auth module", "body": ""},
    )
    data = res.json()
    assert data["matched"]["name"] == "code-review"
    assert "You are reviewing code" in data["injected_prompt"]


def test_squad_worker_report(tmp_path):
    client, is_, ts, ss, ap, sr, _ps = _client(tmp_path)
    res = client.post(
        "/api/squads",
        params={"name": "Team", "leader_agent_id": "agent-1", "member_agent_ids": "agent-2"},
    )
    squad_id = res.json()["id"]
    res = client.post(
        "/api/issues",
        params={"title": "ship", "assignee_type": "squad", "assignee_id": squad_id},
    )
    issue_id = res.json()["id"]
    client.post("/api/daemon/run-once")
    res = client.post(
        f"/api/squads/{squad_id}/worker-report",
        params={"issue_id": issue_id, "worker_agent_id": "agent-2", "text": "done"},
    )
    assert res.json()["task_id"]


def test_demo_seed(tmp_path):
    client, is_, ts, ss, ap, sr, _ps = _client(tmp_path)
    res = client.post("/api/demo/seed")
    data = res.json()
    assert data["issues"] >= 2
    assert len(is_.list()) >= 2
    assert len(sr.list_squads()) >= 1


def test_demo_reset(tmp_path):
    client, is_, ts, ss, ap, sr, _ps = _client(tmp_path)
    client.post("/api/demo/seed")
    assert len(is_.list()) > 0
    client.post("/api/demo/reset")
    assert len(is_.list()) == 0
    assert len(ts.list()) == 0
    assert len(sr.list_squads()) == 0


def test_get_console_html(tmp_path):
    client, *_ = _client(tmp_path)
    res = client.get("/")
    assert res.status_code == 200
    assert "mica" in res.text.lower()
    assert "mission control" in res.text.lower() or "console" in res.text.lower()


def test_project_create_and_list(tmp_path):
    client, *_ = _client(tmp_path)
    res = client.post(
        "/api/projects",
        params={"name": "mica-app", "local_path": "/tmp/mica-app",
                "repo_url": "https://git.example/mica.git"},
    )
    proj = res.json()
    assert proj["name"] == "mica-app"
    assert proj["local_path"] == "/tmp/mica-app"
    assert proj["repo_url"] == "https://git.example/mica.git"
    listed = client.get("/api/projects").json()
    assert any(p["id"] == proj["id"] for p in listed)


def test_issue_with_project_resolves_workdir(tmp_path):
    client, is_, ts, *_ = _client(tmp_path)
    proj = client.post(
        "/api/projects",
        params={"name": "demo", "local_path": "/tmp/demo"},
    ).json()
    res = client.post(
        "/api/issues",
        params={"title": "ship feature", "assignee_type": "agent",
                "assignee_id": "agent-1", "project_id": proj["id"]},
    )
    issue = res.json()
    assert issue["project_id"] == proj["id"]
    detail = client.get(f"/api/issues/{issue['id']}").json()
    assert detail["workdir"] == "/tmp/demo"
    # daemon 运行后 task.workdir 应被解析为 project.local_path
    client.post("/api/daemon/run-once")
    tasks = ts.list(issue["id"])
    assert any(t.workdir == "/tmp/demo" for t in tasks)


def test_issue_without_project_has_empty_workdir(tmp_path):
    client, *_ = _client(tmp_path)
    res = client.post(
        "/api/issues",
        params={"title": "no project", "assignee_type": "agent",
                "assignee_id": "agent-1"},
    )
    issue = res.json()
    assert issue["project_id"] == ""
    detail = client.get(f"/api/issues/{issue['id']}").json()
    assert detail["workdir"] == ""
