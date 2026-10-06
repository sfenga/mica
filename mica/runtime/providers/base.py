from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Protocol

from mica.domain import Agent, Issue, Task

AgentEventHandler = Callable[[str, Dict[str, object]], None]
CancellationCheck = Callable[[], bool]

CTX_USAGE_LINE = (
    "run `mica-ctx comments` to scan the discussion, `mica-ctx comment <id>` to expand one"
)

_CTX_COLD_BLOCK = (
    "\n\n## Issue context channel\n\n"
    "Before doing anything else, read this issue's full discussion: "
    + CTX_USAGE_LINE
    + ". Earlier agents' results are posted as comments — read them before "
    "redoing work that may already exist.\n"
)

_CTX_WARM_BLOCK = (
    "\n\n## Issue context channel\n\n"
    "Your session already carries this issue's background; the messages above are this "
    "run's input. If you need anything beyond them — earlier discussion or other agents' "
    "full results — " + CTX_USAGE_LINE + ".\n"
)


def _default_api_url() -> str:
    host = os.environ.get("MICA_API_HOST", "127.0.0.1")
    port = os.environ.get("MICA_API_PORT", "8000")
    return f"http://{host}:{port}"


def build_child_env(issue: Issue, task: Task) -> Dict[str, str]:
    """Environment for the agent subprocess: task-scoped context channel variables.

    Prepends the interpreter's bin directory to PATH so the `mica-ctx` entry point
    (installed alongside the daemon) is resolvable by the agent's shell.
    """
    env = os.environ.copy()
    env["MICA_API_URL"] = os.environ.get("MICA_API_URL") or _default_api_url()
    env["MICA_ISSUE_ID"] = issue.id
    env["MICA_TASK_ID"] = task.id
    # TODO(auth): MICA_TASK_TOKEN is pass-through only; once the platform mints
    # task-scoped tokens, set one here and enforce it on the API side.
    if os.environ.get("MICA_TASK_TOKEN"):
        env["MICA_TASK_TOKEN"] = os.environ["MICA_TASK_TOKEN"]
    venv_bin = str(Path(sys.executable).parent)
    existing_path = env.get("PATH", "")
    env["PATH"] = venv_bin + os.pathsep + existing_path if existing_path else venv_bin
    return env


def _ctx_block(resumed: bool) -> str:
    return _CTX_WARM_BLOCK if resumed else _CTX_COLD_BLOCK


def build_task_prompt(
    issue: Issue, task: Task, resumed: bool = False, ctx_channel: bool = False
) -> str:
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
            prompt = (
                f"Issue context:\n{issue.title}\n\n{issue.body}\n\n"
                f"Continue based on these follow-up messages in order:\n\n{followups}"
            )
            return prompt + (_ctx_block(resumed) if ctx_channel else "")
    if task.handoff_note:
        prompt = f"{issue.title}\n\n{issue.body}\n\nDelegated instruction:\n{task.handoff_note}"
        return prompt + (_ctx_block(resumed) if ctx_channel else "")
    prompt = f"{issue.title}\n\n{issue.body}"
    return prompt + (_ctx_block(resumed) if ctx_channel else "")


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
