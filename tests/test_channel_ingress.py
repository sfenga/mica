import hashlib
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mica.api.app import create_app
from mica.application import IdempotencyConflictError, IngressCommand
from mica.bootstrap import build_memory_container
from mica.domain import OutboxMessage, TaskStatus
from mica.integrations.feishu import (
    FeishuApiError,
    FeishuConfig,
    FeishuMessageSender,
    FeishuTypingReceipt,
    FeishuWebhook,
    parse_feishu_target,
    parse_issue_command,
)


def command(event_id="event-1", body="检查产品目录约束"):
    return IngressCommand(
        channel="feishu",
        tenant_key="tenant-1",
        external_event_id=event_id,
        external_user_id="ou-user-1",
        display_name="测试用户",
        conversation_id="oc-chat-1",
        external_message_id=f"message-{event_id}",
        title=body,
        body=body,
        workspace_id="feishu:tenant-1",
        assignee_id="local-assistant",
    )


def test_ingress_event_is_idempotent_and_maps_same_external_user():
    container = build_memory_container()

    first, first_created = container.ingress_service.submit(command())
    duplicate, duplicate_created = container.ingress_service.submit(command())
    second, _ = container.ingress_service.submit(command("event-2", "第二个任务"))

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.id == first.id
    assert len(container.issues.list()) == 2
    assert first.created_by_user_id == second.created_by_user_id
    # KI-001: acceptance cards are gone — messenger ingress no longer produces
    # outbox traffic; chat results are the only messenger replies.
    assert container.channels.list_outbox() == []


def test_same_idempotency_key_rejects_a_different_payload():
    container = build_memory_container()
    container.ingress_service.submit(command())

    with pytest.raises(IdempotencyConflictError):
        container.ingress_service.submit(command(body="被替换的内容"))

    assert len(container.issues.list()) == 1


def test_channel_reply_becomes_a_comment_instead_of_a_second_issue():
    container = build_memory_container()
    issue, _ = container.ingress_service.submit(command())
    reply = replace(
        command("event-2", "继续检查超时重试"),
        related_issue_id=issue.id,
        event_type="comment.created",
    )

    returned, created = container.ingress_service.submit(reply)

    assert created is True
    assert returned.id == issue.id
    assert len(container.issues.list()) == 1
    assert [item.body for item in returned.comments] == ["继续检查超时重试"]
    assert container.channels.list_outbox() == []


def test_terminal_task_event_is_projected_to_feishu_outbox():
    container = build_memory_container()
    issue, _ = container.ingress_service.submit(command())

    assert container.daemon.run_once()
    container.channel_worker.projector.run_once()

    messages = container.channels.list_outbox()
    assert [message.message_type for message in messages] == ["task.completed"]
    assert messages[0].destination == "oc-chat-1"
    assert messages[0].payload["reply_to"] == "message-event-1"
    assert messages[0].payload["reply_in_thread"] is False
    assert "任务已完成" in messages[0].payload["text"]
    assert container.tasks.list(issue.id)[0].status == TaskStatus.COMPLETED


def test_projector_retries_event_when_outbox_write_fails(monkeypatch):
    container = build_memory_container()
    container.ingress_service.submit(command())
    assert container.daemon.run_once()
    projector = container.channel_worker.projector
    original = container.channels.add_outbox
    calls = 0

    def fail_once(message):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary storage failure")
        return original(message)

    monkeypatch.setattr(container.channels, "add_outbox", fail_once)
    with pytest.raises(RuntimeError, match="temporary storage failure"):
        projector.run_once()

    assert projector.run_once() == 1
    assert [item.message_type for item in container.channels.list_outbox()] == ["task.completed"]


def _seed_completed_task_outbox(container):
    container.ingress_service.submit(command())
    assert container.daemon.run_once()
    container.channel_worker.projector.run_once()
    return container.channels.list_outbox()[0]


def test_outbox_without_sender_is_released_for_retry():
    container = build_memory_container()
    _seed_completed_task_outbox(container)

    assert container.channel_worker.run_once() is False

    message = container.channels.list_outbox()[0]
    assert message.status == "pending"
    assert message.attempt == 1
    assert message.last_error == "channel sender is not configured"


def test_outbox_stops_retrying_after_configured_attempt_limit():
    container = build_memory_container()
    _seed_completed_task_outbox(container)
    container.channel_worker.max_attempts = 1

    assert container.channel_worker.run_once() is False

    message = container.channels.list_outbox()[0]
    assert message.status == "failed"
    assert message.attempt == 1


def test_web_console_and_api_share_idempotent_ingress():
    container = build_memory_container()
    with TestClient(create_app(container)) as client:
        assert "MICA / AGENT CONSOLE" in client.get("/app").text
        payload = {
            "title": "修复导出异常",
            "body": "大量导出时中断",
            "workspace_id": "local",
            "assignee_id": "local-assistant",
        }
        first = client.post("/api/issues", json=payload, headers={"Idempotency-Key": "web-key-1"})
        second = client.post("/api/issues", json=payload, headers={"Idempotency-Key": "web-key-1"})

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(container.issues.list()) == 1


def test_health_endpoints_distinguish_process_liveness_from_readiness():
    ready = build_memory_container()
    not_ready = build_memory_container(seed_defaults=False)

    with TestClient(create_app(ready)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health/ready").json() == {"status": "ready"}
    with TestClient(create_app(not_ready)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"


def test_feishu_parser_selects_explicit_agent():
    assert parse_feishu_target("agent=geometry-agent 生成一个会议室布局") == (
        ("agent", "geometry-agent"),
        "生成一个会议室布局",
    )
    assert parse_feishu_target("squad=layout-squad 生成并校验布局") == (
        ("squad", "layout-squad"),
        "生成并校验布局",
    )
    assert parse_feishu_target("普通消息") == (None, "普通消息")


def test_feishu_issue_command_parser_is_token_bounded():
    assert parse_issue_command("/issue 修复导出异常\n第二行描述") == (
        "修复导出异常",
        "第二行描述",
    )
    assert parse_issue_command("/issue") == (None, "")
    assert parse_issue_command("/issuetracker x") is None
    assert parse_issue_command("普通消息") is None


def _feishu_webhook(container):
    config = FeishuConfig(
        app_id="app-id",
        app_secret="app-secret",
        encrypt_key="encrypt-key",
        verification_token="verify-token",
    )
    return config, FeishuWebhook(config, container.chat_ingress)


def _lark_message_event(event_id, message_id, text, thread_id=None, mentions=True):
    event = {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "create_time": str(int(time.time() * 1000)),
            "token": "verify-token",
            "app_id": "app-id",
            "tenant_key": "tenant-1",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou-user-1"},
                "sender_type": "user",
                "tenant_key": "tenant-1",
            },
            "message": {
                "message_id": message_id,
                "chat_id": "oc-chat-1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }
    if thread_id:
        event["event"]["message"]["thread_id"] = thread_id
    if mentions:
        event["event"]["message"]["mentions"] = [
            {
                "key": "@_user_1",
                "id": {"union_id": "on-bot", "user_id": "bot", "open_id": "ou-bot"},
                "name": "mica-bot",
                "tenant_key": "tenant-1",
            }
        ]
    return event


def _signed_handle(webhook, event):
    body = json.dumps(event, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "nonce-1"
    signature = hashlib.sha256(
        (timestamp + nonce + webhook.config.encrypt_key).encode() + body
    ).hexdigest()
    return webhook.handle(
        "http://localhost/api/integrations/feishu/events",
        {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        },
        body,
    )


def test_feishu_sdk_routes_group_message_into_chat_session():
    container = build_memory_container()
    config, webhook = _feishu_webhook(container)

    status, _, content = _signed_handle(
        webhook,
        _lark_message_event("feishu-event-1", "om-message-1", "mica 检查业务规则"),
    )

    assert status == 200
    assert json.loads(content) == {"msg": "success"}
    assert container.issues.list() == []
    sessions = container.chats.sessions_with_unowned_messages()
    assert len(sessions) == 1
    messages = container.chats.list_messages(sessions[0])
    assert [message.role for message in messages] == ["user"]
    assert messages[0].body == "检查业务规则"
    assert messages[0].channel_ingested is True


def test_feishu_webhook_persists_topic_thread_id_on_binding():
    container = build_memory_container()
    config, webhook = _feishu_webhook(container)

    status, _, _ = _signed_handle(
        webhook,
        _lark_message_event(
            "feishu-event-topic-1", "om-topic-message-1", "mica 检查话题消息", thread_id="omt-topic-9"
        ),
    )

    assert status == 200
    session_id = container.chats.sessions_with_unowned_messages()[0]
    binding = container.chats.binding_for_session(session_id)
    assert binding.thread_id == "omt-topic-9"
    assert binding.conversation_id == "oc-chat-1"
    messages = container.chats.list_messages(session_id)
    assert messages[0].body == "检查话题消息"


def test_feishu_group_message_without_mention_is_dropped():
    container = build_memory_container()
    config, webhook = _feishu_webhook(container)

    status, _, _ = _signed_handle(
        webhook,
        _lark_message_event("feishu-event-2", "om-message-2", "没人 @我", mentions=False),
    )

    assert status == 200
    assert container.chats.sessions_with_unowned_messages() == []
    assert container.issues.list() == []


def test_feishu_first_message_explicit_agent_binds_the_session():
    container = build_memory_container()
    container.agents.add(replace(container.agents.get("local-assistant"), id="geometry-agent"))
    config, webhook = _feishu_webhook(container)

    status, _, _ = _signed_handle(
        webhook,
        _lark_message_event("feishu-event-3", "om-message-3", "agent=geometry-agent 画个布局"),
    )

    assert status == 200
    session_id = container.chats.sessions_with_unowned_messages()[0]
    session = container.chats.get_session(session_id)
    assert session.agent_id == "geometry-agent"
    messages = container.chats.list_messages(session_id)
    # The prefix is stripped: the transcript carries the user's actual request.
    assert messages[0].body == "画个布局"


class _FakeFeishuResponse:
    def __init__(self, code=0, msg="ok"):
        self.code = code
        self.msg = msg

    def success(self):
        return self.code == 0

    def get_log_id(self):
        return "log-id"


class _FakeFeishuMessageApi:
    def __init__(self, reply_code=0, create_code=0):
        self.calls = []
        self._reply_code = reply_code
        self._create_code = create_code

    def reply(self, request):
        self.calls.append(("reply", request.message_id, request.request_body))
        return _FakeFeishuResponse(self._reply_code)

    def create(self, request):
        self.calls.append(("create", None, request.request_body))
        return _FakeFeishuResponse(self._create_code)


def _sender(reply_code=0, create_code=0):
    config = FeishuConfig(
        app_id="app-id",
        app_secret="app-secret",
        encrypt_key="encrypt-key",
        verification_token="verify-token",
    )
    sender = FeishuMessageSender(config)
    api = _FakeFeishuMessageApi(reply_code, create_code)
    sender._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=api)))
    return sender, api


def _notice(**payload):
    return OutboxMessage(
        channel="feishu",
        destination="oc-chat-1",
        source_event_id="event-1",
        message_type="task.completed",
        payload=payload,
    )


def test_sender_replies_to_the_origin_message_when_reply_target_present():
    sender, api = _sender()
    notice = _notice(text="任务已完成", reply_to="om-origin-1", reply_in_thread=True)

    sender.send(notice)

    assert [call[0] for call in api.calls] == ["reply"]
    _, message_id, body = api.calls[0]
    assert message_id == "om-origin-1"
    assert body.reply_in_thread is True
    assert body.uuid == notice.id


def test_sender_falls_back_to_chat_level_when_reply_target_is_unusable():
    sender, api = _sender(reply_code=230011)

    sender.send(_notice(text="任务已完成", reply_to="om-recalled", reply_in_thread=False))

    assert [call[0] for call in api.calls] == ["reply", "create"]
    assert api.calls[1][2].receive_id == "oc-chat-1"


def test_sender_does_not_fall_back_on_ambiguous_reply_errors():
    sender, api = _sender(reply_code=230020)

    with pytest.raises(FeishuApiError):
        sender.send(_notice(text="任务已完成", reply_to="om-origin-1"))

    assert [call[0] for call in api.calls] == ["reply"]


def test_sender_posts_at_chat_level_without_reply_target():
    sender, api = _sender()

    sender.send(_notice(text="任务已完成"))

    assert [call[0] for call in api.calls] == ["create"]


def test_typing_receipt_degrades_to_noop_without_lark_client():
    receipt = FeishuTypingReceipt(
        FeishuConfig(
            app_id="app-id",
            app_secret="app-secret",
            encrypt_key="encrypt-key",
            verification_token="verify-token",
        )
    )
    # No Lark credentials in the test environment: add/clear must never raise.
    receipt.add("om-message-1")
    receipt.clear("om-message-1")
