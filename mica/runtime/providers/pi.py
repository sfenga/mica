from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from mica.domain import Agent, Issue, Task

from .base import (
    AgentEventHandler,
    CancellationCheck,
    ChatBrief,
    ProviderCancelled,
    ProviderRunResult,
    build_task_prompt,
)


class PiProvider:
    """Runs Pi in JSON mode with a durable session file."""

    def __init__(self, executable: str = "pi", timeout_seconds: int = 1800) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

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
        if not workdir:
            raise RuntimeError("pi tasks require a working directory")
        config = agent.provider_config
        executable = str(config.get("executable") or self.executable)
        session_path = resume_session_id or self._new_session_path()
        Path(session_path).parent.mkdir(parents=True, exist_ok=True)
        Path(session_path).touch(exist_ok=True)
        command = [executable, "-p", "--mode", "json", "--session", session_path]
        if config.get("model"):
            command.extend(["--model", str(config["model"])])
        process = subprocess.Popen(
            command,
            cwd=workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=os.name != "nt",
        )
        prompt = (
            chat.prompt
            if chat is not None
            else self._prompt(issue, task, replay_conflicts=replay_conflicts)
        )
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()
        lines: queue.Queue[Optional[str]] = queue.Queue()
        stderr: list[str] = []

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        def read_stderr() -> None:
            assert process.stderr is not None
            stderr.extend(process.stderr)

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()
        started = time.monotonic()
        output: list[str] = []
        try:
            while True:
                if is_cancelled and is_cancelled():
                    self._terminate(process)
                    raise ProviderCancelled("pi task cancelled")
                if time.monotonic() - started > float(
                    config.get("timeout_seconds", self.timeout_seconds)
                ):
                    self._terminate(process)
                    raise RuntimeError("pi task timed out")
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type", "agent.event")
                if event_type == "message_update":
                    delta = (event.get("assistantMessageEvent") or {}).get("delta", "")
                    if delta:
                        output.append(delta)
                        on_event("agent.text", {"text": delta})
                elif event_type.startswith("tool_execution"):
                    on_event(
                        "agent.tool",
                        {"name": event.get("toolName", ""), "event": event_type},
                    )
                elif event_type == "error":
                    on_event("agent.error", {"message": str(event.get("message", "pi failed"))})
            return_code = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                self._terminate(process)
        if return_code != 0:
            raise RuntimeError("".join(stderr).strip() or f"pi exited with code {return_code}")
        result = "".join(output).strip()
        if not result:
            raise RuntimeError("".join(stderr).strip() or "pi stream ended without output")
        return ProviderRunResult(output=result, session_id=session_path)

    @staticmethod
    def _prompt(issue: Issue, task: Task, replay_conflicts: Optional[List[str]] = None) -> str:
        return build_task_prompt(issue, task, replay_conflicts=replay_conflicts)

    @staticmethod
    def _new_session_path() -> str:
        return str(Path.home() / ".mica" / "pi-sessions" / f"{uuid4().hex}.jsonl")

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
