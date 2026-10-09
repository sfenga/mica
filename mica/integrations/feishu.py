from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Mapping, Optional

from mica.application.channels import IngressCommand, IngressService
from mica.domain import AssigneeType, OutboxMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    encrypt_key: str
    verification_token: str
    default_agent_id: str = "local-assistant"
    max_clock_skew_seconds: int = 300
    workspace_id: Optional[str] = None


class FeishuWebhook:
    def __init__(self, config: FeishuConfig, ingress: IngressService) -> None:
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

        self.config = config
        self.ingress = ingress
        self._dispatcher = (
            EventDispatcherHandler.builder(config.encrypt_key, config.verification_token)
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )

    def handle(
        self, uri: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        from lark_oapi.core.model import RawRequest

        normalized = {key: value for key, value in headers.items()}
        timestamp = normalized.get("X-Lark-Request-Timestamp") or normalized.get(
            "x-lark-request-timestamp"
        )
        if timestamp:
            try:
                if abs(time.time() - int(timestamp)) > self.config.max_clock_skew_seconds:
                    return 401, {"content-type": "application/json"}, b'{"msg":"stale event"}'
            except ValueError:
                return 401, {"content-type": "application/json"}, b'{"msg":"invalid timestamp"}'
        request = RawRequest()
        request.uri = uri
        # SDK constant lookups use canonical header casing.
        request.headers = {key.title(): value for key, value in normalized.items()}
        request.body = body
        response = self._dispatcher.do(request)
        return response.status_code or 500, response.headers, response.content or b""

    def _on_message(self, callback) -> None:
        header = callback.header
        event = callback.event
        if not header or not event:
            return
        message = event.message
        sender = event.sender
        if not message or not sender or not sender.sender_id:
            return
        if message.message_type != "text":
            return
        external_user_id = sender.sender_id.open_id or sender.sender_id.user_id
        if not external_user_id or not message.message_id or not message.chat_id:
            return
        try:
            content = json.loads(message.content or "{}")
            text = content.get("text", "")
        except (TypeError, json.JSONDecodeError):
            return
        assignee_type, assignee_id, body = parse_feishu_target(text, self.config.default_agent_id)
        if not body:
            return
        tenant_key = header.tenant_key or sender.tenant_key or "unknown"
        thread_id = getattr(message, "thread_id", None) or None
        related_origin = None
        for related_message_id in (
            getattr(message, "parent_id", None),
            getattr(message, "root_id", None),
        ):
            if related_message_id:
                related_origin = self.ingress.channels.find_origin(
                    "feishu", tenant_key, related_message_id
                )
                if related_origin:
                    break
        title = next((line.strip() for line in body.splitlines() if line.strip()), "飞书任务")[:200]
        self.ingress.submit(
            IngressCommand(
                channel="feishu",
                tenant_key=tenant_key,
                external_event_id=header.event_id or message.message_id,
                external_user_id=external_user_id,
                display_name=external_user_id,
                conversation_id=message.chat_id,
                external_message_id=message.message_id,
                title=title,
                body=body,
                workspace_id=self.config.workspace_id or f"feishu:{tenant_key}",
                assignee_id=assignee_id,
                assignee_type=assignee_type,
                event_type=header.event_type or "im.message.receive_v1",
                related_issue_id=related_origin.issue_id if related_origin else None,
                thread_id=thread_id,
            )
        )


def parse_feishu_target(text: str, default_agent_id: str) -> tuple[AssigneeType, str, str]:
    cleaned = re.sub(r"@_user_\d+", "", text).strip()
    cleaned = re.sub(r"^/?mica\b", "", cleaned, flags=re.IGNORECASE).strip()
    match = re.match(r"(agent|squad)=([\w.-]+)\s*(.*)$", cleaned, flags=re.DOTALL)
    if match:
        return AssigneeType(match.group(1)), match.group(2), match.group(3).strip()
    return AssigneeType.AGENT, default_agent_id, cleaned


class FeishuApiError(RuntimeError):
    """Carries the Lark business code so the sender can classify failures."""

    def __init__(self, code: int, msg: str, request_id: str) -> None:
        super().__init__(
            f"Feishu message API failed: code={code}, msg={msg}, request_id={request_id}"
        )
        self.code = code


# Reply-endpoint business codes that mean "this trigger message/topic cannot
# receive a reply and nothing was sent": message recalled (230011), topic gone
# (230019), message invisible to the operator (230050), group does not support
# reply-in-thread (230071), aggregated message (230072), self-destructing
# message (230111). Only these justify a chat-level fallback; rate limits, 5xx
# and ambiguous errors stay failures so the outbox retry never duplicates a
# reply or leaks a thread-only reply into the main group chat.
NON_REPLYABLE_CODES = frozenset({230011, 230019, 230050, 230071, 230072, 230111})


class FeishuMessageSender:
    def __init__(self, config: FeishuConfig) -> None:
        import lark_oapi as lark

        self._client = (
            lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret).build()
        )

    def send(self, message: OutboxMessage) -> None:
        text = str(message.payload.get("text", ""))
        reply_to = str(message.payload.get("reply_to") or "")
        if reply_to:
            try:
                self._reply(
                    reply_to,
                    text,
                    bool(message.payload.get("reply_in_thread")),
                    message.id,
                )
                return
            except FeishuApiError as exc:
                if exc.code not in NON_REPLYABLE_CODES:
                    raise
                logger.warning(
                    "feishu reply target %s unusable (%s); falling back to chat-level send",
                    reply_to,
                    exc,
                )
        self._create(message.destination, text, message.id)

    def _create(self, chat_id: str, text: str, uuid: str) -> None:
        from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
        from lark_oapi.api.im.v1.model.create_message_request_body import CreateMessageRequestBody

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .uuid(uuid)
            .build()
        )
        request = (
            CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        )
        self._check(self._client.im.v1.message.create(request))

    def _reply(self, message_id: str, text: str, in_thread: bool, uuid: str) -> None:
        from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
        from lark_oapi.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody

        body = (
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .uuid(uuid)
            .reply_in_thread(in_thread)
            .build()
        )
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        self._check(self._client.im.v1.message.reply(request))

    @staticmethod
    def _check(response) -> None:
        if response.success():
            return
        request_id = response.get_log_id() if hasattr(response, "get_log_id") else "unknown"
        raise FeishuApiError(response.code, response.msg, request_id)


def config_from_env(environ: Mapping[str, str]) -> Optional[FeishuConfig]:
    required = [
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_ENCRYPT_KEY",
        "FEISHU_VERIFICATION_TOKEN",
    ]
    if not all(environ.get(key) for key in required):
        return None
    return FeishuConfig(
        app_id=environ["FEISHU_APP_ID"],
        app_secret=environ["FEISHU_APP_SECRET"],
        encrypt_key=environ["FEISHU_ENCRYPT_KEY"],
        verification_token=environ["FEISHU_VERIFICATION_TOKEN"],
        default_agent_id=environ.get("FEISHU_DEFAULT_AGENT_ID", "local-assistant"),
        max_clock_skew_seconds=int(environ.get("FEISHU_MAX_CLOCK_SKEW_SECONDS", "300")),
        workspace_id=environ.get("FEISHU_WORKSPACE_ID") or None,
    )
