import pytest

from sessions.zh.s12_squads import (
    IssueStatus,
    IssueStore,
    Squad,
    SquadMember,
    SquadRouter,
    TaskStore,
    main as s12_demo_main,
)


def build_router():
    issue_store = IssueStore()
    task_store = TaskStore()
    router = SquadRouter(issue_store, task_store)
    squad = Squad(
        id="sq-1",
        name="Frontend Team",
        leader_agent_id="lead",
        instructions="Accessibility fixes go first.",
        members=[
            SquadMember("agent", "lead", "leader", ["planning", "review"]),
            SquadMember("agent", "ui", "frontend", ["react", "css"]),
            SquadMember("agent", "qa", "tester", ["playwright"]),
            SquadMember("member", "alice", "pm"),
        ],
    )
    router.register(squad)
    return issue_store, task_store, router, squad


def test_assign_issue_to_squad_enqueues_leader_task():
    issue_store, task_store, router, _ = build_router()
    issue = issue_store.create("ws-1", "build landing page")

    task = router.assign_issue_to_squad(issue.id, "sq-1")

    assert issue_store.get(issue.id).assignee_type == "squad"
    assert issue_store.get(issue.id).assignee_id == "sq-1"
    assert task.assignee_type == "agent"
    assert task.assignee_id == "lead"
    assert task.is_leader_task is True
    assert task.squad_id == "sq-1"
    assert task_store.list(issue.id) == [task]


def test_leader_briefing_contains_protocol_roster_skills_and_instructions():
    _, _, router, _ = build_router()

    briefing = router.build_leader_briefing("sq-1")

    assert "## Squad Operating Protocol" in briefing
    assert "## Squad Roster" in briefing
    assert "[@ui](mention://agent/ui)" in briefing
    assert "skills: react, css" in briefing
    assert "Accessibility fixes go first." in briefing


def test_leader_delegates_worker_task_and_records_activity():
    issue_store, task_store, router, _ = build_router()
    issue = issue_store.create("ws-1", "fix mobile nav")
    router.assign_issue_to_squad(issue.id, "sq-1")

    worker_task = router.delegate(issue.id, "sq-1", "ui", "please implement the responsive nav")
    activity = router.record_activity(issue.id, "sq-1", "lead", "action", "delegated to ui")

    assert worker_task.assignee_id == "ui"
    assert worker_task.is_leader_task is False
    assert worker_task.squad_id == "sq-1"
    assert worker_task.handoff_note == "please implement the responsive nav"
    assert "mention://agent/ui" in issue_store.get(issue.id).comments[-2]
    assert activity.outcome == "action"
    assert router.list_activities(issue.id) == [activity]
    assert len(task_store.list(issue.id)) == 2


def test_delegate_rejects_agent_outside_squad():
    issue_store, _, router, _ = build_router()
    issue = issue_store.create("ws-1", "fix nav")

    with pytest.raises(PermissionError):
        router.delegate(issue.id, "sq-1", "outsider", "please help")


def test_create_child_issue_assigned_to_agent_enqueues_worker_task():
    issue_store, task_store, router, _ = build_router()
    parent = issue_store.create("ws-1", "ship checkout")
    router.assign_issue_to_squad(parent.id, "sq-1")

    child = router.create_child_issue(parent.id, "Stage 1: build form", "agent", "ui", stage=1)

    child_tasks = task_store.list(child.id)
    assert len(child_tasks) == 1
    assert child_tasks[0].assignee_id == "ui"
    assert child_tasks[0].is_leader_task is False
    assert child_tasks[0].squad_id == "sq-1"
    assert child_tasks[0].handoff_note == "child issue stage 1: Stage 1: build form"


def test_worker_report_wakes_leader_once():
    issue_store, task_store, router, _ = build_router()
    issue = issue_store.create("ws-1", "fix flaky test")
    leader_task = router.assign_issue_to_squad(issue.id, "sq-1")
    task_store.complete(leader_task.id)
    router.delegate(issue.id, "sq-1", "qa", "investigate the flaky test")

    wake = router.worker_report(issue.id, "sq-1", "qa", "done, root cause found")
    duplicate = router.worker_report(issue.id, "sq-1", "qa", "one more note")

    assert wake is not None
    assert wake.assignee_id == "lead"
    assert wake.is_leader_task is True
    assert wake.handoff_note == "worker qa reported"
    assert duplicate is None
    leader_tasks = [t for t in task_store.list(issue.id) if t.assignee_id == "lead"]
    assert len(leader_tasks) == 2


def test_child_stage_barrier_wakes_parent_leader_only_when_stage_closes():
    issue_store, task_store, router, _ = build_router()
    parent = issue_store.create("ws-1", "ship checkout")
    issue_store.transition(parent.id, IssueStatus.IN_PROGRESS)
    first_leader_task = router.assign_issue_to_squad(parent.id, "sq-1")
    task_store.complete(first_leader_task.id)

    child_a = router.create_child_issue(parent.id, "build form", "agent", "ui", stage=1)
    child_b = router.create_child_issue(parent.id, "test form", "agent", "qa", stage=1)
    child_c = router.create_child_issue(parent.id, "polish", "agent", "ui", stage=2)

    issue_store.transition(child_a.id, IssueStatus.IN_PROGRESS)
    issue_store.transition(child_a.id, IssueStatus.DONE)
    assert router.notify_child_done(child_a.id) is None

    issue_store.transition(child_b.id, IssueStatus.IN_PROGRESS)
    issue_store.transition(child_b.id, IssueStatus.DONE)
    wake = router.notify_child_done(child_b.id)

    assert wake is not None
    assert wake.assignee_id == "lead"
    assert wake.is_leader_task is True
    assert wake.squad_id == "sq-1"
    assert wake.handoff_note == "child stage completed"
    assert issue_store.get(child_c.id).status == IssueStatus.TODO


def test_member_status_marks_active_agents_working():
    issue_store, task_store, router, _ = build_router()
    issue = issue_store.create("ws-1", "qa sweep")
    task = router.delegate(issue.id, "sq-1", "qa", "run tests")
    task_store.claim(task.id)
    task_store.start(task.id)

    by_id = {row["member_id"]: row for row in router.member_status("sq-1")}

    assert by_id["qa"]["status"] == "working"
    assert by_id["qa"]["active_issues"] == [issue.id]
    assert by_id["ui"]["status"] == "idle"
    assert by_id["alice"]["status"] == "human"


def test_demo_prints_full_squad_lifecycle(capsys):
    assert s12_demo_main() == 0

    out = capsys.readouterr().out
    assert "=== s12 demo: squad leader lifecycle ===" in out
    assert "1) assign parent to squad" in out
    assert "2) leader creates Stage 1 children and stops" in out
    assert "stage barrier: open, leader wake=none" in out
    assert "stage barrier: closed, leader wake=agent-lead" in out
    assert "5) leader reviews Stage 1 results and promotes Stage 2" in out
    assert "member status:" in out
    assert "6) parent timeline comments:" in out
