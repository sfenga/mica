from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from mica.domain import ChannelChatBinding, ChatMessage, ChatSession

from .connection import PostgresDatabase

CHAT_SESSION_COLUMNS = """
id, workspace_id, title, status, agent_id, creator_user_id,
session_id, runtime_id, work_dir, created_at, updated_at
"""

CHAT_BINDING_COLUMNS = """
id, channel, tenant_key, conversation_id, thread_id, chat_session_id,
last_message_id, last_thread_id, created_at, updated_at
"""

CHAT_MESSAGE_COLUMNS = """
id, chat_session_id, role, body, external_message_id, parent_external_message_id,
task_id, message_kind, channel_ingested, created_at
"""

# '' (not NULL) is the non-topic sentinel inside the binding key so the
# composite UNIQUE constraint is total.
_NO_THREAD = ""


def _session(row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        workspace_id=row["workspace_id"],
        agent_id=row["agent_id"],
        creator_user_id=row["creator_user_id"],
        title=row["title"],
        status=row["status"],
        session_id=row["session_id"],
        runtime_id=row["runtime_id"],
        work_dir=row["work_dir"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _binding(row) -> ChannelChatBinding:
    return ChannelChatBinding(
        id=row["id"],
        channel=row["channel"],
        tenant_key=row["tenant_key"],
        conversation_id=row["conversation_id"],
        thread_id=row["thread_id"] or None,
        chat_session_id=row["chat_session_id"],
        last_message_id=row["last_message_id"],
        last_thread_id=row["last_thread_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _message(row) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        chat_session_id=row["chat_session_id"],
        role=row["role"],
        body=row["body"],
        external_message_id=row["external_message_id"],
        parent_external_message_id=row["parent_external_message_id"],
        task_id=row["task_id"],
        message_kind=row["message_kind"],
        channel_ingested=row["channel_ingested"],
        created_at=row["created_at"],
    )


class PostgresChatRepository:
    """All chat SQL in one place — the engine's only view of conversation state."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

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
        key_thread = thread_id or _NO_THREAD
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT s.id AS session_id FROM channel_chat_binding AS b
                JOIN chat_session AS s ON s.id = b.chat_session_id
                WHERE b.channel=%s AND b.tenant_key=%s AND b.conversation_id=%s AND b.thread_id=%s""",
                (channel, tenant_key, conversation_id, key_thread),
            ).fetchone()
            if row is not None:
                return self.get_session(row["session_id"]), False
            session = ChatSession(
                workspace_id=workspace_id,
                agent_id=agent_id,
                creator_user_id=creator_user_id,
            )
            connection.execute(
                f"""INSERT INTO chat_session ({CHAT_SESSION_COLUMNS})
                VALUES ({", ".join(["%s"] * 11)})""",
                (
                    session.id,
                    session.workspace_id,
                    session.title,
                    session.status,
                    session.agent_id,
                    session.creator_user_id,
                    session.session_id,
                    session.runtime_id,
                    session.work_dir,
                    session.created_at,
                    session.updated_at,
                ),
            )
            binding = ChannelChatBinding(
                channel=channel,
                tenant_key=tenant_key,
                conversation_id=conversation_id,
                thread_id=key_thread,
                chat_session_id=session.id,
            )
            inserted = connection.execute(
                f"""INSERT INTO channel_chat_binding ({CHAT_BINDING_COLUMNS})
                VALUES ({", ".join(["%s"] * 10)})
                ON CONFLICT (channel, tenant_key, conversation_id, thread_id) DO NOTHING
                RETURNING id""",
                (
                    binding.id,
                    binding.channel,
                    binding.tenant_key,
                    binding.conversation_id,
                    binding.thread_id,
                    binding.chat_session_id,
                    binding.last_message_id,
                    binding.last_thread_id,
                    binding.created_at,
                    binding.updated_at,
                ),
            ).fetchone()
            if inserted is None:
                # Two first messages raced: the binding belongs to the winner.
                # Discard the just-inserted loser pair (same transaction, never
                # committed) and re-read the winner's session.
                connection.execute("DELETE FROM chat_session WHERE id=%s", (session.id,))
                winner = connection.execute(
                    """SELECT chat_session_id FROM channel_chat_binding
                    WHERE channel=%s AND tenant_key=%s AND conversation_id=%s AND thread_id=%s""",
                    (channel, tenant_key, conversation_id, key_thread),
                ).fetchone()
                return self.get_session(winner["chat_session_id"]), False
        return session, True

    def get_session(self, chat_session_id: str) -> Optional[ChatSession]:
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT {CHAT_SESSION_COLUMNS} FROM chat_session WHERE id=%s",
                (chat_session_id,),
            ).fetchone()
        return _session(row) if row else None

    def get_session_for_update(self, chat_session_id: str) -> Optional[ChatSession]:
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT {CHAT_SESSION_COLUMNS} FROM chat_session WHERE id=%s FOR UPDATE",
                (chat_session_id,),
            ).fetchone()
        return _session(row) if row else None

    def find_session(
        self,
        channel: str,
        tenant_key: str,
        conversation_id: str,
        thread_id: Optional[str],
    ) -> Optional[ChatSession]:
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT s.id AS session_id FROM channel_chat_binding AS b
                JOIN chat_session AS s ON s.id = b.chat_session_id
                WHERE b.channel=%s AND b.tenant_key=%s AND b.conversation_id=%s AND b.thread_id=%s""",
                (channel, tenant_key, conversation_id, thread_id or _NO_THREAD),
            ).fetchone()
        return self.get_session(row["session_id"]) if row else None

    def save_session(self, session: ChatSession) -> ChatSession:
        with self.database.connection() as connection:
            connection.execute(
                """UPDATE chat_session SET title=%s, status=%s, session_id=%s,
                runtime_id=%s, work_dir=%s, updated_at=%s WHERE id=%s""",
                (
                    session.title,
                    session.status,
                    session.session_id,
                    session.runtime_id,
                    session.work_dir,
                    session.updated_at,
                    session.id,
                ),
            )
        return session

    def binding_for_session(self, chat_session_id: str) -> Optional[ChannelChatBinding]:
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT {CHAT_BINDING_COLUMNS} FROM channel_chat_binding WHERE chat_session_id=%s",
                (chat_session_id,),
            ).fetchone()
        return _binding(row) if row else None

    def append_message(self, message: ChatMessage) -> Tuple[ChatMessage, bool]:
        with self.database.connection() as connection:
            row = connection.execute(
                f"""INSERT INTO chat_message ({CHAT_MESSAGE_COLUMNS})
                VALUES ({", ".join(["%s"] * 10)})
                ON CONFLICT (chat_session_id, external_message_id) DO NOTHING
                RETURNING {CHAT_MESSAGE_COLUMNS}""",
                (
                    message.id,
                    message.chat_session_id,
                    message.role,
                    message.body,
                    message.external_message_id,
                    message.parent_external_message_id,
                    message.task_id,
                    message.message_kind,
                    message.channel_ingested,
                    message.created_at,
                ),
            ).fetchone()
            if row is None:
                return message, False
            if message.external_message_id is not None:
                connection.execute(
                    """UPDATE channel_chat_binding SET last_message_id=%s, last_thread_id=%s,
                    updated_at=now() WHERE chat_session_id=%s""",
                    (
                        message.external_message_id,
                        message.external_message_id,
                        message.chat_session_id,
                    ),
                )
        return message, True

    def list_messages(
        self,
        chat_session_id: str,
        limit: Optional[int] = None,
        exclude_task_id: Optional[str] = None,
    ) -> List[ChatMessage]:
        query = f"""SELECT {CHAT_MESSAGE_COLUMNS} FROM chat_message
        WHERE chat_session_id=%s AND message_kind <> 'channel_command'"""
        params: list = [chat_session_id]
        if exclude_task_id is not None:
            query += " AND (task_id IS NULL OR task_id <> %s)"
            params.append(exclude_task_id)
        query += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [_message(row) for row in reversed(rows)]

    def input_messages(self, chat_input_task_id: str) -> List[ChatMessage]:
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT {CHAT_MESSAGE_COLUMNS} FROM chat_message
                WHERE task_id=%s ORDER BY created_at, id""",
                (chat_input_task_id,),
            ).fetchall()
        return [_message(row) for row in rows]

    def unowned_messages(self, chat_session_id: str) -> List[ChatMessage]:
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""SELECT {CHAT_MESSAGE_COLUMNS} FROM chat_message
                WHERE chat_session_id=%s AND role='user' AND task_id IS NULL
                    AND message_kind='text'
                ORDER BY created_at, id""",
                (chat_session_id,),
            ).fetchall()
        return [_message(row) for row in rows]

    def seal_messages(self, chat_session_id: str, task_id: str) -> List[ChatMessage]:
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""UPDATE chat_message SET task_id=%s
                WHERE chat_session_id=%s AND role='user' AND task_id IS NULL
                    AND message_kind='text'
                RETURNING {CHAT_MESSAGE_COLUMNS}""",
                (task_id, chat_session_id),
            ).fetchall()
        return [_message(row) for row in rows]

    def skip_messages(self, chat_session_id: str) -> int:
        with self.database.connection() as connection:
            result = connection.execute(
                """UPDATE chat_message SET task_id='skipped_' || id
                WHERE chat_session_id=%s AND role='user' AND task_id IS NULL
                    AND message_kind='text'""",
                (chat_session_id,),
            )
        return result.rowcount

    def sessions_with_unowned_messages(self) -> List[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT chat_session_id FROM chat_message
                WHERE role='user' AND task_id IS NULL AND message_kind='text'"""
            ).fetchall()
        return [row["chat_session_id"] for row in rows]

    def append_assistant(self, chat_session_id: str, task_id: str, body: str) -> ChatMessage:
        message = ChatMessage(
            chat_session_id=chat_session_id, role="assistant", body=body, task_id=task_id
        )
        with self.database.connection() as connection:
            connection.execute(
                f"""INSERT INTO chat_message ({CHAT_MESSAGE_COLUMNS})
                VALUES ({", ".join(["%s"] * 10)})""",
                (
                    message.id,
                    message.chat_session_id,
                    message.role,
                    message.body,
                    message.external_message_id,
                    message.parent_external_message_id,
                    message.task_id,
                    message.message_kind,
                    message.channel_ingested,
                    message.created_at,
                ),
            )
        return message

    def pin_session(
        self,
        chat_session_id: str,
        session_id: str,
        runtime_id: str,
        work_dir: Optional[str],
        task_created_at: datetime,
        exclude_task_id: str,
    ) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                """UPDATE chat_session SET session_id=%s, runtime_id=%s,
                work_dir=COALESCE(%s, work_dir), updated_at=now()
                WHERE id=%s AND NOT EXISTS (
                    SELECT 1 FROM agent_task newer
                    WHERE newer.chat_session_id=chat_session.id
                        AND newer.id <> %s
                        AND newer.session_id IS NOT NULL
                        AND newer.created_at > %s
                ) RETURNING id""",
                (
                    session_id,
                    runtime_id,
                    work_dir,
                    chat_session_id,
                    exclude_task_id,
                    task_created_at,
                ),
            ).fetchone()
        return row is not None

    def last_session_pointer(self, chat_session_id: str, runtime_id: str) -> Optional[str]:
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT session_id FROM agent_task
                WHERE chat_session_id=%s AND session_id IS NOT NULL AND runtime_id=%s
                ORDER BY created_at DESC, id DESC LIMIT 1""",
                (chat_session_id, runtime_id),
            ).fetchone()
        return row["session_id"] if row else None
