"""s13 — project & workdir 测试"""
import io

import sessions.zh.s13_project_workdir as m
from sessions.zh.s13_project_workdir import (
    Agent,
    ClaudeCliRunner,
    Daemon,
    Issue,
    IssueStore,
    Project,
    ProjectStore,
    StubRunner,
    TaskStore,
)


def test_project_create():
    ps = ProjectStore()
    p = ps.create("ws-1", "my-project", local_path="/tmp/test-project")
    assert p.name == "my-project"
    assert p.local_path == "/tmp/test-project"
    assert p.id
    assert p.workspace_id == "ws-1"


def test_project_store_list_get():
    ps = ProjectStore()
    p1 = ps.create("ws-1", "proj-a", local_path="/tmp/a")
    p2 = ps.create("ws-1", "proj-b")
    projects = ps.list("ws-1")
    assert {p.id for p in projects} == {p1.id, p2.id}
    assert ps.get(p1.id).name == "proj-a"
    assert ps.get("missing") is None


def test_issue_with_project():
    is_ = IssueStore()
    ps = ProjectStore()
    p = ps.create("ws-1", "proj", local_path="/tmp/proj")
    issue = is_.create("ws-1", "test", assignee_type="agent", assignee_id="a1",
                       project_id=p.id)
    assert issue.project_id == p.id


def test_issue_set_project():
    is_ = IssueStore()
    ps = ProjectStore()
    p = ps.create("ws-1", "proj", local_path="/tmp/proj")
    issue = is_.create("ws-1", "test", assignee_type="agent", assignee_id="a1")
    is_.set_project(issue.id, p.id)
    reloaded = is_.get(issue.id)
    assert reloaded.project_id == p.id


def test_task_workdir_resolved():
    is_ = IssueStore()
    ts = TaskStore()
    ps = ProjectStore()
    p = ps.create("ws-1", "proj", local_path="/tmp/proj")
    issue = is_.create("ws-1", "test", assignee_type="agent", assignee_id="a1",
                       project_id=p.id)
    ts.enqueue(issue.id, "agent", "a1")
    daemon = Daemon(is_, ts, StubRunner(), project_store=ps)
    daemon.run_once()
    tasks = ts.list(issue.id)
    assert tasks
    assert tasks[-1].workdir == "/tmp/proj"


def test_task_workdir_empty_when_no_project():
    is_ = IssueStore()
    ts = TaskStore()
    issue = is_.create("ws-1", "test", assignee_type="agent", assignee_id="a1")
    ts.enqueue(issue.id, "agent", "a1")
    daemon = Daemon(is_, ts, StubRunner(), project_store=None)
    daemon.run_once()
    tasks = ts.list(issue.id)
    assert tasks[-1].workdir == ""


def test_runner_uses_cwd():
    is_ = IssueStore()
    ts = TaskStore()
    ps = ProjectStore()
    p = ps.create("ws-1", "proj", local_path="/tmp/myrepo")
    issue = is_.create("ws-1", "run thing", assignee_type="agent", assignee_id="a1",
                       project_id=p.id)
    ts.enqueue(issue.id, "agent", "a1")

    captured = []

    class FakeProc:
        stdout = io.StringIO(
            '{"type":"result","subtype":"success","result":"ok","is_error":false,"session_id":"ses1"}\n'
        )
        stderr = io.StringIO('')

        def wait(self):
            return 0

    def fake_popen(cmd, **kw):
        captured.append(kw.get("cwd"))
        return FakeProc()

    m.subprocess.Popen = fake_popen
    try:
        runner = ClaudeCliRunner()
        daemon = Daemon(is_, ts, runner, project_store=ps)
        daemon.run_once()
    finally:
        import importlib
        importlib.reload(m.subprocess)
    assert captured, "Popen should have been called"
    assert captured[0] == "/tmp/myrepo"


def test_runner_cwd_none_when_no_project():
    is_ = IssueStore()
    ts = TaskStore()
    issue = is_.create("ws-1", "run thing", assignee_type="agent", assignee_id="a1")
    ts.enqueue(issue.id, "agent", "a1")

    captured = []

    class FakeProc:
        stdout = io.StringIO(
            '{"type":"result","subtype":"success","result":"ok","is_error":false,"session_id":"ses1"}\n'
        )
        stderr = io.StringIO('')

        def wait(self):
            return 0

    def fake_popen(cmd, **kw):
        captured.append(kw.get("cwd"))
        return FakeProc()

    m.subprocess.Popen = fake_popen
    try:
        runner = ClaudeCliRunner()
        daemon = Daemon(is_, ts, runner, project_store=None)
        daemon.run_once()
    finally:
        import importlib
        importlib.reload(m.subprocess)
    assert captured
    assert captured[0] is None
