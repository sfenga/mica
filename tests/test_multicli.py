import shutil, tempfile
from sessions.zh.s10_multicli import (
    PathDetector, Runtime, MultiCliRunner, Daemon, IssueStore, TaskStore,
    TaskStatus, IssueStatus, StubRunner, Agent,
)


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
