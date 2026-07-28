"""s99 Mission Control API tests — TestClient against create_console_app.

Each test builds a fresh in-memory app via _client(tmp_path), so stores never
leak between tests. Pattern mirrors tests/test_web_panel.py.
"""
import datetime
import os
from datetime import timedelta, timezone

from fastapi.testclient import TestClient

from sessions.zh.s99_mission_control import (
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
    browse_dirs,
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


def test_skill_get_by_name(tmp_path):
    client, *_ = _client(tmp_path)
    res = client.get("/api/skills/code-review")
    data = res.json()
    assert data["skill"]["name"] == "code-review"
    assert "You are reviewing code" in data["skill"]["body"]


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


def test_list_issues_filtered_by_workspace(tmp_path):
    """s11:GET /api/issues?workspace_id=... 只返回该 workspace 的 issue。"""
    client, is_, *_ = _client(tmp_path)
    is_.create("ws-1", "ws1 issue", assignee_type="agent", assignee_id="agent-1")
    is_.create("ws-2", "ws2 issue", assignee_type="agent", assignee_id="agent-1")
    ws1 = client.get("/api/issues", params={"workspace_id": "ws-1"}).json()
    ws2 = client.get("/api/issues", params={"workspace_id": "ws-2"}).json()
    assert [i["title"] for i in ws1] == ["ws1 issue"]
    assert [i["title"] for i in ws2] == ["ws2 issue"]


def test_list_issues_member_gate(tmp_path):
    """s11:alice 不是 ws-2 成员 -> list 返回 error 而非数据。"""
    client, is_, *_ = _client(tmp_path)
    is_.create("ws-2", "ws2 issue", assignee_type="agent", assignee_id="agent-1")
    # alice 是 ws-1 成员,不是 ws-2 成员
    res = client.get("/api/issues", params={"workspace_id": "ws-2",
                                            "member_id": "alice"}).json()
    assert "error" in res
    assert "not in workspace ws-2" in res["error"]


def test_create_issue_member_gate(tmp_path):
    """s11:非成员在 ws-2 create issue 被拒,返回 error。"""
    client, *_ = _client(tmp_path)
    res = client.post(
        "/api/issues",
        params={"title": "forbidden", "assignee_type": "agent",
                "assignee_id": "agent-1", "workspace_id": "ws-2",
                "member_id": "alice"},
    ).json()
    assert "error" in res
    assert "not in workspace ws-2" in res["error"]


def test_list_tasks_filtered_by_workspace(tmp_path):
    """tasks 视图按 workspace 过滤(task 自身无 workspace_id,经 issue 关联)。"""
    client, is_, ts, *_ = _client(tmp_path)
    i1 = is_.create("ws-1", "ws1", assignee_type="agent", assignee_id="agent-1")
    i2 = is_.create("ws-2", "ws2", assignee_type="agent", assignee_id="agent-1")
    ts.enqueue(i1.id, "agent", "agent-1")
    ts.enqueue(i2.id, "agent", "agent-1")
    ws1 = client.get("/api/tasks", params={"workspace_id": "ws-1"}).json()
    ws2 = client.get("/api/tasks", params={"workspace_id": "ws-2"}).json()
    assert len(ws1) == 1 and ws1[0]["issue_id"] == i1.id
    assert len(ws2) == 1 and ws2[0]["issue_id"] == i2.id


def test_get_task_detail(tmp_path):
    """GET /api/tasks/{id} 返回 task + 所属 issue 标题 + workdir + events。"""
    client, is_, ts, *_ = _client(tmp_path)
    issue = is_.create("ws-1", "login bug", assignee_type="agent",
                       assignee_id="agent-1")
    task = ts.enqueue(issue.id, "agent", "agent-1")
    res = client.get(f"/api/tasks/{task.id}").json()
    assert res["id"] == task.id
    assert res["issue_id"] == issue.id
    assert res["issue_title"] == "login bug"
    assert res["issue_status"] == "todo"
    assert "events" in res
    assert isinstance(res["events"], list)


def test_get_task_with_workdir(tmp_path):
    """task 详情解析 issue 关联 project 的 workdir。"""
    client, is_, ts, *_ = _client(tmp_path)
    proj = client.post("/api/projects", params={"name": "repo",
                       "local_path": "/tmp/demo-repo"}).json()
    issue = is_.create("ws-1", "x", assignee_type="agent",
                      assignee_id="agent-1", project_id=proj["id"])
    task = ts.enqueue(issue.id, "agent", "agent-1")
    res = client.get(f"/api/tasks/{task.id}").json()
    assert res["workdir"] == "/tmp/demo-repo"


def test_get_task_not_found(tmp_path):
    """不存在 task id 返回 error 而非 404 崩溃。"""
    client, *_ = _client(tmp_path)
    res = client.get("/api/tasks/does-not-exist").json()
    assert "error" in res
    assert "not found" in res["error"]


def test_trace_events_after_daemon(tmp_path):
    """Trace 视图数据源:daemon 跑完后 GET /api/tasks/{id} 含事件流。"""
    client, is_, ts, *_ = _client(tmp_path)
    issue = is_.create("ws-1", "do thing", assignee_type="agent",
                      assignee_id="agent-1")
    ts.enqueue(issue.id, "agent", "agent-1")
    client.post("/api/daemon/run-once")
    task = ts.list(issue.id)[0]
    res = client.get(f"/api/tasks/{task.id}").json()
    assert res["status"] == "completed"
    assert len(res["events"]) > 0
    kinds = [e.get("type") or e.get("kind") for e in res["events"]]
    # daemon 记 claim/result;StubRunner 记 progress/done
    assert "claim" in kinds
    assert "done" in kinds



def test_member_added_then_can_access_ws2(tmp_path):
    """端到端:add member 后 gate 放行。"""
    client, *_ = _client(tmp_path)
    # alice 不能 list ws-2
    before = client.get("/api/issues", params={"workspace_id": "ws-2",
                                               "member_id": "alice"}).json()
    assert "error" in before
    # 加入 ws-2
    client.post("/api/workspaces/ws-2/members", params={"member_id": "alice"})
    # 现在能 list(空列表,不是 error)
    after = client.get("/api/issues", params={"workspace_id": "ws-2",
                                              "member_id": "alice"}).json()
    assert isinstance(after, list)
    assert after == []


def test_browse_dirs_lists_subdirs(tmp_path):
    """browse_dirs 只列目录,不含文件。"""
    (tmp_path / "repo-a").mkdir()
    (tmp_path / "repo-b").mkdir()
    (tmp_path / "file.txt").write_text("x")
    res = browse_dirs(str(tmp_path))
    assert res["current"] == str(tmp_path)
    names = [d["name"] for d in res["dirs"]]
    assert names == ["repo-a", "repo-b"]   # 排序 + 无文件


def test_browse_dirs_parent_and_root(tmp_path):
    """parent 指向上一级;根 / 时 parent 为 None。"""
    sub = tmp_path / "x"
    sub.mkdir()
    res = browse_dirs(str(sub))
    assert res["parent"] == str(tmp_path)
    # 根:parent 与 current 相同则归 None
    root_res = browse_dirs("/")
    assert root_res["parent"] is None
    assert root_res["current"] == "/"


def test_browse_dirs_missing_path_empty(tmp_path):
    """不存在的路径 -> 空 dirs,不抛错。"""
    res = browse_dirs(str(tmp_path / "nope"))
    assert res["dirs"] == []


def test_fs_browse_route(tmp_path):
    """/api/fs/browse 路由返回目录结构。"""
    client, *_ = _client(tmp_path)
    # 在 tmp_path 下造两个子目录,用 client 不易访问 tmp_path;改用 home 默认
    res = client.get("/api/fs/browse").json()
    assert "current" in res and "dirs" in res and "parent" in res
    res2 = client.get("/api/fs/browse", params={"path": "/"}).json()
    assert res2["current"] == "/"
    assert res2["parent"] is None
    assert isinstance(res2["dirs"], list)


