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
from mica.domain import AssigneeType, OutboxMessage, TaskStatus
from mica.integrations.feishu import (
    FeishuApiError,
    FeishuConfig,
    FeishuMessageSender,
    FeishuWebhook,
    parse_feishu_target,
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
    assert len(container.channels.list_outbox()) == 2


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
    assert [item.message_type for item in container.channels.list_outbox()] == [
        "issue.accepted",
        "comment.accepted",
    ]


def test_issue_acceptance_outbox_carries_reply_target_and_thread():
    container = build_memory_container()
    issue, _ = container.ingress_service.submit(replace(command(), thread_id="omt-topic-1"))

    notice = container.channels.list_outbox()[0]
    assert notice.message_type == "issue.accepted"
    assert notice.payload["reply_to"] == "message-event-1"
    assert notice.payload["reply_in_thread"] is True
    origin = container.channels.get_origin(issue.id)
    assert origin.thread_id == "omt-topic-1"


def test_comment_acceptance_outbox_replies_to_the_comment_message():
    container = build_memory_container()
    issue, _ = container.ingress_service.submit(command())
    reply = replace(
        command("event-2", "继续检查超时重试"),
        related_issue_id=issue.id,
        event_type="comment.created",
    )

    container.ingress_service.submit(reply)

    notice = container.channels.list_outbox()[1]
    assert notice.message_type == "comment.accepted"
    assert notice.payload["reply_to"] == "message-event-2"
    assert notice.payload["reply_in_thread"] is False


def test_terminal_task_event_is_projected_to_feishu_outbox():
    container = build_memory_container()
    issue, _ = container.ingress_service.submit(command())

    assert container.daemon.run_once()
    container.channel_worker.projector.run_once()

    messages = container.channels.list_outbox()
    assert [message.message_type for message in messages] == ["issue.accepted", "task.completed"]
    assert messages[1].destination == "oc-chat-1"
    assert messages[1].payload["reply_to"] == "message-event-1"
    assert messages[1].payload["reply_in_thread"] is False
    assert "任务已完成" in messages[1].payload["text"]
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
    assert [item.message_type for item in container.channels.list_outbox()] == [
        "issue.accepted",
        "task.completed",
    ]


def test_outbox_without_sender_is_released_for_retry():
    container = build_memory_container()
    container.ingress_service.submit(command())

    assert container.channel_worker.run_once() is False

    message = container.channels.list_outbox()[0]
    assert message.status == "pending"
    assert message.attempt == 1
    assert message.last_error == "channel sender is not configured"


def test_outbox_stops_retrying_after_configured_attempt_limit():
    container = build_memory_container()
    container.ingress_service.submit(command())
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
    assert parse_feishu_target(
        "@_user_1 /mica agent=geometry-agent 生成一个会议室布局", "local-assistant"
    ) == (AssigneeType.AGENT, "geometry-agent", "生成一个会议室布局")
    assert parse_feishu_target(
        "mica squad=layout-squad 生成并校验会议室布局", "local-assistant"
    ) == (AssigneeType.SQUAD, "layout-squad", "生成并校验会议室布局")


def test_feishu_sdk_verifies_and_dispatches_signed_message():
    container = build_memory_container()
    config = FeishuConfig(
        app_id="app-id",
        app_secret="app-secret",
        encrypt_key="encrypt-key",
        verification_token="verify-token",
    )
    webhook = FeishuWebhook(config, container.ingress_service)
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "feishu-event-1",
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
                "message_id": "om-message-1",
                "chat_id": "oc-chat-1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "mica 检查业务规则"}),
            },
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "nonce-1"
    signature = hashlib.sha256((timestamp + nonce + config.encrypt_key).encode() + body).hexdigest()

    status, _, content = webhook.handle(
        "http://localhost/api/integrations/feishu/events",
        {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        },
        body,
    )

    assert status == 200
    assert json.loads(content) == {"msg": "success"}
    assert len(container.issues.list()) == 1


def test_feishu_webhook_persists_topic_thread_id_on_origin():
    container = build_memory_container()
    config = FeishuConfig(
        app_id="app-id",
        app_secret="app-secret",
        encrypt_key="encrypt-key",
        verification_token="verify-token",
    )
    webhook = FeishuWebhook(config, container.ingress_service)
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "feishu-event-topic-1",
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
                "message_id": "om-topic-message-1",
                "chat_id": "oc-chat-1",
                "chat_type": "group",
                "message_type": "text",
                "thread_id": "omt-topic-9",
                "content": json.dumps({"text": "mica 检查话题消息"}),
            },
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = "nonce-1"
    signature = hashlib.sha256((timestamp + nonce + config.encrypt_key).encode() + body).hexdigest()

    status, _, _ = webhook.handle(
        "http://localhost/api/integrations/feishu/events",
        {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        },
        body,
    )

    assert status == 200
    issue = container.issues.list()[0]
    origin = container.channels.get_origin(issue.id)
    assert origin.thread_id == "omt-topic-9"
    notice = container.channels.list_outbox()[0]
    assert notice.payload["reply_to"] == "om-topic-message-1"
    assert notice.payload["reply_in_thread"] is True


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
        message_type="issue.accepted",
        payload=payload,
    )


def test_sender_replies_to_the_origin_message_when_reply_target_present():
    sender, api = _sender()
    notice = _notice(text="任务已受理", reply_to="om-origin-1", reply_in_thread=True)

    sender.send(notice)

    assert [call[0] for call in api.calls] == ["reply"]
    _, message_id, body = api.calls[0]
    assert message_id == "om-origin-1"
    assert body.reply_in_thread is True
    assert body.uuid == notice.id


def test_sender_falls_back_to_chat_level_when_reply_target_is_unusable():
    sender, api = _sender(reply_code=230011)

    sender.send(_notice(text="任务已受理", reply_to="om-recalled", reply_in_thread=False))

    assert [call[0] for call in api.calls] == ["reply", "create"]
    assert api.calls[1][2].receive_id == "oc-chat-1"


def test_sender_does_not_fall_back_on_ambiguous_reply_errors():
    sender, api = _sender(reply_code=230020)

    with pytest.raises(FeishuApiError):
        sender.send(_notice(text="任务已受理", reply_to="om-origin-1"))

    assert [call[0] for call in api.calls] == ["reply"]


def test_sender_posts_at_chat_level_without_reply_target():
    sender, api = _sender()

    sender.send(_notice(text="任务已受理"))

    assert [call[0] for call in api.calls] == ["create"]
