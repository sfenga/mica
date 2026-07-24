import pytest, tempfile
from sessions.zh.s11_workspaces import IssueStore, MemberStore


def test_non_member_denied():
    d = tempfile.mkdtemp()
    ms = MemberStore(data_dir=d)
    ms.add_member("ws-1", "alice")
    is_ = IssueStore()
    is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    # alice 是 ws-1 成员
    assert len(is_.list("ws-1", "alice", ms)) == 1
    # bob 不是 ws-1 成员 → PermissionError
    with pytest.raises(PermissionError):
        is_.list("ws-1", "bob", ms)


def test_member_can_access():
    d = tempfile.mkdtemp()
    ms = MemberStore(data_dir=d)
    ms.add_member("ws-1", "alice")
    is_ = IssueStore()
    is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    assert len(is_.list("ws-1", "alice", ms)) == 1


def test_create_denied_for_non_member():
    d = tempfile.mkdtemp()
    ms = MemberStore(data_dir=d)
    ms.add_member("ws-1", "alice")
    is_ = IssueStore()
    with pytest.raises(PermissionError):
        is_.create("ws-1", "hack", assignee_type="agent", assignee_id="a1",
                   member_id="bob", member_store=ms)
