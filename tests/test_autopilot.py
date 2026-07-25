import datetime, os, tempfile
from sessions.zh.s09_autopilot import Autopilot, AutopilotStore, Scheduler, IssueStore, TaskStore


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def test_cron_to_next_run():
    d = tempfile.mkdtemp()
    aps = AutopilotStore(data_dir=d)
    is_ = IssueStore()
    ts = TaskStore()
    sched = Scheduler(aps, is_, ts)
    ap = Autopilot(id="ap-1", cron="0 9 * * *", agent_id="a1", title="daily")
    sched._compute_next_run(ap, _now())
    assert ap.next_run  # ISO 时间字符串


def test_fire_creates_issue_and_task():
    d = tempfile.mkdtemp()
    aps = AutopilotStore(data_dir=d)
    is_ = IssueStore()
    ts = TaskStore()
    sched = Scheduler(aps, is_, ts)
    ap = Autopilot(id="ap-1", cron="* * * * *", agent_id="a1", title="tick")
    ap.next_run = "2020-01-01T00:00:00+00:00"  # 过去
    aps.save(ap)
    triggered = sched.tick(now=_now())
    assert triggered == 1
    issues = is_.list()
    assert len(issues) == 1
    assert issues[0].title == "tick"
    tasks = ts.list(issues[0].id)
    assert len(tasks) == 1


def test_fire_advances_next_run():
    d = tempfile.mkdtemp()
    aps = AutopilotStore(data_dir=d)
    sched = Scheduler(aps, IssueStore(), TaskStore())
    ap = Autopilot(id="ap-1", cron="0 9 * * *", agent_id="a1", title="daily")
    ap.next_run = "2020-01-01T00:00:00+00:00"
    aps.save(ap)
    sched.tick(now=_now())
    ap2 = aps.get("ap-1")
    assert ap2.next_run != "2020-01-01T00:00:00+00:00"  # 推进了


def test_disabled_not_triggered():
    d = tempfile.mkdtemp()
    aps = AutopilotStore(data_dir=d)
    sched = Scheduler(aps, IssueStore(), TaskStore())
    ap = Autopilot(id="ap-1", cron="* * * * *", agent_id="a1", title="t", enabled=False)
    ap.next_run = "2020-01-01T00:00:00+00:00"
    aps.save(ap)
    assert sched.tick(now=_now()) == 0


def test_consecutive_failures_disable():
    d = tempfile.mkdtemp()
    aps = AutopilotStore(data_dir=d)
    ap = Autopilot(id="ap-1", cron="* * * * *", agent_id="a1", title="t")
    aps.save(ap)
    sched = Scheduler(aps, IssueStore(), TaskStore())
    for _ in range(5):
        sched.mark_fail("ap-1")
    assert aps.get("ap-1").enabled is False


def test_success_resets_fail_count():
    d = tempfile.mkdtemp()
    aps = AutopilotStore(data_dir=d)
    ap = Autopilot(id="ap-1", cron="* * * * *", agent_id="a1", title="t")
    aps.save(ap)
    sched = Scheduler(aps, IssueStore(), TaskStore())
    sched.mark_fail("ap-1")
    sched.mark_fail("ap-1")
    assert aps.get("ap-1").fail_count == 2
    sched.mark_success("ap-1")
    assert aps.get("ap-1").fail_count == 0


def test_restart_no_retrigger():
    """重启后 next_run 已推进,不重触发已过槽位。"""
    d = tempfile.mkdtemp()
    aps = AutopilotStore(data_dir=d)
    sched = Scheduler(aps, IssueStore(), TaskStore())
    ap = Autopilot(id="ap-1", cron="0 9 * * *", agent_id="a1", title="daily")
    # 已经触发过,next_run 推进到未来
    ap.next_run = "2099-01-01T09:00:00+00:00"
    aps.save(ap)
    assert sched.tick(now=_now()) == 0  # 未来不触发
