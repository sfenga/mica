"""s99 Mission Control agent profile tests — API + daemon injection + derived fallback.

覆盖:
- POST/GET /api/agents 创建 + 列表
- PATCH /api/agents/{id} 更新 system_prompt
- POST/DELETE /api/agents/{id}/skills 绑定/解绑
- AgentStore 空 -> GET /api/agents 回退 runtime.clis 派生
- daemon 注入 agent.system_prompt(RecordingRunner 断言 append_system_prompt)
- issue 创建表单 assignee 下拉数据源(GET /api/agents)
"""
import os

from fastapi.testclient import TestClient

from sessions.zh.s99_mission_control import (
    Agent,
    AgentStore,
    Daemon,
    EXAMPLE_SKILL_MD,
    IssueStore,
    MemberStore,
    ProjectStore,
    RealtimeHub,
    Runtime,
    SessionStore,
    Skill,
    SkillLoader,
    SquadRouter,
    TaskStore,
    create_console_app,
)


class _RecordingRunner:
    """记录 run() kwargs,供断言 system_prompt / skill_body 注入。"""
    def __init__(self):
        self.calls = []

    def run(self, task, issue, context, on_event, resume_session_id=None,
            append_system_prompt=None, skill_body=None, cwd=None):
        self.calls.append({
            "task_id": task.id,
            "assignee_id": task.assignee_id,
            "append_system_prompt": append_system_prompt,
            "skill_body": skill_body,
            "cwd": cwd,
        })
        on_event({"type": "progress", "task_id": task.id, "message": "rec"})
        on_event({"type": "done", "task_id": task.id, "output": "rec"})
        return {"status": "done", "output": "rec", "exit_code": 0, "session_id": ""}


def _client(tmp_path, runner=None, with_agents=True):
    """装配 s99 app(含 agent_store)。runner 给定则用之(供注入测试)。"""
    is_ = IssueStore()
    ts = TaskStore()
    ss = SessionStore(str(tmp_path))
    hub = RealtimeHub()
    ms = MemberStore()
    ap = type("AP", (), {"list": lambda self: []})()   # autopilot store stub
    proj = ProjectStore()
    ag = AgentStore() if with_agents else None
    skills_dir = str(tmp_path / "skills" / "code-review")
    os.makedirs(skills_dir, exist_ok=True)
    with open(os.path.join(skills_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(EXAMPLE_SKILL_MD)
    sl = SkillLoader(str(tmp_path / "skills"))
    squad = SquadRouter(is_, ts)
    daemon = Daemon(
        is_, ts, runner or _RecordingRunner(), runtime_id="rt-1",
        session_store=ss, hub=hub, skill_loader=sl,
        project_store=proj, agent_store=ag,
    )
    runtime = Runtime(id="rt-1", name="local", clis=["claude", "codex"])
    app = create_console_app(
        is_, ts, ss, hub, skill_loader=sl, autopilot_store=None,
        member_store=ms, squad_router=squad, daemon=daemon,
        runtime=runtime, project_store=proj, agent_store=ag,
    )
    return TestClient(app), is_, ts, ag, sl


# --- API ---

def test_agents_api_create_and_list(tmp_path):
    client, *_ = _client(tmp_path)
    res = client.post("/api/agents", params={
        "id": "agent-ui", "name": "UI Agent", "cli": "claude",
        "system_prompt": "you do ui", "skill_names": "code-review, react"})
    a = res.json()
    assert a["id"] == "agent-ui"
    assert a["skill_names"] == ["code-review", "react"]
    listed = client.get("/api/agents").json()
    assert any(x["id"] == "agent-ui" for x in listed)


def test_agents_api_patch_system_prompt(tmp_path):
    client, *_ = _client(tmp_path)
    client.post("/api/agents", params={"id": "a", "name": "A"})
    res = client.patch("/api/agents/a", params={"system_prompt": "new prompt"})
    assert res.json()["system_prompt"] == "new prompt"


def test_agents_api_bind_unbind_skill(tmp_path):
    client, *_ = _client(tmp_path)
    client.post("/api/agents", params={"id": "a", "name": "A"})
    bound = client.post("/api/agents/a/skills", params={"skill_name": "code-review"}).json()
    assert bound["skill_names"] == ["code-review"]
    unbound = client.delete("/api/agents/a/skills/code-review").json()
    assert unbound["skill_names"] == []


def test_agents_empty_falls_back_to_derived(tmp_path):
    """AgentStore 无 agent -> GET /api/agents 回退 runtime.clis 派生(agent-1, agent-2)。"""
    client, *_ = _client(tmp_path)
    listed = client.get("/api/agents").json()
    ids = [a["id"] for a in listed]
    assert "agent-1" in ids   # claude -> agent-1
    assert "agent-2" in ids   # codex -> agent-2


def test_agents_merge_derived_and_configured(tmp_path):
    """创建自定义 agent 后,派生 agent-1/agent-2 仍可见(合并,不消失);
    用同 id 可覆盖派生 agent 的配置。"""
    client, *_ = _client(tmp_path)
    client.post("/api/agents", params={"id": "agent-ui", "name": "UI"})
    listed = client.get("/api/agents").json()
    ids = [a["id"] for a in listed]
    assert "agent-ui" in ids          # 自定义
    assert "agent-1" in ids           # 派生仍可见
    assert "agent-2" in ids
    # 用 id agent-1 覆盖派生
    client.post("/api/agents", params={"id": "agent-1", "name": "Custom A1",
                                        "system_prompt": "override"})
    listed = client.get("/api/agents").json()
    a1 = next(a for a in listed if a["id"] == "agent-1")
    assert a1["name"] == "Custom A1"
    assert a1["system_prompt"] == "override"


def test_agents_create_dup_id_rejected(tmp_path):
    client, *_ = _client(tmp_path)
    client.post("/api/agents", params={"id": "a", "name": "A"})
    res = client.post("/api/agents", params={"id": "a", "name": "dup"}).json()
    assert "error" in res


# --- daemon injection via console-wired daemon ---

def test_daemon_injects_agent_system_prompt(tmp_path):
    """console 装配的 daemon 按 assignee_id 注入 agent.system_prompt。"""
    runner = _RecordingRunner()
    client, is_, ts, ag, _ = _client(tmp_path, runner=runner)
    ag.create("agent-fe", "FE", system_prompt="You are a frontend agent.")
    issue = is_.create("ws-1", "do thing", assignee_type="agent",
                       assignee_id="agent-fe")
    ts.enqueue(issue.id, "agent", "agent-fe")
    client.post("/api/daemon/run-once")
    call = runner.calls[-1]
    assert call["append_system_prompt"] == "You are a frontend agent."


def test_daemon_injects_bound_skill(tmp_path):
    """agent 绑 code-review -> skill_body 含 code-review;Issue 文本不参与选择。"""
    runner = _RecordingRunner()
    client, is_, ts, ag, _ = _client(tmp_path, runner=runner)
    ag.create("agent-fe", "FE", skill_names=["code-review"])
    issue = is_.create("ws-1", "review the auth", assignee_type="agent",
                       assignee_id="agent-fe")
    ts.enqueue(issue.id, "agent", "agent-fe")
    client.post("/api/daemon/run-once")
    call = runner.calls[-1]
    assert (call["skill_body"] or "").count("## Skill: code-review") == 1


def test_daemon_unconfigured_agent_has_no_skill_context(tmp_path):
    """assignee 不在 AgentStore(派生 agent) -> 无 system_prompt,无 skill_body。"""
    runner = _RecordingRunner()
    client, is_, ts, _, _ = _client(tmp_path, runner=runner)
    issue = is_.create("ws-1", "review the auth", assignee_type="agent",
                       assignee_id="agent-1")   # 派生 agent,不在 AgentStore
    ts.enqueue(issue.id, "agent", "agent-1")
    client.post("/api/daemon/run-once")
    call = runner.calls[-1]
    assert call["append_system_prompt"] is None
    assert call["skill_body"] is None
