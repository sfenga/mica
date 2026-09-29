from mica.bootstrap import build_memory_container
from mica.domain import Agent, TaskStatus
from mica.runtime.providers.base import build_task_prompt


def create_issue(container):
    return container.trigger_service.create_issue(
        title="排查订单同步异常",
        body="先阅读代码并说明同步链路。",
        workspace_id="workspace-demo",
        assignee_id="local-assistant",
    )


def test_rapid_comments_merge_into_the_single_queued_task():
    container = build_memory_container()
    issue = create_issue(container)

    first = container.trigger_service.add_comment(issue.id, "补充日志 A", "user-1")
    second = container.trigger_service.add_comment(issue.id, "补充日志 B", "user-1")

    tasks = container.tasks.list(issue.id)
    assert len(tasks) == 1
    assert tasks[0].trigger_type == "comment.created"
    assert tasks[0].trigger_comment_id == second.id
    assert tasks[0].merged_comment_ids == [first.id]

    claimed = container.task_service.claim_next("local", "worker-1")
    assert claimed is not None
    assert claimed.delivered_comment_ids == [first.id, second.id]


def test_running_task_gets_at_most_one_queued_successor():
    container = build_memory_container()
    issue = create_issue(container)
    running = container.task_service.claim_next("local", "worker-1")
    assert running is not None

    first = container.trigger_service.add_comment(issue.id, "先检查重试逻辑", "user-1")
    second = container.trigger_service.add_comment(issue.id, "再检查幂等键", "user-1")

    tasks = container.tasks.list(issue.id)
    assert [task.status for task in tasks] == [TaskStatus.DISPATCHED, TaskStatus.QUEUED]
    successor = tasks[1]
    assert successor.trigger_comment_id == second.id
    assert successor.merged_comment_ids == [first.id]
    assert running.delivered_comment_ids == []


def test_explicit_agent_and_reply_to_agent_comment_override_issue_owner():
    container = build_memory_container()
    container.agents.add(
        Agent(id="reviewer", name="Reviewer", runtime_id="local", provider="stub")
    )
    issue = create_issue(container)
    owner_task = container.task_service.claim_next("local", "worker-1")
    assert owner_task is not None
    container.task_service.start(owner_task.id, "worker-1", owner_task.attempt)
    container.task_service.complete(
        owner_task.id,
        "初步结论：检查同步游标。",
        worker_id="worker-1",
        attempt=owner_task.attempt,
    )
    agent_comment = container.issue_service.get(issue.id).comments[-1]
    assert agent_comment.author_type == "agent"
    assert agent_comment.source_task_id == owner_task.id

    container.trigger_service.add_comment(
        issue.id, "@agent:reviewer 请独立审核", "user-1"
    )
    container.trigger_service.add_comment(
        issue.id,
        "继续解释你的结论",
        "user-1",
        parent_comment_id=agent_comment.id,
    )

    queued = [task for task in container.tasks.list(issue.id) if task.status == TaskStatus.QUEUED]
    assert {task.agent_id for task in queued} == {"local-assistant", "reviewer"}
    owner_followup = next(task for task in queued if task.agent_id == "local-assistant")
    assert owner_followup.trigger_comment_id is not None


def test_plain_reply_in_human_only_thread_does_not_start_issue_assignee():
    container = build_memory_container()
    issue = create_issue(container)
    initial = container.task_service.claim_next("local", "worker-1")
    assert initial is not None

    root = container.issue_service.add_comment(issue.id, "这个问题先由我们讨论", "user-1")
    container.trigger_service.add_comment(
        issue.id,
        "我补充一个业务现象",
        "user-2",
        parent_comment_id=root.id,
    )

    assert [task.status for task in container.tasks.list(issue.id)] == [TaskStatus.DISPATCHED]


def test_reply_routes_to_agent_when_thread_root_was_merged_into_pending_task():
    container = build_memory_container()
    issue = create_issue(container)

    root = container.trigger_service.add_comment(issue.id, "先检查同步链路", "user-1")
    later = container.trigger_service.add_comment(issue.id, "再补充一份日志", "user-1")

    pending = container.tasks.list(issue.id)
    assert len(pending) == 1
    assert pending[0].trigger_comment_id == later.id
    assert root.id in pending[0].merged_comment_ids

    reply = container.trigger_service.add_comment(
        issue.id,
        "同步链路里还需要检查重试次数",
        "user-2",
        parent_comment_id=root.id,
    )

    tasks = container.tasks.list(issue.id)
    assert len(tasks) == 1
    assert tasks[0].agent_id == "local-assistant"
    assert tasks[0].trigger_comment_id == reply.id
    assert root.id in tasks[0].merged_comment_ids
    assert later.id in tasks[0].merged_comment_ids


def test_prompt_uses_only_the_claimed_input_snapshot_in_order():
    container = build_memory_container()
    issue = create_issue(container)
    first = container.trigger_service.add_comment(issue.id, "第一条补充", "user-1")
    second = container.trigger_service.add_comment(issue.id, "第二条补充", "user-1")
    claimed = container.task_service.claim_next("local", "worker-1")
    assert claimed is not None

    later = container.issue_service.add_comment(issue.id, "领取之后才到达", "user-1")
    current_issue = container.issue_service.get(issue.id)
    prompt = build_task_prompt(current_issue, claimed)

    assert prompt.index("第一条补充") < prompt.index("第二条补充")
    assert first.id in claimed.delivered_comment_ids
    assert second.id in claimed.delivered_comment_ids
    assert later.id not in claimed.delivered_comment_ids
    assert "领取之后才到达" not in prompt
