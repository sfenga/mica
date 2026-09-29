from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol

from mica.domain import Agent, Issue, Task

AgentEventHandler = Callable[[str, Dict[str, object]], None]
CancellationCheck = Callable[[], bool]


def build_task_prompt(issue: Issue, task: Task) -> str:
    if task.trigger_type == "comment.created":
        delivered = task.delivered_comment_ids or [
            *task.merged_comment_ids,
            *([task.trigger_comment_id] if task.trigger_comment_id else []),
        ]
        delivered_set = set(delivered)
        comments = sorted(
            (item for item in issue.comments if item.id in delivered_set),
            key=lambda item: (item.created_at, item.id),
        )
        if comments:
            followups = "\n\n".join(
                f"[{index}] {comment.body}" for index, comment in enumerate(comments, 1)
            )
            return (
                f"Issue context:\n{issue.title}\n\n{issue.body}\n\n"
                f"Continue based on these follow-up messages in order:\n\n{followups}"
            )
    if task.handoff_note:
        return f"{issue.title}\n\n{issue.body}\n\nDelegated instruction:\n{task.handoff_note}"
    return f"{issue.title}\n\n{issue.body}"


class ProviderCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderRunResult:
    output: str
    session_id: Optional[str] = None
    requires_input: bool = False
    wait_reason: Optional[str] = None


class AgentProvider(Protocol):
    def run(
        self,
        agent: Agent,
        issue: Issue,
        task: Task,
        on_event: AgentEventHandler,
        workdir: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        is_cancelled: Optional[CancellationCheck] = None,
    ) -> ProviderRunResult: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: Dict[str, AgentProvider] = {}

    def register(self, name: str, provider: AgentProvider) -> None:
        self._providers[name] = provider

    def contains(self, name: str) -> bool:
        return name in self._providers

    def get(self, name: str) -> AgentProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise LookupError(f"provider not registered: {name}") from exc
