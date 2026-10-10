from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from threading import Lock
from typing import Dict, Mapping, Optional

from mica.application.chat import ChatIngressCommand, ChatIngressService
from mica.domain import OutboxMessage

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


# Media and rich-content handling at the frame-decoder boundary: text and post
# flatten to plain text, media becomes a bracketed placeholder so the user
# never sees a silently swallowed attachment. Anything else drops.
_MEDIA_PLACEHOLDERS = {
    "image": "[图片]",
    "file": "[文件]",
    "audio": "[音频]",
    "media": "[视频]",
    "video": "[视频]",
}


def _strip_noise(text: str) -> str:
    """Drop @mention placeholders and the legacy `mica` invocation prefix."""
    cleaned = re.sub(r"@_user_\d+", "", text).strip()
    return re.sub(r"^/?mica\b", "", cleaned, flags=re.IGNORECASE).strip()


def parse_feishu_target(text: str) -> tuple[Optional[tuple[str, str]], str]:
    """Split an explicit `agent=`/`squad=` prefix from an already-cleaned body."""
    match = re.match(r"(agent|squad)=([\w.-]+)\s*(.*)$", text, flags=re.DOTALL)
    if match:
        return (match.group(1), match.group(2)), match.group(3).strip()
    return None, text


def parse_issue_command(text: str) -> Optional[tuple[Optional[str], str]]:
    """`/issue <title>` with a token boundary (/issuetracker never matches);
    first non-empty line is the title, the rest the description; a bare
    command returns (None, "") so the engine can answer with usage. Takes the
    already-cleaned body."""
    if not re.match(r"^/issue(?=\s|$)", text):
        return None
    rest = text[len("/issue") :].strip()
    if not rest:
        return None, ""
    lines = [line.strip() for line in rest.splitlines() if line.strip()]
    title = lines[0]
    description = "\n".join(lines[1:]).strip() or title
    return title, description


def _flatten_content(message) -> Optional[str]:
    message_type = getattr(message, "message_type", "")
    try:
        content = json.loads(message.content or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(content, dict):
        return None
    if message_type == "text":
        text = str(content.get("text", "")).strip()
        return text or None
    if message_type == "post":
        flattened = _flatten_post(content)
        return flattened or None
    if message_type in _MEDIA_PLACEHOLDERS:
        return _MEDIA_PLACEHOLDERS[message_type]
    return None


def _flatten_post(content: dict) -> str:
    lines: list[str] = []
    title = content.get("title")
    if title:
        lines.append(str(title))
    for paragraph in content.get("content") or []:
        parts: list[str] = []
        for element in paragraph or []:
            tag = element.get("tag")
            if tag == "text":
                parts.append(str(element.get("text", "")))
            elif tag == "a":
                label = str(element.get("text", "")).strip()
                href = str(element.get("href", "")).strip()
                parts.append(f"{label} ({href})" if label and href else label or href)
            elif tag == "at":
                parts.append(f"@{element.get('user_name') or element.get('user_id') or 'user'}")
            elif tag == "img":
                parts.append("[图片]")
            elif tag == "media":
                parts.append("[视频]")
            elif tag == "emotion":
                parts.append(str(element.get("emoji_type", "")))
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


class FeishuWebhook:
    """Frame decoder + adapter: translates Lark events into engine commands.

    Business routing (sessions, bindings, dedup, commands) lives entirely in
    ChatIngressService — this class only decodes frames."""

    def __init__(
        self,
        config: FeishuConfig,
        chat_ingress: ChatIngressService,
        receipt: Optional["FeishuTypingReceipt"] = None,
    ) -> None:
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

        self.config = config
        self.chat_ingress = chat_ingress
        self.receipt = receipt
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
        if not message.message_id or not message.chat_id:
            return
        body = _flatten_content(message)
        if not body:
            return
        chat_type = getattr(message, "chat_type", "") or ""
        mentions = getattr(message, "mentions", None) or []
        # Defensive group gate: the platform only pushes @bot group messages by
        # default, but never route a group event that carries no mention.
        if chat_type == "group" and not mentions:
            return
        external_user_id = sender.sender_id.open_id or sender.sender_id.user_id
        if not external_user_id:
            return
        tenant_key = header.tenant_key or sender.tenant_key or "unknown"
        cleaned_body = _strip_noise(body)
        command = ChatIngressCommand(
            channel="feishu",
            tenant_key=tenant_key,
            workspace_id=self.config.workspace_id or f"feishu:{tenant_key}",
            default_agent_id=self.config.default_agent_id,
            external_message_id=message.message_id,
            external_user_id=external_user_id,
            display_name=external_user_id,
            conversation_id=message.chat_id,
            thread_id=getattr(message, "thread_id", None) or None,
            chat_type=chat_type or "p2p",
            body=cleaned_body,
        )
        issue_command = parse_issue_command(cleaned_body)
        if issue_command is not None:
            title, description = issue_command
            self.chat_ingress.submit_issue_command(command, title, description)
            return
        explicit_target, message_body = parse_feishu_target(cleaned_body)
        if not message_body:
            return
        command = replace(command, body=message_body, explicit_target=explicit_target)
        if self.chat_ingress.submit(command) is not None and self.receipt is not None:
            self.receipt.add(message.message_id)


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


class FeishuTypingReceipt:
    """Best-effort processing receipt: a Typing emoji reaction on the trigger
    message, cleared when the reply goes out. Cosmetic by design — every Lark
    failure degrades to a logged no-op and the missing permission scope only
    means no receipt, never a broken flow."""

    EMOJI_TYPE = "Typing"

    def __init__(self, config: FeishuConfig) -> None:
        self._config = config
        self._client = None
        self._reaction_ids: Dict[str, str] = {}
        self._lock = Lock()
        self._unavailable = False

    def _lark_client(self):
        if self._unavailable:
            return None
        if self._client is None:
            try:
                import lark_oapi as lark

                self._client = (
                    lark.Client.builder()
                    .app_id(self._config.app_id)
                    .app_secret(self._config.app_secret)
                    .build()
                )
            except Exception as exc:
                logger.warning("typing receipt disabled: lark client unavailable (%s)", exc)
                self._unavailable = True
        return self._client

    def add(self, message_id: str) -> None:
        client = self._lark_client()
        if client is None:
            return
        try:
            from lark_oapi.api.im.v1.model.create_message_reaction_request import (
                CreateMessageReactionRequest,
            )
            from lark_oapi.api.im.v1.model.create_message_reaction_request_body import (
                CreateMessageReactionRequestBody,
            )
            from lark_oapi.api.im.v1.model.emoji import Emoji

            body = (
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(self.EMOJI_TYPE).build())
                .build()
            )
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            response = client.im.v1.message_reaction.create(request)
            if response.success():
                reaction_id = (response.data.reaction_id if response.data else None) or None
                if reaction_id:
                    with self._lock:
                        self._reaction_ids[message_id] = reaction_id
            else:
                logger.warning(
                    "typing receipt add failed for %s: %s", message_id, getattr(response, "msg", "")
                )
        except Exception as exc:
            logger.warning("typing receipt add failed for %s: %s", message_id, exc)

    def clear(self, message_id: str) -> None:
        with self._lock:
            reaction_id = self._reaction_ids.pop(message_id, None)
        if not reaction_id:
            return
        client = self._lark_client()
        if client is None:
            return
        try:
            from lark_oapi.api.im.v1.model.delete_message_reaction_request import (
                DeleteMessageReactionRequest,
            )

            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            response = client.im.v1.message_reaction.delete(request)
            if not response.success():
                logger.warning(
                    "typing receipt clear failed for %s: %s",
                    message_id,
                    getattr(response, "msg", ""),
                )
        except Exception as exc:
            logger.warning("typing receipt clear failed for %s: %s", message_id, exc)


class FeishuMessageSender:
    def __init__(self, config: FeishuConfig, receipt: Optional[FeishuTypingReceipt] = None) -> None:
        import lark_oapi as lark

        self._receipt = receipt
        self._client = (
            lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret).build()
        )

    def send(self, message: OutboxMessage) -> None:
        reply_to = str(message.payload.get("reply_to") or "")
        if self._receipt is not None and reply_to:
            self._receipt.clear(reply_to)
        text = str(message.payload.get("text", ""))
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
