from __future__ import annotations

from typing import List, Optional

from mica.domain import Agent, Issue, Task

from .base import (
    AgentEventHandler,
    CancellationCheck,
    ChatBrief,
    ProviderCancelled,
    ProviderRunResult,
)


class StubProvider:
    """Deterministic provider used by local development and contract tests."""

    def __init__(self, fail_with: Optional[str] = None) -> None:
        self.fail_with = fail_with

    def run(
        self,
        agent: Agent,
        issue: Optional[Issue],
        task: Task,
        on_event: AgentEventHandler,
        workdir: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        is_cancelled: Optional[CancellationCheck] = None,
        replay_conflicts: Optional[List[str]] = None,
        chat: Optional[ChatBrief] = None,
    ) -> ProviderRunResult:
        if is_cancelled is not None and is_cancelled():
            raise ProviderCancelled("task cancelled")
        subject = chat.prompt if chat is not None else f"{issue.id}: {issue.title}"
        on_event("agent.message", {"content": f"{agent.name} accepted {subject}"})
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        return ProviderRunResult(
            output=f"Processed: {subject}",
            session_id=resume_session_id or f"stub-session-{task.id}",
        )
