from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

from mica.domain import Project

from .ports import ProjectRepository, RuntimeRepository
from .services import NotFoundError


@dataclass(frozen=True)
class ProjectCandidate:
    name: str
    local_path: str


class ProjectService:
    """Registers local Git repositories that a Runtime can execute against."""

    def __init__(
        self,
        projects: ProjectRepository,
        runtimes: RuntimeRepository,
        environ: Optional[Mapping[str, str]] = None,
        working_directory: Optional[Path] = None,
    ) -> None:
        self.projects = projects
        self.runtimes = runtimes
        self.environ = environ if environ is not None else os.environ
        self.working_directory = (working_directory or Path.cwd()).resolve()

    def list(self, workspace_id: Optional[str] = None) -> list[Project]:
        return self.projects.list(workspace_id)

    def discover(self) -> list[ProjectCandidate]:
        candidates: dict[str, ProjectCandidate] = {}
        for root in self._roots():
            for path in self._walk(root):
                if self._is_git_repository(path):
                    resolved = str(path.resolve())
                    candidates[resolved] = ProjectCandidate(path.name, resolved)
        return sorted(candidates.values(), key=lambda item: item.name.lower())

    def register(
        self,
        name: str,
        local_path: str,
        workspace_id: str,
        runtime_id: str,
        execution_mode: str = "worktree",
    ) -> Project:
        self._validate_execution_mode(execution_mode)
        if self.runtimes.get(runtime_id) is None:
            raise NotFoundError(f"runtime not found: {runtime_id}")
        path = Path(local_path).expanduser().resolve()
        if not self._within_roots(path):
            raise ValueError("project directory is outside MICA_PROJECT_ROOTS")
        if not self._is_git_repository(path):
            raise ValueError(f"project directory is not a Git repository: {path}")
        existing = self.projects.find_by_path(workspace_id, runtime_id, str(path))
        if existing is not None:
            if existing.execution_mode != execution_mode or existing.name != (name.strip() or path.name):
                return self.projects.save(
                    replace(
                        existing,
                        name=name.strip() or path.name,
                        execution_mode=execution_mode,
                    )
                )
            return existing
        return self.projects.add(
            Project(
                name=name.strip() or path.name,
                workspace_id=workspace_id,
                runtime_id=runtime_id,
                local_path=str(path),
                execution_mode=execution_mode,
            )
        )

    def set_execution_mode(self, project_id: str, execution_mode: str) -> Project:
        self._validate_execution_mode(execution_mode)
        project = self.get(project_id)
        return self.projects.save(replace(project, execution_mode=execution_mode))

    def get(self, project_id: str) -> Project:
        project = self.projects.get(project_id)
        if project is None:
            raise NotFoundError(f"project not found: {project_id}")
        return project

    @staticmethod
    def _validate_execution_mode(execution_mode: str) -> None:
        if execution_mode not in {"in_place", "worktree"}:
            raise ValueError("project execution_mode must be in_place or worktree")

    def _roots(self) -> list[Path]:
        configured = self.environ.get("MICA_PROJECT_ROOTS", "").strip()
        values = configured.split(os.pathsep) if configured else [str(self.working_directory.parent)]
        return [Path(value).expanduser().resolve() for value in values if value.strip()]

    def _within_roots(self, path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in self._roots())

    @staticmethod
    def _is_git_repository(path: Path) -> bool:
        return path.is_dir() and (path / ".git").exists()

    @staticmethod
    def _walk(root: Path):
        if not root.is_dir():
            return
        yield root
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or not child.is_dir() or child.is_symlink():
                continue
            yield child
            if (child / ".git").exists():
                continue
            try:
                grandchildren = child.iterdir()
            except OSError:
                continue
            for grandchild in grandchildren:
                if not grandchild.name.startswith(".") and grandchild.is_dir() and not grandchild.is_symlink():
                    yield grandchild
