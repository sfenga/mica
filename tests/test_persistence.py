import json
import os

import pytest

from sessions.zh.s04_persistence import (
    IssueStore, TaskStore, SessionStore,
    IssueStatus, TaskStatus,
    Agent, Daemon, StubRunner,
)


def test_issue_persistence_and_replay(tmp_path):
    store = IssueStore(data_dir=str(tmp_path))
    issue = store.create("ws-1", "修登录 bug",
                         assignee_type="agent", assignee_id="agent-1")
    store.transition(issue.id, IssueStatus.IN_PROGRESS)
    store.transition(issue.id, IssueStatus.IN_REVIEW)
    store.transition(issue.id, IssueStatus.DONE)

    # 重放(新实例从同一 data_dir 读 JSONL,last-wins)
    store2 = IssueStore(data_dir=str(tmp_path))
    replayed = store2.get(issue.id)
    assert replayed is not None
    assert replayed.status == IssueStatus.DONE
    assert replayed.title == "修登录 bug"
    assert replayed.assignee_id == "agent-1"


def test_task_persistence_and_replay(tmp_path):
    ts = TaskStore(data_dir=str(tmp_path))
    t = ts.enqueue("issue-1", "agent", "agent-1")
    assert ts.claim(t.id) is True
    assert ts.start(t.id) is True
    ts.complete(t.id, session_id="sess-abc")

    ts2 = TaskStore(data_dir=str(tmp_path))
    replayed = ts2.get(t.id)
    assert replayed is not None
    assert replayed.status == TaskStatus.COMPLETED
    assert replayed.session_id == "sess-abc"
    assert replayed.claimed_at != ""


def test_session_events_persisted_with_ts(tmp_path):
    ss = SessionStore(str(tmp_path))
    ss.append("task-1", {"type": "progress", "message": "working"})
    ss.append("task-1", {"type": "done", "output": "ok"})
    events = ss.load("task-1")
    assert len(events) == 2
    assert events[0]["message"] == "working"
    assert events[1]["output"] == "ok"
    # ts 自动注入
    assert "ts" in events[0]
    assert "ts" in events[1]
    # 不覆盖已存在 ts
    ss.append("task-1", {"type": "x", "ts": "fixed"})
    assert ss.load("task-1")[-1]["ts"] == "fixed"


def test_daemon_records_claim_and_result_events(tmp_path):
    issue_store = IssueStore(data_dir=str(tmp_path))
    task_store = TaskStore(data_dir=str(tmp_path))
    sessions = SessionStore(str(tmp_path))
    agent = Agent(id="agent-1", name="Claude")
    daemon = Daemon(issue_store, task_store, agent, StubRunner(),
                    session_store=sessions)
    issue = issue_store.create("ws-1", "修登录 bug",
                               assignee_type="agent", assignee_id="agent-1")
    task = task_store.enqueue(issue.id, "agent", "agent-1")
    daemon.run_once()

    events = sessions.load(task.id)
    kinds = [e.get("type") for e in events]
    # claim 事件 + 终态 result 事件
    assert "claim" in kinds
    assert "result" in kinds
    claim_ev = next(e for e in events if e.get("type") == "claim")
    assert claim_ev["task_id"] == task.id
    result_ev = next(e for e in events if e.get("type") == "result")
    assert result_ev["status"] == "done"
    # task 完成 + issue in_review
    assert task_store.get(task.id).status == TaskStatus.COMPLETED
    assert issue_store.get(issue.id).status == IssueStatus.IN_REVIEW


def test_summarize_truncation(tmp_path):
    ss = SessionStore(str(tmp_path))
    task_id = "task-x"
    for i in range(150):
        ss.append(task_id, {"type": "progress", "seq": i})
    # threshold=100, keep=20 → 触发截断
    removed = ss.summarize_if_needed(task_id, threshold=100, keep=20)
    assert removed == 130
    events = ss.load(task_id)
    # 1 summary + 20 recent = 21
    assert len(events) == 21
    assert events[0]["type"] == "summary"
    assert events[0]["dropped_count"] == 130
    assert events[0]["kept"] == 20
    # 近 20 条保留原顺序
    assert events[1]["seq"] == 130
    assert events[-1]["seq"] == 149


def test_summarize_not_triggered_below_threshold(tmp_path):
    ss = SessionStore(str(tmp_path))
    task_id = "task-y"
    for i in range(50):
        ss.append(task_id, {"type": "progress", "seq": i})
    removed = ss.summarize_if_needed(task_id, threshold=100, keep=20)
    assert removed == 0
    events = ss.load(task_id)
    assert len(events) == 50


def test_data_dir_none_in_memory():
    # data_dir=None → 纯内存,无文件落盘,与 s01-s03 行为兼容
    store = IssueStore()
    issue = store.create("ws-1", "t")
    store.transition(issue.id, IssueStatus.IN_PROGRESS)
    assert store.get(issue.id).status == IssueStatus.IN_PROGRESS
    ts = TaskStore()
    t = ts.enqueue(issue.id, "agent", "agent-1")
    assert ts.claim(t.id) is True
    # 无 JSONL 文件产生
    assert not os.path.exists("issues.jsonl")
    assert not os.path.exists("tasks.jsonl")
