from sessions.zh.s04_persistence import (
    TaskStore, TaskStatus,
)


def test_running_orphan_recovered_to_failed(tmp_path):
    ts = TaskStore(data_dir=str(tmp_path))
    t = ts.enqueue("issue-1", "agent", "agent-1")
    ts.claim(t.id)
    ts.start(t.id)
    assert ts.get(t.id).status == TaskStatus.RUNNING

    # 重放:_load 末尾调 recover_orphans,running → failed
    ts2 = TaskStore(data_dir=str(tmp_path))
    replayed = ts2.get(t.id)
    assert replayed.status == TaskStatus.FAILED
    assert replayed.error == "interrupted by restart"
    assert replayed.completed_at != ""


def test_dispatched_orphan_recovered_to_failed(tmp_path):
    ts = TaskStore(data_dir=str(tmp_path))
    t = ts.enqueue("issue-1", "agent", "agent-1")
    ts.claim(t.id)
    assert ts.get(t.id).status == TaskStatus.DISPATCHED

    ts2 = TaskStore(data_dir=str(tmp_path))
    replayed = ts2.get(t.id)
    assert replayed.status == TaskStatus.FAILED
    assert replayed.error == "interrupted by restart"


def test_orphan_recovery_idempotent(tmp_path):
    ts = TaskStore(data_dir=str(tmp_path))
    t = ts.enqueue("issue-1", "agent", "agent-1")
    ts.claim(t.id)
    ts.start(t.id)

    ts2 = TaskStore(data_dir=str(tmp_path))
    first = ts2.get(t.id)
    assert first.status == TaskStatus.FAILED
    first_completed_at = first.completed_at

    # 二次重放:failed 不再动,completed_at 不变
    ts3 = TaskStore(data_dir=str(tmp_path))
    second = ts3.get(t.id)
    assert second.status == TaskStatus.FAILED
    assert second.completed_at == first_completed_at
    # 显式调用 recover_orphans 也应返回 0(已无 running/dispatched)
    assert ts3.recover_orphans() == 0
