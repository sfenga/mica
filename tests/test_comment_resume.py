import pytest
from sessions.zh.s05_comment_resume import (
    IssueStore, TaskStore, TaskStatus, IssueStatus, Agent, Daemon,
    StubRunner, FailingStubRunner, comment_and_enqueue, Issue,
)


class _RunnerWithSid:
    """假 runner,返回指定 session_id。"""
    def __init__(self, sid, status="done"):
        self.sid = sid
        self.status = status
    def run(self, task, issue, context, on_event, resume_session_id=None):
        on_event({"type": "progress", "task_id": task.id, "message": "fake"})
        return {"status": self.status, "output": "ok", "exit_code": 0, "session_id": self.sid}


def test_comment_appends_and_does_not_change_issue_status():
    is_ = IssueStore()
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    is_.transition(issue.id, IssueStatus.IN_PROGRESS)
    is_.transition(issue.id, IssueStatus.IN_REVIEW)
    is_.comment(issue.id, "please also handle X")
    got = is_.get(issue.id)
    assert got.status == IssueStatus.IN_REVIEW   # 不变
    assert got.comments == ["please also handle X"]


def test_comment_and_enqueue_creates_queued_task():
    is_ = IssueStore()
    ts = TaskStore()
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    task = comment_and_enqueue(is_, ts, issue.id, "fix this too")
    assert task.status == TaskStatus.QUEUED
    assert task.assignee_id == "a1"
    assert is_.get(issue.id).status == IssueStatus.TODO   # issue 不变


def test_daemon_finds_prior_session_id_and_passes_resume():
    is_ = IssueStore()
    ts = TaskStore()
    daemon = Daemon(is_, ts, Agent(id="a1", name="A"), _RunnerWithSid("ses-1"))
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    ts.enqueue(issue.id, "agent", "a1")
    daemon.run_once()  # 首次:task completed, session_id=ses-1
    # 评论入队新 task
    comment_and_enqueue(is_, ts, issue.id, "redo")
    # 第二次:daemon 认领新 task,应查到 prior session_id=ses-1
    # 用一个捕获 resume_session_id 的 runner
    captured = []
    class _CaptureRunner:
        def run(self, task, issue, context, on_event, resume_session_id=None):
            captured.append(resume_session_id)
            return {"status": "done", "output": "ok", "exit_code": 0, "session_id": "ses-1"}
    daemon.runner = _CaptureRunner()
    daemon.run_once()
    assert captured[0] == "ses-1"   # 传了 prior session_id


def test_resume_did_not_land_overwrites():
    is_ = IssueStore()
    ts = TaskStore()
    # 首次跑,session_id=ses-old
    daemon = Daemon(is_, ts, Agent(id="a1", name="A"), _RunnerWithSid("ses-old"))
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    ts.enqueue(issue.id, "agent", "a1")
    daemon.run_once()
    # 评论 + 第二次跑,claude 回吐 ses-new(不同)
    comment_and_enqueue(is_, ts, issue.id, "redo")
    daemon.runner = _RunnerWithSid("ses-new")
    daemon.run_once()
    tasks = ts.list(issue.id)
    assert tasks[-1].session_id == "ses-new"  # 覆盖成新 id


def test_no_prior_task_no_resume():
    is_ = IssueStore()
    ts = TaskStore()
    captured = []
    class _CaptureRunner:
        def run(self, task, issue, context, on_event, resume_session_id=None):
            captured.append(resume_session_id)
            return {"status": "done", "output": "ok", "exit_code": 0, "session_id": "ses-1"}
    daemon = Daemon(is_, ts, Agent(id="a1", name="A"), _CaptureRunner())
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    ts.enqueue(issue.id, "agent", "a1")
    daemon.run_once()
    assert captured[0] is None   # 无 prior task → 不传 resume


def test_rerun_creates_new_task_with_rerun_of():
    is_ = IssueStore()
    ts = TaskStore()
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    t1 = ts.enqueue(issue.id, "agent", "a1")
    ts.complete(t1.id, "ses-1")
    t2 = is_.rerun_task(issue.id, ts)
    assert t2.status == TaskStatus.QUEUED
    assert t2.rerun_of_task_id == t1.id
