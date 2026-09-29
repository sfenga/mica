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
