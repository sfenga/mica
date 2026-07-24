import pytest
from sessions.zh.s02_task_daemon import (
    IssueStore, TaskStore, TaskStatus, IssueStatus, Agent, Daemon,
    StubRunner, FailingStubRunner, Issue,
)


def test_enqueue_creates_queued_task():
    ts = TaskStore()
    t = ts.enqueue("issue-1", "agent", "agent-1")
    assert t.status == TaskStatus.QUEUED
    assert t.assignee_id == "agent-1"


def test_claim_marks_dispatched():
    ts = TaskStore()
    t = ts.enqueue("issue-1", "agent", "agent-1")
    assert ts.claim(t.id) is True
    assert ts.get(t.id).status == TaskStatus.DISPATCHED


def test_double_claim_only_one_succeeds():
    ts = TaskStore()
    t = ts.enqueue("issue-1", "agent", "agent-1")
    d1 = Daemon(IssueStore(), ts, Agent(id="agent-1", name="A"), StubRunner())
    d2 = Daemon(IssueStore(), ts, Agent(id="agent-1", name="A"), StubRunner())
    c1 = d1.claim_next()
    c2 = d2.claim_next()
    assert c1 is not None
    assert c2 is None


def test_run_once_completes_task_and_issue_in_review():
    is_ = IssueStore()
    ts = TaskStore()
    daemon = Daemon(is_, ts, Agent(id="agent-1", name="A"), StubRunner())
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="agent-1")
    ts.enqueue(issue.id, "agent", "agent-1")
    daemon.run_once()
    assert is_.get(issue.id).status == IssueStatus.IN_REVIEW
    tasks = ts.list(issue.id)
    assert tasks[0].status == TaskStatus.COMPLETED


def test_failed_task_drives_issue_blocked():
    is_ = IssueStore()
    ts = TaskStore()
    daemon = Daemon(is_, ts, Agent(id="agent-1", name="A"), FailingStubRunner())
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="agent-1")
    ts.enqueue(issue.id, "agent", "agent-1")
    daemon.run_once()
    assert is_.get(issue.id).status == IssueStatus.BLOCKED
    assert ts.list(issue.id)[0].status == TaskStatus.FAILED


def test_member_assigned_issue_not_claimed():
    is_ = IssueStore()
    ts = TaskStore()
    daemon = Daemon(is_, ts, Agent(id="agent-1", name="A"), StubRunner())
    issue = is_.create("ws-1", "m", assignee_type="member", assignee_id="alice")
    ts.enqueue(issue.id, "member", "alice")
    assert daemon.claim_next() is None


def test_one_issue_many_tasks_history():
    is_ = IssueStore()
    ts = TaskStore()
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="agent-1")
    t1 = ts.enqueue(issue.id, "agent", "agent-1")
    ts.complete(t1.id)  # 旧 task completed
    t2 = ts.enqueue(issue.id, "agent", "agent-1")  # 新 task queued
    tasks = ts.list(issue.id)
    assert len(tasks) == 2
    assert tasks[0].status == TaskStatus.COMPLETED
    assert tasks[1].status == TaskStatus.QUEUED
