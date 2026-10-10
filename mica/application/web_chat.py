"""Web conversation actions; durable messages and tasks share a transaction."""

import logging
from dataclasses import asdict
from hashlib import sha256

from mica.domain import ChatMessage, ChatSession, Task, TaskStatus, utc_now

from .services import InvalidStateError, NotFoundError

ACTIVE = {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.RUNNING, TaskStatus.WAITING_INPUT}


class WebChatService:
    def __init__(self, container):
        self.c = container
        self.chats = container.chats

    def get(self, session_id, workspace_id="local", locked=False):
        session = (
            self.chats.get_session_for_update(session_id)
            if locked
            else self.chats.get_session(session_id)
        )
        if session is None or session.workspace_id != workspace_id:
            raise NotFoundError("会话不存在")
        return session

    def tasks(self, session_id):
        return sorted(
            (t for t in self.c.tasks.list() if t.chat_session_id == session_id),
            key=lambda t: (t.created_at, t.id),
        )

    def document(self, session, user_id):
        summary = self.chats.message_summary(session.id, user_id)
        binding = self.chats.binding_for_session(session.id)
        active = [t for t in self.tasks(session.id) if t.status in ACTIVE]
        result = asdict(session)
        result.update(
            source=binding.channel if binding else "web",
            last_message=summary["last_message"],
            unread_count=summary["unread_count"],
            pending_tasks=[asdict(t) for t in active],
        )
        if summary["last_message_at"]:
            result["updated_at"] = max(session.updated_at, summary["last_message_at"])
        return result

    def list(self, workspace_id, user_id):
        items = [self.document(s, user_id) for s in self.chats.list_sessions(workspace_id)]
        return sorted(items, key=lambda s: (s["pinned"], s["updated_at"], s["id"]), reverse=True)

    def send(self, body, request_id, user_id, workspace_id="local", session_id=None, agent_id=None):
        body = body.strip()
        if not body:
            raise ValueError("消息不能为空")
        with self.c.chat_ingress._transaction():
            if session_id is None:
                if self.c.agents.get(agent_id) is None:
                    raise NotFoundError("Agent 不存在")
                digest = sha256(f"{workspace_id}:{user_id}:{request_id}".encode()).hexdigest()
                session = self.chats.add_session(
                    ChatSession(
                        id=f"chat_web_{digest}",
                        workspace_id=workspace_id,
                        agent_id=agent_id,
                        creator_user_id=user_id,
                        title=body.splitlines()[0][:80],
                    )
                )
                if session.agent_id != agent_id or session.creator_user_id != user_id:
                    raise InvalidStateError("请求 ID 已用于另一会话")
                session_id = session.id
            session = self.get(session_id, workspace_id, locked=True)
            key = "web:" + sha256(f"{user_id}:{request_id}".encode()).hexdigest()
            previous = self.chats.find_message(session_id, key)
            if previous:
                if previous.body != body:
                    raise InvalidStateError("请求 ID 已用于另一条消息")
                task = self.c.tasks.get(previous.task_id)
                return {
                    "session": asdict(session),
                    "message": asdict(previous),
                    "task": asdict(task),
                    "created": False,
                }
            if session.status != "active":
                raise InvalidStateError("归档会话不能发送消息，请先恢复")
            agent = self.c.agents.get(session.agent_id)
            if agent is None:
                raise NotFoundError("Agent 不存在")
            # Preserve arrival order without mixing the channel debounce batch
            # into this immediate Web turn or changing its reply audience.
            if self.chats.unowned_messages(session_id):
                self.c.chat_ingress.flush_session(session_id)
            task = Task(
                issue_id=None,
                agent_id=agent.id,
                runtime_id=agent.runtime_id,
                trigger_type="chat.message",
                trigger_id=session_id,
                chat_session_id=session_id,
            )
            task.chat_input_task_id = task.id
            message = ChatMessage(
                chat_session_id=session_id,
                role="user",
                body=body,
                external_message_id=key,
                task_id=task.id,
                channel_ingested=False,
            )
            self.chats.append_message(message)
            self.c.tasks.add(task)
            session.updated_at = utc_now()
            self.chats.save_session(session)
            self.c.task_service.record(task.id, "task.queued", {"chat_session_id": session_id})
        self.c.worker.wake()
        return {
            "session": asdict(session),
            "message": asdict(message),
            "task": asdict(task),
            "created": True,
        }

    def update(self, session_id, workspace_id, title=None, pinned=None, archived=None):
        with self.c.chat_ingress._transaction():
            session = self.get(session_id, workspace_id, locked=True)
            if archived is True:
                if any(t.status in ACTIVE for t in self.tasks(session_id)):
                    raise InvalidStateError("请先停止执行并取消排队消息，再归档会话")
                if self.chats.unowned_messages(session_id):
                    raise InvalidStateError("会话有待处理消息，请处理完成后归档")
            if title is not None:
                if not title.strip():
                    raise ValueError("会话标题不能为空")
                session.title = title.strip()
            if pinned is not None:
                session.pinned = pinned
            if archived is not None:
                session.status = "archived" if archived else "active"
            session.updated_at = utc_now()
            return self.chats.save_session(session)

    def delete(self, session_id, workspace_id):
        with self.c.chat_ingress._transaction():
            session = self.get(session_id, workspace_id, locked=True)
            if session.status != "archived":
                raise InvalidStateError("请先归档会话，再删除")
            tasks = self.tasks(session_id)
            if any(t.status in ACTIVE for t in tasks):
                raise InvalidStateError("会话仍有执行中的任务")
            task_ids = [t.id for t in tasks]
            event_ids = [e.id for t in tasks for e in self.c.events.list(t.id)]
            binding = self.chats.binding_for_session(session_id)
            event_ids += [
                m.external_message_id
                for m in self.chats.list_messages(session_id)
                if m.external_message_id
            ]
            if binding:
                event_ids.append(binding.id)
            self.c.channels.delete_chat_data(event_ids)
            self.c.events.delete_for_tasks(task_ids)
            self.chats.delete_session(session_id)
        # Database deletion is committed before filesystem cleanup. Never
        # trust work_dir from a remote daemon or delete an arbitrary path.
        try:
            self.c.workspaces.remove_chat(session_id)
        except (OSError, ValueError):
            logging.getLogger(__name__).exception("chat workspace cleanup failed: %s", session_id)

    def mark_read(self, session_id, workspace_id, user_id, message_id):
        with self.c.chat_ingress._transaction():
            self.get(session_id, workspace_id, locked=True)
            messages = self.chats.list_messages(session_id)
            message = next((m for m in messages if m.id == message_id), None)
            if message is None:
                raise NotFoundError("消息不存在")
            self.chats.mark_read(session_id, user_id, message.created_at)
