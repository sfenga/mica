"""The /issue command: synchronous issue creation, never a chat run."""

from mica.bootstrap import build_memory_container
from mica.domain import AssigneeType, IssueStatus
from tests.test_chat_ingress import make_command


def issue_container():
    return build_memory_container()


def test_issue_command_creates_issue_with_origin_and_receipt():
    container = issue_container()

    container.chat_ingress.submit_issue_command(
        make_command("om-cmd-1", thread_id="omt-3"), "修复导出异常", "修复导出异常\n大量导出时中断"
    )

    issues = container.issues.list()
    assert len(issues) == 1
    issue = issues[0]
    assert issue.title == "修复导出异常"
    assert issue.body == "修复导出异常\n大量导出时中断"
    assert issue.assignee_type == AssigneeType.AGENT
    assert issue.assignee_id == "local-assistant"
    origin = container.channels.get_origin(issue.id)
    assert origin.external_message_id == "om-cmd-1"
    assert origin.thread_id == "omt-3"
    assert origin.conversation_id == "oc-chat-1"
    receipt = container.channels.list_outbox()[0]
    assert "已创建" in receipt.payload["text"]
    assert issue.id in receipt.payload["text"]
    assert receipt.payload["reply_to"] == "om-cmd-1"
    assert receipt.payload["reply_in_thread"] is True


def test_bare_command_replies_usage_without_creating_an_issue():
    container = issue_container()

    container.chat_ingress.submit_issue_command(make_command("om-cmd-1"), None, "")

    assert container.issues.list() == []
    usage = container.channels.list_outbox()[0]
    assert "用法：/issue" in usage.payload["text"]


def test_command_is_durable_but_invisible_to_agent_input():
    container = issue_container()
    container.chat_ingress.submit_issue_command(make_command("om-cmd-1"), "修东西", "修东西")

    session_id = container.chats.find_session("feishu", "tenant-1", "oc-chat-1", None).id
    # The command never blocks a later chat batch...
    assert container.chat_ingress.flush_session(session_id) is None
    # ...and never enters the transcript the agent sees.
    assert container.chats.list_messages(session_id) == []

    container.chat_ingress.submit(make_command("om-2", "顺便帮我看下日志"))
    container.chat_ingress.flush_session(session_id)
    chat_task = next(task for task in container.tasks.list() if task.chat_session_id == session_id)
    sealed = container.chats.input_messages(chat_task.chat_input_task_id)
    assert [message.body for message in sealed] == ["顺便帮我看下日志"]


def test_chat_created_issue_notification_replies_into_the_topic():
    container = issue_container()
    container.chat_ingress.submit_issue_command(
        make_command("om-cmd-1", thread_id="omt-7"), "修东西", "修东西"
    )

    assert container.daemon.run_once()
    container.channel_worker.projector.run_once()

    notices = [message.message_type for message in container.channels.list_outbox()]
    # The synchronous /issue receipt plus the issue task's terminal notice.
    assert notices == ["chat.notice", "task.completed"]
    completion = container.channels.list_outbox()[1]
    assert completion.payload["reply_to"] == "om-cmd-1"
    assert completion.payload["reply_in_thread"] is True
    assert container.issues.list()[0].status == IssueStatus.IN_REVIEW


def test_duplicate_command_event_does_not_create_a_second_issue():
    container = issue_container()
    container.chat_ingress.submit_issue_command(make_command("om-cmd-1"), "修东西", "修东西")

    container.chat_ingress.submit_issue_command(make_command("om-cmd-1"), "修东西", "修东西")

    assert len(container.issues.list()) == 1
