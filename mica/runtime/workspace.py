from __future__ import annotations

import hashlib
from pathlib import Path


class WorkspaceManager:
    """Provides a durable empty working directory for non-project local tasks."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()

    def prepare(self, issue_id: str, agent_id: str) -> str:
        key = hashlib.sha256(f"{issue_id}\0{agent_id}".encode()).hexdigest()[:24]
        path = self.root / key / "workdir"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def prepare_chat(self, chat_session_id: str) -> str:
        """Stable per-conversation working directory. The path is the daemon's
        own state — it is pinned back to chat_session.work_dir and reused every
        turn, and recreated silently when deleted (a chat workdir holds no
        durable work of its own)."""
        key = hashlib.sha256(chat_session_id.encode()).hexdigest()[:24]
        path = self.root / "chats" / key / "workdir"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
