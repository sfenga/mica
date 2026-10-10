from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from threading import RLock
from typing import Dict, List, Optional, Tuple

from mica.domain import ChannelChatBinding, ChatMessage, ChatSession

_NO_THREAD = ""


class InMemoryChatRepository:
    """In-memory mirror of the PostgresChatRepository contract.

    `tasks` is the task repository (same store as PostgresChatRepository's
    agent_task queries): pin arbitration and the resume fallback read task
    rows to arbitrate the daemon-owned session pointer.
    """

    def __init__(self, tasks=None) -> None:
        self._sessions: Dict[str, ChatSession] = {}
        self._bindings: Dict[str, ChannelChatBinding] = {}
        self._messages: List[ChatMessage] = []
        self._reads: Dict[tuple, datetime] = {}
        self._tasks = tasks
        self._lock = RLock()

    def add_session(self, session: ChatSession) -> ChatSession:
        with self._lock:
            self._sessions.setdefault(session.id, deepcopy(session))
            return deepcopy(self._sessions[session.id])

    def list_sessions(self, workspace_id: str) -> List[ChatSession]:
        with self._lock:
            return deepcopy([s for s in self._sessions.values() if s.workspace_id == workspace_id])

    def mark_read(self, session_id: str, user_id: str, read_at: datetime) -> None:
        with self._lock:
            self._reads[(session_id, user_id)] = max(
                read_at, self._reads.get((session_id, user_id), read_at)
            )

    def read_at(self, session_id: str, user_id: str) -> Optional[datetime]:
        with self._lock:
            return self._reads.get((session_id, user_id))

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._messages = [m for m in self._messages if m.chat_session_id != session_id]
            self._bindings = {
                k: b for k, b in self._bindings.items() if b.chat_session_id != session_id
            }
            self._reads = {k: v for k, v in self._reads.items() if k[0] != session_id}
            if self._tasks is not None:
                self._tasks.delete_for_chat(session_id)

    def ensure_session(
        self,
        workspace_id: str,
        agent_id: str,
        creator_user_id: str,
        channel: str,
        tenant_key: str,
        conversation_id: str,
        thread_id: Optional[str],
    ) -> Tuple[ChatSession, bool]:
        key = (channel, tenant_key, conversation_id, thread_id or _NO_THREAD)
        with self._lock:
            binding = next(
                (item for item in self._bindings.values() if self._binding_key(item) == key),
                None,
            )
            if binding is not None:
                return deepcopy(self._sessions[binding.chat_session_id]), False
            session = ChatSession(
                workspace_id=workspace_id,
                agent_id=agent_id,
                creator_user_id=creator_user_id,
            )
            binding = ChannelChatBinding(
                channel=channel,
                tenant_key=tenant_key,
                conversation_id=conversation_id,
                thread_id=key[3],
                chat_session_id=session.id,
            )
            self._sessions[session.id] = deepcopy(session)
            self._bindings[binding.id] = deepcopy(binding)
            return deepcopy(session), True

    @staticmethod
    def _binding_key(binding: ChannelChatBinding) -> tuple:
        return (
            binding.channel,
            binding.tenant_key,
            binding.conversation_id,
            binding.thread_id or _NO_THREAD,
        )

    def get_session(self, chat_session_id: str) -> Optional[ChatSession]:
        with self._lock:
            item = self._sessions.get(chat_session_id)
            return deepcopy(item) if item else None

    def get_session_for_update(self, chat_session_id: str) -> Optional[ChatSession]:
        return self.get_session(chat_session_id)

    def find_session(
        self,
        channel: str,
        tenant_key: str,
        conversation_id: str,
        thread_id: Optional[str],
    ) -> Optional[ChatSession]:
        key = (channel, tenant_key, conversation_id, thread_id or _NO_THREAD)
        with self._lock:
            binding = next(
                (item for item in self._bindings.values() if self._binding_key(item) == key),
                None,
            )
            if binding is None:
                return None
            return deepcopy(self._sessions[binding.chat_session_id])

    def save_session(self, session: ChatSession) -> ChatSession:
        with self._lock:
            if session.id not in self._sessions:
                raise KeyError(session.id)
            self._sessions[session.id] = deepcopy(session)
            return deepcopy(session)

    def binding_for_session(self, chat_session_id: str) -> Optional[ChannelChatBinding]:
        with self._lock:
            binding = next(
                (
                    item
                    for item in self._bindings.values()
                    if item.chat_session_id == chat_session_id
                ),
                None,
            )
            return deepcopy(binding) if binding else None

    def append_message(self, message: ChatMessage) -> Tuple[ChatMessage, bool]:
        with self._lock:
            duplicate = any(
                item.chat_session_id == message.chat_session_id
                and message.external_message_id is not None
                and item.external_message_id == message.external_message_id
                for item in self._messages
            )
            if duplicate:
                return deepcopy(message), False
            self._messages.append(deepcopy(message))
            binding = self.binding_for_session(message.chat_session_id)
            if (
                binding is not None
                and message.channel_ingested
                and message.external_message_id is not None
            ):
                stored = self._bindings[binding.id]
                stored.last_message_id = message.external_message_id
                stored.last_thread_id = message.external_message_id
        return deepcopy(message), True

    def list_messages(
        self,
        chat_session_id: str,
        limit: Optional[int] = None,
        exclude_task_id: Optional[str] = None,
    ) -> List[ChatMessage]:
        with self._lock:
            items = [
                item
                for item in self._messages
                if item.chat_session_id == chat_session_id
                and item.message_kind != "channel_command"
                and (exclude_task_id is None or item.task_id != exclude_task_id)
            ]
            items.sort(key=lambda item: (item.created_at, item.id))
            if limit is not None:
                items = items[-limit:]
            return deepcopy(items)

    def message_page(self, session_id: str, before: Optional[str], limit: int):
        messages = self.list_messages(session_id)
        if before:
            index = next((i for i, m in enumerate(messages) if m.id == before), None)
            if index is None:
                raise KeyError(before)
            messages = messages[:index]
        return messages[-limit:], len(messages) > limit

    def find_message(self, session_id: str, external_id: str) -> Optional[ChatMessage]:
        with self._lock:
            return deepcopy(
                next(
                    (
                        m
                        for m in self._messages
                        if m.chat_session_id == session_id and m.external_message_id == external_id
                    ),
                    None,
                )
            )

    def message_summary(self, session_id: str, user_id: str):
        messages = self.list_messages(session_id)
        read_at = self.read_at(session_id, user_id)
        last = messages[-1] if messages else None
        return {
            "last_message": last.body if last else "",
            "last_message_at": last.created_at if last else None,
            "unread_count": sum(
                m.role == "assistant" and (read_at is None or m.created_at > read_at)
                for m in messages
            ),
        }

    def input_messages(self, chat_input_task_id: str) -> List[ChatMessage]:
        with self._lock:
            items = [item for item in self._messages if item.task_id == chat_input_task_id]
            items.sort(key=lambda item: (item.created_at, item.id))
            return deepcopy(items)

    def unowned_messages(self, chat_session_id: str) -> List[ChatMessage]:
        with self._lock:
            items = [
                item
                for item in self._messages
                if item.chat_session_id == chat_session_id
                and item.role == "user"
                and item.task_id is None
                and item.message_kind == "text"
            ]
            items.sort(key=lambda item: (item.created_at, item.id))
            return deepcopy(items)

    def seal_messages(self, chat_session_id: str, task_id: str) -> List[ChatMessage]:
        with self._lock:
            sealed: List[ChatMessage] = []
            for item in self._messages:
                if (
                    item.chat_session_id == chat_session_id
                    and item.role == "user"
                    and item.task_id is None
                    and item.message_kind == "text"
                ):
                    item.task_id = task_id
                    sealed.append(deepcopy(item))
            return sealed

    def skip_messages(self, chat_session_id: str) -> int:
        with self._lock:
            skipped = 0
            for item in self._messages:
                if (
                    item.chat_session_id == chat_session_id
                    and item.role == "user"
                    and item.task_id is None
                    and item.message_kind == "text"
                ):
                    item.task_id = f"skipped_{item.id}"
                    skipped += 1
            return skipped

    def sessions_with_unowned_messages(self) -> List[str]:
        with self._lock:
            return sorted(
                {
                    item.chat_session_id
                    for item in self._messages
                    if item.role == "user" and item.task_id is None and item.message_kind == "text"
                }
            )

    def append_assistant(self, chat_session_id: str, task_id: str, body: str) -> ChatMessage:
        message = ChatMessage(
            chat_session_id=chat_session_id, role="assistant", body=body, task_id=task_id
        )
        with self._lock:
            self._messages.append(deepcopy(message))
        return deepcopy(message)

    def pin_session(
        self,
        chat_session_id: str,
        session_id: str,
        runtime_id: str,
        work_dir: Optional[str],
        task_created_at: datetime,
        exclude_task_id: str,
    ) -> bool:
        with self._lock:
            if self._task_rows_newer(chat_session_id, task_created_at, exclude_task_id):
                return False
            session = self._sessions.get(chat_session_id)
            if session is None:
                return False
            session.session_id = session_id
            session.runtime_id = runtime_id
            if work_dir:
                session.work_dir = work_dir
            return True

    def _task_rows(self) -> List:
        if self._tasks is None:
            return []
        return self._tasks.list()

    def _task_rows_newer(
        self, chat_session_id: str, task_created_at: datetime, exclude_task_id: str
    ) -> bool:
        return any(
            item.chat_session_id == chat_session_id
            and item.id != exclude_task_id
            and item.session_id is not None
            and item.created_at > task_created_at
            for item in self._task_rows()
        )

    def last_session_pointer(self, chat_session_id: str, runtime_id: str) -> Optional[str]:
        with self._lock:
            candidates = [
                item
                for item in self._task_rows()
                if item.chat_session_id == chat_session_id
                and item.session_id is not None
                and item.runtime_id == runtime_id
            ]
            if not candidates:
                return None
            best = max(candidates, key=lambda item: (item.created_at, item.id))
            return best.session_id
