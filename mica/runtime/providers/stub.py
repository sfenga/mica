from __future__ import annotations

from typing import List, Optional

from mica.domain import Agent, Issue, Task

from .base import AgentEventHandler, CancellationCheck, ProviderCancelled, ProviderRunResult


class StubProvider:
    """Deterministic provider used by local development and contract tests."""

    def __init__(self, fail_with: Optional[str] = None) -> None:
        self.fail_with = fail_with

    def run(
        self,
        agent: Agent,
        issue: Issue,
        task: Task,
        on_event: AgentEventHandler,
        workdir: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        is_cancelled: Optional[CancellationCheck] = None,
        replay_conflicts: Optional[List[str]] = None,
    ) -> ProviderRunResult:
        if is_cancelled is not None and is_cancelled():
            raise ProviderCancelled("task cancelled")
        on_event("agent.message", {"content": f"{agent.name} accepted {issue.id}"})
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        return ProviderRunResult(
            output=f"Processed: {issue.title}",
            session_id=resume_session_id or f"stub-session-{task.id}",
        )
