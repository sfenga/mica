from __future__ import annotations

import hashlib
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Dict, Optional, Tuple


class WorktreeManager:
    """Creates one durable branch/worktree for each Issue and Agent pair."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self._locks: Dict[str, Lock] = {}
        self._guard = Lock()

    def prepare(
        self,
        source_path: str,
        issue_id: str,
        agent_id: str,
        previous_branch: Optional[str] = None,
        previous_path: Optional[str] = None,
    ) -> Tuple[str, str]:
        source = self._repository_root(source_path)
        key = f"{source}:{issue_id}:{agent_id}"
        with self._guard:
            lock = self._locks.setdefault(key, Lock())
        with lock:
            with self._repository_lock(source):
                return self._prepare_locked(
                    source,
                    issue_id,
                    agent_id,
                    previous_branch,
                    previous_path,
                )

    def _prepare_locked(
        self,
        source: Path,
        issue_id: str,
        agent_id: str,
        previous_branch: Optional[str],
        previous_path: Optional[str],
    ) -> Tuple[str, str]:
        branch = previous_branch or self._branch_name(issue_id, agent_id)
        path = (
            Path(previous_path).resolve()
            if previous_path
            else self._path(source, issue_id, agent_id)
        )
        if path.exists():
            current = self._git(path, "branch", "--show-current").strip()
            if current != branch:
                raise RuntimeError(f"worktree {path} uses branch {current}, expected {branch}")
            return branch, str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(source, "worktree", "prune")
        branch_exists = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                ],
                check=False,
            ).returncode
            == 0
        )
        args = ["worktree", "add"]
        if not branch_exists:
            args.extend(["-b", branch])
        args.extend([str(path), branch if branch_exists else "HEAD"])
        self._git(source, *args)
        return branch, str(path)

    @contextmanager
    def _repository_lock(self, source: Path):
        common_dir_value = self._git(source, "rev-parse", "--git-common-dir").strip()
        common_dir = Path(common_dir_value)
        if not common_dir.is_absolute():
            common_dir = (source / common_dir).resolve()
        lock_path = common_dir / "mica-worktree.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                import fcntl
            except ModuleNotFoundError:
                yield
                return
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _repository_root(self, source_path: str) -> Path:
        source = Path(source_path).expanduser().resolve()
        if not source.is_dir():
            raise RuntimeError(f"project path does not exist: {source}")
        root = self._git(source, "rev-parse", "--show-toplevel").strip()
        repository = Path(root).resolve()
        dirty = self._git(repository, "status", "--porcelain").strip()
        if dirty:
            raise RuntimeError(
                "project repository has uncommitted changes; commit or stash them before creating a worktree"
            )
        return repository

    def _path(self, source: Path, issue_id: str, agent_id: str) -> Path:
        repo_key = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
        return self.root / repo_key / f"{self._slug(issue_id)}-{self._slug(agent_id)}"

    @classmethod
    def _branch_name(cls, issue_id: str, agent_id: str) -> str:
        return f"mica/{cls._slug(issue_id)}/{cls._slug(agent_id)}"

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
        return (slug or "task")[:48]

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
        return completed.stdout
