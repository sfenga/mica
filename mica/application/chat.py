"""Channel-agnostic chat engine: ingress routing, debounce batching, prompts.

Mirrors Multica's `channel/engine` layer — this module knows about sessions,
bindings and tasks but never about a specific messenger. Adapters (feishu.py)
translate wire events into `ChatIngressCommand`; repositories own the SQL.
"""

from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from dataclasses import dataclass
from threading import Lock, Timer
from typing import Callable, Dict, List, Optional, Tuple

from mica.domain import (
    AssigneeType,
    ChannelChatBinding,
    ChatMessage,
    ChatSession,
    IssueOrigin,
    Task,
)

from .ports import AgentRepository, ChannelRepository, ChatRepository, TransactionManager
from .services import TaskService, TriggerService

logger = logging.getLogger(__name__)

CHAT_BATCH_WINDOW_SECONDS = 3.0
CHAT_TRANSCRIPT_LIMIT = 20
GROUP_OWNER_EXTERNAL_ID = "system:group-owner"
GROUP_OWNER_DISPLAY_NAME = "Feishu 群会话"


def chat_app_url() -> str:
    configured = os.environ.get("MICA_API_URL")
    if configured:
        return configured.rstrip("/") + "/app"
    host = os.environ.get("MICA_API_HOST", "127.0.0.1")
    port = os.environ.get("MICA_API_PORT", "8000")
    return f"http://{host}:{port}/app"


@dataclass(frozen=True)
class ChatIngressCommand:
    """One adapter-translated inbound chat message (no messenger specifics)."""

    channel: str
    tenant_key: str
    workspace_id: str
    default_agent_id: str
    external_message_id: str
    external_user_id: str
    display_name: str
    conversation_id: str
    thread_id: Optional[str]
    chat_type: str  # "p2p" | "group"
    body: str
    # ("agent", id) parsed from an explicit prefix; squad targets are passed
    # through so the engine can guide them to /issue.
    explicit_target: Optional[Tuple[str, str]] = None
    parent_external_message_id: Optional[str] = None
    message_kind: str = "text"


class ChatIngressService:
    """Routing pipeline: ensure session → append + dedup → command → flush."""

    def __init__(
        self,
        chats: ChatRepository,
        channels: ChannelRepository,
        agents: AgentRepository,
        triggers: TriggerService,
        tasks: TaskService,
        transactions: Optional[TransactionManager] = None,
    ) -> None:
        self.chats = chats
        self.channels = channels
        self.agents = agents
        self.triggers = triggers
        self.tasks = tasks
        self.transactions = transactions
        self.batcher: Optional["ChatBatcher"] = None
        self._memory_lock = Lock()

    def bind_batcher(self, batcher: "ChatBatcher") -> None:
        self.batcher = batcher

    def _transaction(self):
        return self.transactions.transaction() if self.transactions is not None else nullcontext()

    def _lock(self):
        return nullcontext() if self.transactions is not None else self._memory_lock

    def submit(self, command: ChatIngressCommand) -> Optional[str]:
        """Ingest one message; returns the session id, or None when the event
        was dropped (duplicate / fail-closed guidance)."""
        explicit_agent_id: Optional[str] = None
        if command.explicit_target is not None:
            kind, target_id = command.explicit_target
            if kind == "squad":
                if (
                    self.chats.find_session(
                        command.channel,
                        command.tenant_key,
                        command.conversation_id,
                        command.thread_id,
                    )
                    is None
                ):
                    self._reply(
                        command,
                        "Squad 不进入 Chat 会话。要委派 Squad 请使用 /issue squad="
                        f"{target_id} 建单；要在群里与单个 Agent 对话，请在新话题的首条消息用 agent= 指定。",
                    )
                    return None
            else:
                if self.agents.get(target_id) is None:
                    self._reply(
                        command,
                        f"未找到 Agent：{target_id}。本轮未执行；请检查 agent= 指定后重发。",
                    )
                    return None
                explicit_agent_id = target_id

        with self._lock(), self._transaction():
            user, _ = self.channels.resolve_identity(
                command.channel,
                command.tenant_key,
                GROUP_OWNER_EXTERNAL_ID
                if command.chat_type == "group"
                else command.external_user_id,
                command.workspace_id,
                GROUP_OWNER_DISPLAY_NAME if command.chat_type == "group" else command.display_name,
            )
            session, created = self.chats.ensure_session(
                workspace_id=command.workspace_id,
                agent_id=explicit_agent_id or command.default_agent_id,
                creator_user_id=user.id,
                channel=command.channel,
                tenant_key=command.tenant_key,
                conversation_id=command.conversation_id,
                thread_id=command.thread_id,
            )
            message, stored = self.chats.append_message(
                ChatMessage(
                    chat_session_id=session.id,
                    role="user",
                    body=command.body,
                    external_message_id=command.external_message_id,
                    parent_external_message_id=command.parent_external_message_id,
                    message_kind=command.message_kind,
                    channel_ingested=True,
                )
            )
            if not stored:
                return None
            if created and not session.title:
                session.title = command.body.splitlines()[0].strip()[:80] if command.body else ""
                self.chats.save_session(session)
        if not created and command.explicit_target is not None:
            self._reply(
                command,
                f"当前会话已绑定 Agent：{session.agent_id}，本轮仍由它处理。要与其他 Agent 协作："
                "在群里开新话题（话题即独立会话），或使用 /issue agent=... 建单。",
            )
        self._schedule(session.id)
        return session.id

    def submit_issue_command(
        self,
        command: ChatIngressCommand,
        title: Optional[str],
        description: str,
    ) -> None:
        """`/issue` turn: durable but invisible to agent input, synchronous
        issue creation — the command is terminal, never a chat run."""
        usage = "用法：/issue <标题>\n标题会作为看板卡片标题，其余行作为描述。"
        with self._lock(), self._transaction():
            user, _ = self.channels.resolve_identity(
                command.channel,
                command.tenant_key,
                GROUP_OWNER_EXTERNAL_ID
                if command.chat_type == "group"
                else command.external_user_id,
                command.workspace_id,
                GROUP_OWNER_DISPLAY_NAME if command.chat_type == "group" else command.display_name,
            )
            session, _ = self.chats.ensure_session(
                workspace_id=command.workspace_id,
                agent_id=command.default_agent_id,
                creator_user_id=user.id,
                channel=command.channel,
                tenant_key=command.tenant_key,
                conversation_id=command.conversation_id,
                thread_id=command.thread_id,
            )
            message, stored = self.chats.append_message(
                ChatMessage(
                    chat_session_id=session.id,
                    role="user",
                    body=command.body,
                    external_message_id=command.external_message_id,
                    parent_external_message_id=command.parent_external_message_id,
                    message_kind="channel_command",
                    channel_ingested=True,
                )
            )
            if not stored:
                return
            if not title:
                self._reply(command, usage)
                return
            issue = self.triggers.create_issue(
                title=title,
                body=description,
                workspace_id=command.workspace_id,
                assignee_id=session.agent_id,
                assignee_type=AssigneeType.AGENT,
                created_by_user_id=user.id,
            )
            self.channels.add_origin(
                IssueOrigin(
                    issue_id=issue.id,
                    channel=command.channel,
                    tenant_key=command.tenant_key,
                    conversation_id=command.conversation_id,
                    external_message_id=command.external_message_id,
                    thread_id=command.thread_id,
                )
            )
        self._reply(
            command,
            f"已创建 {issue.id} — {issue.title}\n{chat_app_url()}",
        )

    def flush_session(self, chat_session_id: str) -> Optional[Task]:
        """Seal the session's unowned user messages into exactly one task."""
        with self._lock(), self._transaction():
            session = self.chats.get_session_for_update(chat_session_id)
            if session is None or session.status != "active":
                return None
            unowned = self.chats.unowned_messages(chat_session_id)
            if not unowned:
                return None
            agent = self.agents.get(session.agent_id)
            if agent is None:
                # The bound agent disappeared: consume the batch so recovery
                # cannot loop, and tell the user instead of failing silently.
                self.chats.skip_messages(chat_session_id)
                binding = self.chats.binding_for_session(chat_session_id)
                self._reply_via_binding(
                    binding,
                    unowned[-1].external_message_id,
                    f"会话绑定的 Agent {session.agent_id} 已不存在，本轮消息未执行。",
                )
                return None
            binding = self.chats.binding_for_session(chat_session_id)
            task = Task(
                issue_id=None,
                agent_id=agent.id,
                runtime_id=agent.runtime_id,
                trigger_type="chat.message",
                trigger_id=chat_session_id,
                chat_session_id=chat_session_id,
                reply_to_message_id=unowned[-1].external_message_id,
                reply_in_thread=bool(binding and binding.thread_id),
            )
            task.chat_input_task_id = task.id
            # TaskService stays issue-only; the chat enqueue is a bare repo add
            # inside this transaction, with the event recorded after commit.
            self.tasks.tasks.add(task)
            self.chats.seal_messages(chat_session_id, task.id)
        self.tasks.record(
            task.id,
            "task.queued",
            {"chat_session_id": chat_session_id, "runtime_id": agent.runtime_id},
        )
        return task

    def recover_pending(self) -> None:
        """Re-arm debounce windows for batches a crash left unflushed."""
        if self.batcher is None:
            return
        for session_id in self.chats.sessions_with_unowned_messages():
            self.batcher.schedule(session_id)

    def _reply(self, command: ChatIngressCommand, text: str) -> None:
        self.channels.add_outbox(_outbox_reply(command, text))

    def _reply_via_binding(
        self,
        binding: Optional[ChannelChatBinding],
        reply_to: Optional[str],
        text: str,
    ) -> None:
        if binding is None:
            logger.warning("chat guidance dropped: no binding for session")
            return
        from mica.domain import OutboxMessage

        self.channels.add_outbox(
            OutboxMessage(
                channel=binding.channel,
                destination=binding.conversation_id,
                source_event_id=reply_to or binding.id,
                message_type="chat.notice",
                payload={
                    "text": text,
                    "reply_to": reply_to,
                    "reply_in_thread": bool(binding.thread_id),
                },
            )
        )

    def _schedule(self, session_id: str) -> None:
        if self.batcher is not None:
            self.batcher.schedule(session_id)


def _outbox_reply(command: ChatIngressCommand, text: str):
    from mica.domain import OutboxMessage

    return OutboxMessage(
        channel=command.channel,
        destination=command.conversation_id,
        source_event_id=command.external_message_id,
        message_type="chat.notice",
        payload={
            "text": text,
            "reply_to": command.external_message_id,
            "reply_in_thread": bool(command.thread_id),
        },
    )


class ChatBatcher:
    """Debounce window that gates ONLY the run trigger; messages are already
    durable when schedule() is called. Latest window per session wins."""

    def __init__(self, flush: Callable[[str], None], window_seconds: float = 3.0) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._flush = flush
        self._window = window_seconds
        self._timers: Dict[str, Timer] = {}
        self._lock = Lock()

    def schedule(self, chat_session_id: str) -> None:
        with self._lock:
            previous = self._timers.pop(chat_session_id, None)
            if previous is not None:
                previous.cancel()
            timer = Timer(self._window, self._fire, args=(chat_session_id,))
            timer.daemon = True
            self._timers[chat_session_id] = timer
        timer.start()

    def _fire(self, chat_session_id: str) -> None:
        with self._lock:
            self._timers.pop(chat_session_id, None)
        self._run_flush(chat_session_id)

    def drain(self) -> List[str]:
        """Flush every pending window immediately (graceful shutdown)."""
        with self._lock:
            timers = list(self._timers.items())
            self._timers.clear()
        for _, timer in timers:
            timer.cancel()
        session_ids = [session_id for session_id, _ in timers]
        for session_id in session_ids:
            self._run_flush(session_id)
        return session_ids

    def pending_sessions(self) -> List[str]:
        with self._lock:
            return sorted(self._timers)

    def _run_flush(self, chat_session_id: str) -> None:
        try:
            self._flush(chat_session_id)
        except Exception:
            # Messages stay durable and unowned; startup recovery re-arms.
            logger.exception("chat batch flush failed", extra={"chat_session_id": chat_session_id})


def build_chat_prompt(
    session: ChatSession, transcript: List[ChatMessage], batch: List[ChatMessage]
) -> str:
    """Daemon-side brief: recent transcript plus the sealed input batch."""
    sections: List[str] = []
    if session.title:
        sections.append(f"会话：{session.title}")
    if transcript:
        lines = [f"{'用户' if item.role == 'user' else '助手'}：{item.body}" for item in transcript]
        sections.append("## 对话记录\n" + "\n".join(lines))
    batch_lines = [f"[{index}] {item.body}" for index, item in enumerate(batch, 1)]
    sections.append("## 本轮用户消息\n" + "\n".join(batch_lines))
    sections.append("请基于本轮用户消息回复；对话记录仅作为背景。")
    return "\n\n".join(sections)


def resolve_chat_resume(chats: ChatRepository, session: ChatSession, task: Task) -> Optional[str]:
    """Three-level fallback: session pointer (runtime-matched) → newest task
    row that recorded a session → fresh. A failed turn never costs the
    conversation its memory."""
    if session.session_id and session.runtime_id == task.runtime_id:
        return session.session_id
    return chats.last_session_pointer(session.id, task.runtime_id)
