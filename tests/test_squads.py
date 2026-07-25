from sessions.zh.s12_squads import Squad, SquadRouter, IssueStore, TaskStore


def test_squad_registered():
    router = SquadRouter(IssueStore(), TaskStore())
    squad = Squad(id="sq-1", name="team", leader_agent_id="a1",
                  member_agent_ids=["a2", "a3"])
    router.register(squad)
    assert len(router.list_squads()) == 1
    assert router.list_squads()[0].name == "team"


def test_route_creates_task_for_member():
    is_ = IssueStore()
    ts = TaskStore()
    router = SquadRouter(is_, ts)
    squad = Squad(id="sq-1", name="t", leader_agent_id="a1",
                  member_agent_ids=["a2", "a3"])
    router.register(squad)
    issue = is_.create("ws-1", "build", assignee_type="agent", assignee_id="a1")
    chosen = router.route(issue.id, "sq-1")
    assert chosen in ["a2", "a3"]
    tasks = ts.list(issue.id)
    assert len(tasks) == 1
    assert tasks[0].assignee_id == chosen


def test_route_no_members_returns_none():
    is_ = IssueStore()
    ts = TaskStore()
    router = SquadRouter(is_, ts)
    squad = Squad(id="sq-1", name="empty", leader_agent_id="a1",
                  member_agent_ids=[])
    router.register(squad)
    issue = is_.create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    assert router.route(issue.id, "sq-1") is None


def test_route_unknown_squad_returns_none():
    router = SquadRouter(IssueStore(), TaskStore())
    issue = IssueStore().create("ws-1", "t", assignee_type="agent", assignee_id="a1")
    assert router.route(issue.id, "nonexistent") is None
