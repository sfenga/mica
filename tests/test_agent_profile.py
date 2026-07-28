"""s14 Agent Profile tests — AgentStore / SkillLoader.get / merge_skills / daemon 注入。

覆盖:
- AgentStore CRUD + bind/unbind skill + JSONL 持久化重载
- SkillLoader.get(name) 命中/未命中
- merge_skills 按 name 去重
- daemon 注入 append_system_prompt(agent.system_prompt)
- daemon 只注入 agent.skill_names 绑定的 skill
- 不同 agent 的 skill 不互相泄漏
- agent_store=None 时无 system_prompt、无 skill_body
"""
import os

from sessions.zh.s14_agents import (
    Agent,
    AgentStore,
    Daemon,
    Issue,
    IssueStore,
    RecordingRunner,
    Skill,
    SkillLoader,
    TaskStore,
    merge_skills,
)


def _write_skill(dirpath, name, body):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: d\n---\n{body}")


def _stores(tmp_path):
    is_ = IssueStore()
    ts = TaskStore()
    return is_, ts


# --- AgentStore ---

def test_agent_store_crud(tmp_path):
    store = AgentStore(data_dir=str(tmp_path))
    a = store.create("agent-x", "X", cli="claude", runtime_id="rt-1",
                     system_prompt="be careful", skill_names=["s1"])
    assert a.id == "agent-x"
    assert a.system_prompt == "be careful"
    assert a.skill_names == ["s1"]
    assert store.get("agent-x").name == "X"
    assert [ag.id for ag in store.list()] == ["agent-x"]
    # duplicate id rejected
    import pytest
    with pytest.raises(KeyError):
        store.create("agent-x", "dup")


def test_agent_store_update(tmp_path):
    store = AgentStore(data_dir=str(tmp_path))
    store.create("a", "A", system_prompt="old")
    a = store.update("a", system_prompt="new", cli="codex")
    assert a.system_prompt == "new"
    assert a.cli == "codex"


def test_agent_store_bind_unbind_skill(tmp_path):
    store = AgentStore(data_dir=str(tmp_path))
    store.create("a", "A")
    a = store.bind_skill("a", "react")
    assert a.skill_names == ["react"]
    # idempotent
    store.bind_skill("a", "react")
    assert store.get("a").skill_names == ["react"]
    a = store.unbind_skill("a", "react")
    assert a.skill_names == []


def test_agent_store_persistence_reload(tmp_path):
    store = AgentStore(data_dir=str(tmp_path))
    store.create("a", "A", system_prompt="persist", skill_names=["react"])
    store2 = AgentStore(data_dir=str(tmp_path))   # 重放 agents.jsonl
    a = store2.get("a")
    assert a is not None
    assert a.system_prompt == "persist"
    assert a.skill_names == ["react"]


# --- SkillLoader.get ---

def test_skill_loader_get(tmp_path):
    _write_skill(str(tmp_path / "react"), "react", "react body")
    sl = SkillLoader(str(tmp_path))
    assert sl.get("react").body == "react body"
    assert sl.get("REACT").name == "react"   # 大小写不敏感
    assert sl.get("nope") is None


def test_skill_loader_get_missing_returns_none(tmp_path):
    _write_skill(str(tmp_path / "review"), "code-review", "review body")
    sl = SkillLoader(str(tmp_path))
    assert sl.get("code-review").body == "review body"
    assert sl.get("review the auth module") is None


# --- merge_skills ---

def test_merge_skills_dedup():
    react = Skill(name="react", description="d", body="react body")
    review = Skill(name="code-review", description="d", body="review body")
    # bound=[react], matched=review -> both, distinct names
    out = merge_skills([react, review])
    assert "## Skill: react" in out
    assert "## Skill: code-review" in out
    # same name -> only once
    out2 = merge_skills([react, react])
    assert out2.count("## Skill: react") == 1
    # empty
    assert merge_skills([]) == ""


# --- daemon injection ---

def _wire(tmp_path, runner, with_agent_store=True, skills=("react", "code-review")):
    """装配 stores + skills + daemon(RecordingRunner)。返回 (is_, ts, ag, sl, daemon)。"""
    is_ = IssueStore()
    ts = TaskStore()
    skills_dir = str(tmp_path / "skills")
    if "react" in skills:
        _write_skill(skills_dir + "/react", "react", "react body")
    if "code-review" in skills:
        _write_skill(skills_dir + "/code-review", "code-review", "review body")
    sl = SkillLoader(skills_dir)
    ag = AgentStore() if with_agent_store else None
    daemon = Daemon(is_, ts, runner, skill_loader=sl, agent_store=ag)
    return is_, ts, ag, sl, daemon


def test_daemon_injects_system_prompt(tmp_path):
    runner = RecordingRunner()
    is_, ts, ag, _, daemon = _wire(tmp_path, runner)
    ag.create("agent-fe", "FE", system_prompt="You are a frontend agent.")
    issue = is_.create("ws-1", "do thing", assignee_type="agent", assignee_id="agent-fe")
    ts.enqueue(issue.id, "agent", "agent-fe")
    daemon.run_once()
    call = runner.calls[-1]
    assert call["append_system_prompt"] == "You are a frontend agent."


def test_daemon_injects_bound_skill(tmp_path):
    runner = RecordingRunner()
    is_, ts, ag, _, daemon = _wire(tmp_path, runner)
    ag.create("agent-fe", "FE", skill_names=["react"])
    issue = is_.create("ws-1", "no keyword hit here", assignee_type="agent", assignee_id="agent-fe")
    ts.enqueue(issue.id, "agent", "agent-fe")
    daemon.run_once()
    call = runner.calls[-1]
    assert "## Skill: react" in (call["skill_body"] or "")
    assert call["append_system_prompt"] is None   # no system_prompt set


def test_daemon_ignores_issue_text_for_skill_selection(tmp_path):
    """Issue title/body 不会触发 skill;只有 agent.skill_names 会注入。"""
    runner = RecordingRunner()
    is_, ts, ag, _, daemon = _wire(tmp_path, runner)
    ag.create("agent-fe", "FE", skill_names=["react"])
    issue = is_.create("ws-1", "review the auth", assignee_type="agent", assignee_id="agent-fe")
    ts.enqueue(issue.id, "agent", "agent-fe")
    daemon.run_once()
    call = runner.calls[-1]
    assert "## Skill: react" in (call["skill_body"] or "")
    assert "## Skill: code-review" not in (call["skill_body"] or "")


def test_agent_skills_isolated(tmp_path):
    """agent A 的绑定 skill 不出现在 agent B 的 skill_body。"""
    runner = RecordingRunner()
    is_, ts, ag, _, daemon = _wire(tmp_path, runner)
    ag.create("agent-a", "A", skill_names=["react"])
    ag.create("agent-b", "B", skill_names=["code-review"])
    ia = is_.create("ws-1", "no keyword hit", assignee_type="agent", assignee_id="agent-a")
    ib = is_.create("ws-1", "no keyword hit", assignee_type="agent", assignee_id="agent-b")
    ta = ts.enqueue(ia.id, "agent", "agent-a")
    tb = ts.enqueue(ib.id, "agent", "agent-b")
    daemon.run_once()   # A
    daemon.run_once()   # B
    call_a = next(c for c in runner.calls if c["task_id"] == ta.id)
    call_b = next(c for c in runner.calls if c["task_id"] == tb.id)
    assert "## Skill: react" in (call_a["skill_body"] or "")
    assert "## Skill: react" not in (call_b["skill_body"] or "")   # 隔离
    assert "## Skill: code-review" in (call_b["skill_body"] or "")


def test_daemon_no_agent_store_has_no_agent_skill_context(tmp_path):
    """agent_store=None:无法按 assignee_id 找 Agent,因此不注入 system_prompt 或 skill_body。"""
    runner = RecordingRunner()
    is_, ts, _, _, daemon = _wire(tmp_path, runner, with_agent_store=False)
    issue = is_.create("ws-1", "review the auth", assignee_type="agent", assignee_id="agent-1")
    ts.enqueue(issue.id, "agent", "agent-1")
    daemon.run_once()
    call = runner.calls[-1]
    assert call["append_system_prompt"] is None
    assert call["skill_body"] is None
