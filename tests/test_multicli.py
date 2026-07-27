import shutil
from sessions.zh.s10_multicli import (
    PathDetector, Runtime, MultiCliRunner, Daemon, IssueStore, TaskStore,
    TaskStatus, IssueStatus, StubRunner, Agent,
)


class RecordingRunner:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def run(self, task, issue, context, on_event, resume_session_id=None,
            append_system_prompt=None):
        self.calls.append((task.id, issue.title, resume_session_id, append_system_prompt))
        on_event({"type": "progress", "task_id": task.id,
                  "message": f"{self.name}: running"})
        return {"status": "done", "output": f"{self.name}: done",
                "exit_code": 0, "session_id": self.name}


def test_path_detector_detects_claude():
    """如果 claude 在 PATH 上(CI 环境),应该检测到。"""
    if shutil.which("claude"):
        assert "claude" in PathDetector.detect()
    else:
        # claude 不在 PATH 上时,不在列表里
        assert "claude" not in PathDetector.detect()


def test_path_detector_is_available():
    assert PathDetector.is_available("nonexistent-cli-xyz") is False
    assert PathDetector.is_available("claude") == (shutil.which("claude") is not None)


def test_runtime_holds_clis():
    rt = Runtime(id="rt-1", name="local", clis=["claude", "codex"])
    assert "claude" in rt.clis
    assert "codex" in rt.clis


def test_multi_cli_runner_register_and_has():
    mcr = MultiCliRunner()
    mcr.register("claude", StubRunner())
    assert mcr.has("claude")
    assert not mcr.has("codex")
    assert "claude" in mcr.available()
    assert mcr.get("claude") is not None


def test_daemon_1_n_claims_multiple_agents():
    """1:N daemon 认领多个不同 agent 的 task。"""
    is_ = IssueStore()
    ts = TaskStore()
    daemon = Daemon(is_, ts, StubRunner(), runtime_id="rt-1")
    # 两个 agent 各一条 task
    i1 = is_.create("ws-1", "t1", assignee_type="agent", assignee_id="agent-1")
    ts.enqueue(i1.id, "agent", "agent-1")
    i2 = is_.create("ws-1", "t2", assignee_type="agent", assignee_id="agent-2")
    ts.enqueue(i2.id, "agent", "agent-2")
    daemon.run_once()
    # 两个 task 都 completed
    for t in ts.list():
        assert t.status == TaskStatus.COMPLETED


def test_daemon_1_n_does_not_claim_member():
    is_ = IssueStore()
    ts = TaskStore()
    daemon = Daemon(is_, ts, StubRunner(), runtime_id="rt-1")
    issue = is_.create("ws-1", "t", assignee_type="member", assignee_id="alice")
    ts.enqueue(issue.id, "member", "alice")
    assert daemon.claim_next() is None


def test_double_daemon_same_runtime_one_wins():
    """同 runtime 双 daemon 抢同一 task 只一个赢。"""
    is_ = IssueStore()
    ts = TaskStore()
    d1 = Daemon(is_, ts, StubRunner(), runtime_id="rt-1")
    d2 = Daemon(is_, ts, StubRunner(), runtime_id="rt-1")
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="agent-1")
    ts.enqueue(issue.id, "agent", "agent-1")
    c1 = d1.claim_next()
    c2 = d2.claim_next()
    assert c1 is not None
    assert c2 is None


def test_daemon_claims_only_own_runtime_tasks():
    """runtime_id 是 claim 边界:rt-1 daemon 不能抢 rt-2 task。"""
    is_ = IssueStore()
    ts = TaskStore()
    daemon = Daemon(is_, ts, StubRunner(), runtime_id="rt-1")
    own = is_.create("ws-1", "own", assignee_type="agent", assignee_id="agent-1")
    other = is_.create("ws-1", "other", assignee_type="agent", assignee_id="agent-2")
    ts.enqueue(own.id, "agent", "agent-1", runtime_id="rt-1")
    ts.enqueue(other.id, "agent", "agent-2", runtime_id="rt-2")

    claimed = daemon.claim_next()

    assert claimed is not None
    assert claimed.issue_id == own.id
    assert ts.list(other.id)[0].status == TaskStatus.QUEUED


def test_daemon_dispatches_by_agent_cli():
    """同一 daemon 托管多个 agent,execute 按 agent.cli 选择 runner。"""
    is_ = IssueStore()
    ts = TaskStore()
    claude = RecordingRunner("claude")
    codex = RecordingRunner("codex")
    mcr = MultiCliRunner()
    mcr.register("claude", claude)
    mcr.register("codex", codex)
    agents = [
        Agent(id="agent-claude", name="Claude", cli="claude", runtime_id="rt-1"),
        Agent(id="agent-codex", name="Codex", cli="codex", runtime_id="rt-1"),
    ]
    daemon = Daemon(is_, ts, StubRunner(), runtime_id="rt-1",
                    agents=agents, multi_runner=mcr)
    i1 = is_.create("ws-1", "use claude", assignee_type="agent",
                    assignee_id="agent-claude")
    i2 = is_.create("ws-1", "use codex", assignee_type="agent",
                    assignee_id="agent-codex")
    ts.enqueue(i1.id, "agent", "agent-claude", runtime_id="rt-1")
    ts.enqueue(i2.id, "agent", "agent-codex", runtime_id="rt-1")

    daemon.run_once()

    assert [call[1] for call in claude.calls] == ["use claude"]
    assert [call[1] for call in codex.calls] == ["use codex"]
    assert all(t.status == TaskStatus.COMPLETED for t in ts.list())


def test_missing_cli_runner_fails_closed():
    """agent.cli 未注册时 task 失败,不会悄悄落到默认 runner。"""
    is_ = IssueStore()
    ts = TaskStore()
    mcr = MultiCliRunner()
    mcr.register("claude", RecordingRunner("claude"))
    agents = [Agent(id="agent-codex", name="Codex", cli="codex", runtime_id="rt-1")]
    daemon = Daemon(is_, ts, StubRunner(), runtime_id="rt-1",
                    agents=agents, multi_runner=mcr)
    issue = is_.create("ws-1", "needs codex", assignee_type="agent",
                       assignee_id="agent-codex")
    ts.enqueue(issue.id, "agent", "agent-codex", runtime_id="rt-1")

    daemon.run_once()

    task = ts.list(issue.id)[0]
    assert task.status == TaskStatus.FAILED
    assert "runner not registered for cli: codex" in task.error
    assert is_.get(issue.id).status == IssueStatus.BLOCKED
