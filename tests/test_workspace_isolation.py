from sessions.zh.s11_workspaces import IssueStore, MemberStore, IssueStatus


def test_cross_workspace_no_leak():
    is_ = IssueStore()
    is_.create("ws-a", "task A")
    is_.create("ws-b", "task B")
    assert len(is_.list("ws-a")) == 1
    assert is_.list("ws-a")[0].title == "task A"
    assert len(is_.list("ws-b")) == 1
    assert len(is_.list()) == 2


def test_switch_workspace():
    is_ = IssueStore()
    is_.create("ws-a", "a")
    is_.create("ws-b", "b")
    assert len(is_.list("ws-a")) == 1
    assert len(is_.list("ws-b")) == 1


def test_workspace_filter_with_tasks():
    from sessions.zh.s11_workspaces import TaskStore, TaskStatus
    is_ = IssueStore()
    ts = TaskStore()
    i1 = is_.create("ws-a", "t1", assignee_type="agent", assignee_id="a1")
    i2 = is_.create("ws-b", "t2", assignee_type="agent", assignee_id="a1")
    ts.enqueue(i1.id, "agent", "a1")
    ts.enqueue(i2.id, "agent", "a1")
    # task list 也可以按 issue_id 过滤,但跨 workspace 不串(通过 issue 间接)
    assert len(ts.list(i1.id)) == 1
    assert len(ts.list(i2.id)) == 1


def test_member_store_persistence():
    import tempfile
    d = tempfile.mkdtemp()
    ms = MemberStore(data_dir=d)
    ms.add_member("ws-1", "alice")
    ms2 = MemberStore(data_dir=d)
    assert ms2.is_member("ws-1", "alice")
