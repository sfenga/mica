import pytest
from sessions.zh.s01_issue_board import (
    IssueStore, IssueStatus, IllegalTransition,
)


def test_create_defaults_todo():
    store = IssueStore()
    issue = store.create("ws-1", "修登录 bug")
    assert issue.status == IssueStatus.TODO
    assert issue.assignee_type == ""


def test_backlog_to_todo():
    store = IssueStore()
    issue = store.create("ws-1", "t")
    issue.status = IssueStatus.BACKLOG  # 直接置 backlog 起点测转移
    store.transition(issue.id, IssueStatus.TODO)
    assert store.get(issue.id).status == IssueStatus.TODO


def test_todo_to_in_progress():
    store = IssueStore()
    issue = store.create("ws-1", "t")
    store.transition(issue.id, IssueStatus.IN_PROGRESS)
    assert store.get(issue.id).status == IssueStatus.IN_PROGRESS


def test_in_progress_to_in_review():
    store = IssueStore()
    issue = store.create("ws-1", "t")
    store.transition(issue.id, IssueStatus.IN_PROGRESS)
    store.transition(issue.id, IssueStatus.IN_REVIEW)
    assert store.get(issue.id).status == IssueStatus.IN_REVIEW


def test_review_approved_to_done():
    store = IssueStore()
    issue = store.create("ws-1", "t")
    store.transition(issue.id, IssueStatus.IN_PROGRESS)
    store.transition(issue.id, IssueStatus.IN_REVIEW)
    store.transition(issue.id, IssueStatus.DONE)
    assert store.get(issue.id).status == IssueStatus.DONE


def test_review_rework_back_to_in_progress():
    store = IssueStore()
    issue = store.create("ws-1", "t")
    store.transition(issue.id, IssueStatus.IN_PROGRESS)
    store.transition(issue.id, IssueStatus.IN_REVIEW)
    store.transition(issue.id, IssueStatus.IN_PROGRESS)   # 要求返工
    assert store.get(issue.id).status == IssueStatus.IN_PROGRESS


def test_in_progress_to_blocked_and_unblock():
    store = IssueStore()
    issue = store.create("ws-1", "t")
    store.transition(issue.id, IssueStatus.IN_PROGRESS)
    store.transition(issue.id, IssueStatus.BLOCKED)
    store.transition(issue.id, IssueStatus.TODO)          # 解除阻塞,重新入队
    assert store.get(issue.id).status == IssueStatus.TODO


def test_blocked_to_cancelled():
    store = IssueStore()
    issue = store.create("ws-1", "t")
    store.transition(issue.id, IssueStatus.IN_PROGRESS)
    store.transition(issue.id, IssueStatus.BLOCKED)
    store.transition(issue.id, IssueStatus.CANCELLED)
    assert store.get(issue.id).status == IssueStatus.CANCELLED


def test_illegal_done_to_todo_rejected():
    store = IssueStore()
    issue = store.create("ws-1", "t")
    store.transition(issue.id, IssueStatus.IN_PROGRESS)
    store.transition(issue.id, IssueStatus.DONE)
    with pytest.raises(IllegalTransition):
        store.transition(issue.id, IssueStatus.TODO)


def test_illegal_skip_to_done_rejected():
    # todo 不能直接 done(必须经过 in_progress)
    store = IssueStore()
    issue = store.create("ws-1", "t")
    with pytest.raises(IllegalTransition):
        store.transition(issue.id, IssueStatus.DONE)


def test_illegal_backlog_to_in_progress_rejected():
    # backlog 必须先到 todo
    store = IssueStore()
    issue = store.create("ws-1", "t")
    issue.status = IssueStatus.BACKLOG
    with pytest.raises(IllegalTransition):
        store.transition(issue.id, IssueStatus.IN_PROGRESS)


def test_assign_to_agent_sets_polymorphic_fields():
    store = IssueStore()
    issue = store.create("ws-1", "t")
    store.assign(issue.id, "agent", "agent-1")
    got = store.get(issue.id)
    assert got.assignee_type == "agent"
    assert got.assignee_id == "agent-1"


def test_list_filters_by_workspace():
    store = IssueStore()
    store.create("ws-1", "a")
    store.create("ws-2", "b")
    assert len(store.list("ws-1")) == 1
    assert len(store.list()) == 2
