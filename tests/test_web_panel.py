import pytest
from fastapi.testclient import TestClient
from sessions.zh.s07_web_panel import (
    create_app, IssueStore, TaskStore, RealtimeHub, IssueStatus, IllegalTransition,
    _issue_to_dict,
)


def _client():
    is_ = IssueStore()
    ts = TaskStore()
    hub = RealtimeHub()
    app = create_app(is_, ts, hub)
    return TestClient(app), is_, ts


def test_list_issues_empty():
    client, _, _ = _client()
    res = client.get("/api/issues")
    assert res.json() == []


def test_create_issue():
    client, is_, ts = _client()
    res = client.post("/api/issues?title=test&assignee_type=agent&assignee_id=a1")
    data = res.json()
    assert data["title"] == "test"
    assert data["status"] == "todo"
    assert data["assignee_id"] == "a1"
    # task 入队
    assert len(ts.list(data["id"])) == 1


def test_status_transition_legal():
    client, is_, _ = _client()
    issue = is_.create("ws-1", "t")
    is_.transition(issue.id, IssueStatus.IN_PROGRESS)
    res = client.patch(f"/api/issues/{issue.id}/status?status=in_review")
    assert res.json()["status"] == "in_review"


def test_status_transition_illegal_rejected():
    client, is_, _ = _client()
    issue = is_.create("ws-1", "t")
    res = client.patch(f"/api/issues/{issue.id}/status?status=done")
    # todo→done is illegal (must go through in_progress)
    assert res.status_code == 200  # FastAPI returns 200 with error body
    assert "error" in res.json()


def test_comment_creates_new_task():
    client, is_, ts = _client()
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    res = client.post(f"/api/issues/{issue.id}/comments?text=fix+this")
    data = res.json()
    assert data["issue"]["comments"] == ["fix this"]
    assert data["task_id"]  # task 入队
    tasks = ts.list(issue.id)
    assert len(tasks) == 1
    assert tasks[0].trigger_comment_id == "fix this"


def test_get_html_page():
    client, _, _ = _client()
    res = client.get("/")
    assert res.status_code == 200
    assert "<html" in res.text.lower()
    assert "mica board" in res.text
