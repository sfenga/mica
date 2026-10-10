from __future__ import annotations

import hashlib
import shutil
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

    def remove_chat(self, chat_session_id: str) -> None:
        """Remove only the directory derived from an owned conversation ID."""
        key = hashlib.sha256(chat_session_id.encode()).hexdigest()[:24]
        path = self.root / "chats" / key
        if path.exists():
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError("chat workspace is outside the managed root")
            shutil.rmtree(path)
