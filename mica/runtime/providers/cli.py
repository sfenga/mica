from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from mica.domain import Agent, Issue, Task

from .base import (
    AgentEventHandler,
    CancellationCheck,
    ProviderCancelled,
    ProviderRunResult,
    build_task_prompt,
)

CLAUDE_INTERNAL_SYSTEM_EVENTS = {
    "hook_started",
    "hook_response",
    "thinking_tokens",
}


def _claude_input_frame(prompt: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }
    ) + "\n"


def _claude_control_response(line: str) -> Optional[str]:
    """Build the daemon-side approval for a Claude stream-json control request."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if payload.get("type") != "control_request" or not payload.get("request_id"):
        return None
    request = payload.get("request")
    if not isinstance(request, dict):
        return None
    tool_input = request.get("input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    else:
        tool_input = dict(tool_input)
    if tool_input.get("run_in_background") is True:
        tool_input["run_in_background"] = False
    return json.dumps(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": payload["request_id"],
                "response": {"behavior": "allow", "updatedInput": tool_input},
            },
        }
    ) + "\n"


def _claude_event_type(line: str) -> Optional[str]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    event_type = payload.get("type")
    return event_type if isinstance(event_type, str) else None


def _claude_launched_async_task(line: str) -> bool:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return False
    if payload.get("type") != "user":
        return False

    def contains_async_launch(value: object) -> bool:
        if isinstance(value, dict):
            return value.get("status") == "async_launched" or any(
                contains_async_launch(item) for item in value.values()
            )
        if isinstance(value, list):
            return any(contains_async_launch(item) for item in value)
        return False

    for block in payload.get("message", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            if contains_async_launch(block.get("content")):
                return True
    return False


def parse_cli_event(
    flavor: str, line: str
) -> Tuple[List[Tuple[str, Dict[str, object]]], Optional[str], Optional[str]]:
    """Return normalized events, final text and a discovered session id."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return [], None, None
    events: List[Tuple[str, Dict[str, object]]] = []
    final_text = None
    session_id = None
    event_type = payload.get("type")

    if flavor == "claude":
        if event_type == "system":
            session_id = payload.get("session_id")
            subtype = str(payload.get("subtype", ""))
            if subtype not in CLAUDE_INTERNAL_SYSTEM_EVENTS:
                events.append(("agent.system", {"subtype": subtype}))
        elif event_type == "assistant":
            parent_tool_use_id = payload.get("parent_tool_use_id")
            for block in payload.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    event_payload: Dict[str, object] = {"text": block.get("text", "")}
                    if parent_tool_use_id:
                        event_payload["parent_tool_use_id"] = parent_tool_use_id
                    events.append(("agent.text", event_payload))
                elif block.get("type") == "tool_use":
                    event_payload = {
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    }
                    if parent_tool_use_id:
                        event_payload["parent_tool_use_id"] = parent_tool_use_id
                    events.append(
                        (
                            "agent.tool",
                            event_payload,
                        )
                    )
        elif event_type == "result":
            session_id = payload.get("session_id")
            final_text = payload.get("result") or ""
            if payload.get("is_error"):
                events.append(("agent.error", {"message": final_text or "Claude Code failed"}))
    elif flavor == "codex":
        if event_type == "thread.started":
            session_id = payload.get("thread_id")
            events.append(("agent.system", {"subtype": "thread.started"}))
        elif event_type in {"item.started", "item.updated", "item.completed"}:
            item = payload.get("item", {})
            item_type = item.get("type", "unknown")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    events.append(("agent.text", {"text": text}))
                    if event_type == "item.completed":
                        final_text = text
            else:
                events.append(("agent.tool", {"name": item_type, "item": item}))
        elif event_type == "error":
            events.append(("agent.error", {"message": payload.get("message", "Codex failed")}))
    return events, final_text, session_id


class CliProvider:
    """Runs Codex or Claude Code as a streaming, cancellable subprocess."""

    def __init__(
        self,
        flavor: str,
        executable: Optional[str] = None,
        timeout_seconds: int = 1800,
        command_prefix: Optional[List[str]] = None,
    ) -> None:
        if flavor not in {"codex", "claude"}:
            raise ValueError(f"unsupported CLI provider: {flavor}")
        self.flavor = flavor
        self.executable = executable or flavor
        self.timeout_seconds = timeout_seconds
        self.command_prefix = list(command_prefix) if command_prefix else None

    def run(
        self,
        agent: Agent,
        issue: Issue,
        task: Task,
        on_event: AgentEventHandler,
        workdir: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        is_cancelled: Optional[CancellationCheck] = None,
    ) -> ProviderRunResult:
        if not workdir:
            raise RuntimeError(f"{self.flavor} tasks require a working directory")
        prompt = self._prompt(agent, issue, task)
        command = self._command(agent, prompt, resume_session_id)
        if (
            self.flavor == "claude"
            and self.command_prefix is None
            and hasattr(os, "geteuid")
            and os.geteuid() == 0
            and os.environ.get("IS_SANDBOX", "").lower() not in {"1", "true", "yes", "on"}
        ):
            raise RuntimeError(
                "Claude Code bypassPermissions cannot run as root outside a declared sandbox; "
                "run the daemon as a non-root user or set IS_SANDBOX=1 in a genuine sandbox"
            )
        try:
            process = subprocess.Popen(
                command,
                cwd=workdir,
                stdin=subprocess.PIPE if self.flavor == "claude" else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"CLI not found: {self.executable}") from exc

        lines: queue.Queue[Optional[str]] = queue.Queue()
        stderr_lines: List[str] = []
        stdin_lock = threading.Lock()
        stdin_closed = False
        stdin_errors: List[str] = []

        def write_stdin(frame: str) -> None:
            nonlocal stdin_closed
            if self.flavor != "claude":
                return
            with stdin_lock:
                if stdin_closed or process.stdin is None:
                    return
                try:
                    process.stdin.write(frame)
                    process.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    stdin_errors.append(str(exc))
                    stdin_closed = True

        def close_stdin() -> None:
            nonlocal stdin_closed
            if self.flavor != "claude":
                return
            with stdin_lock:
                if stdin_closed or process.stdin is None:
                    return
                stdin_closed = True
                try:
                    process.stdin.close()
                except OSError:
                    pass

        def read_stdout() -> None:
            assert process.stdout is not None
            for output_line in process.stdout:
                lines.put(output_line)
            lines.put(None)

        def read_stderr() -> None:
            assert process.stderr is not None
            for error_line in process.stderr:
                stderr_lines.append(error_line)
                if len(stderr_lines) > 200:
                    del stderr_lines[:100]

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        prompt_thread = None
        if self.flavor == "claude":
            # Claude may write a startup event before reading stdin. Keep this
            # write off the reader thread and leave stdin open for later
            # control_request/control_response frames.
            prompt_thread = threading.Thread(
                target=write_stdin, args=(_claude_input_frame(prompt),), daemon=True
            )
            prompt_thread.start()
        started_at = time.monotonic()
        output_parts: List[str] = []
        last_top_level_text = ""
        last_top_level_text_sequence = -1
        last_top_level_tool_sequence = -1
        event_sequence = 0
        session_id = resume_session_id
        provider_error = None
        provider_diagnostic = None
        pending_claude_text: List[str] = []
        pending_parent_tool_use_id: Optional[str] = None

        def flush_claude_text() -> None:
            nonlocal pending_parent_tool_use_id
            if not pending_claude_text:
                return
            text_payload: Dict[str, object] = {"text": "".join(pending_claude_text)}
            if pending_parent_tool_use_id:
                text_payload["parent_tool_use_id"] = pending_parent_tool_use_id
            on_event("agent.text", text_payload)
            pending_claude_text.clear()
            pending_parent_tool_use_id = None

        try:
            while True:
                if is_cancelled is not None and is_cancelled():
                    self._terminate(process)
                    raise ProviderCancelled("task cancelled")
                if time.monotonic() - started_at > self.timeout_seconds:
                    self._terminate(process)
                    raise RuntimeError(f"{self.flavor} timed out after {self.timeout_seconds}s")
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                if self.flavor == "claude":
                    control_response = _claude_control_response(line)
                    if control_response is not None:
                        write_stdin(control_response)
                    if _claude_launched_async_task(line):
                        provider_error = (
                            "Claude launched an asynchronous background task; "
                            "Mica-managed runs require foreground execution"
                        )
                events, final_text, discovered_session = parse_cli_event(self.flavor, line)
                event_sequence += 1
                assistant_text = "".join(
                    str(payload.get("text", ""))
                    for event_type, payload in events
                    if event_type == "agent.text"
                    and payload.get("text")
                    and not payload.get("parent_tool_use_id")
                )
                if assistant_text.strip():
                    if last_top_level_text_sequence > last_top_level_tool_sequence:
                        last_top_level_text += assistant_text
                    else:
                        last_top_level_text = assistant_text
                    last_top_level_text_sequence = event_sequence
                for event_type, payload in events:
                    if self.flavor == "claude" and event_type == "agent.text":
                        parent_tool_use_id = payload.get("parent_tool_use_id")
                        parent = str(parent_tool_use_id) if parent_tool_use_id else None
                        if pending_claude_text and parent != pending_parent_tool_use_id:
                            flush_claude_text()
                        pending_parent_tool_use_id = parent
                        pending_claude_text.append(str(payload.get("text", "")))
                        continue
                    flush_claude_text()
                    if event_type == "agent.tool" and not payload.get("parent_tool_use_id"):
                        last_top_level_tool_sequence = event_sequence
                    if (
                        event_type == "agent.system"
                        and payload.get("subtype") == "permission_denied"
                    ):
                        provider_diagnostic = "Claude Code reported a permission denial"
                    on_event(event_type, payload)
                    if event_type == "agent.error":
                        provider_error = str(payload.get("message") or "agent failed")
                if final_text:
                    output_parts.append(final_text)
                if discovered_session:
                    session_id = discovered_session
                if self.flavor == "claude" and _claude_event_type(line) == "result":
                    close_stdin()
            flush_claude_text()
            close_stdin()
            return_code = process.wait(timeout=5)
        finally:
            close_stdin()
            if process.poll() is None:
                self._terminate(process)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            if prompt_thread is not None:
                prompt_thread.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

        stderr = "".join(stderr_lines).strip()
        if provider_error:
            raise RuntimeError(provider_error)
        if return_code != 0:
            raise RuntimeError(stderr or f"{self.flavor} exited with code {return_code}")
        output = output_parts[-1] if output_parts else ""
        if (
            not output
            and last_top_level_text
            and last_top_level_text_sequence > last_top_level_tool_sequence
        ):
            output = last_top_level_text
        if not output:
            detail = (
                provider_diagnostic
                or (f"failed to write Claude input: {stdin_errors[-1]}" if stdin_errors else None)
                or f"{self.flavor} stream ended without a final response"
            )
            raise RuntimeError(stderr or detail)
        return ProviderRunResult(output=output, session_id=session_id)

    def _command(self, agent: Agent, prompt: str, resume_session_id: Optional[str]) -> List[str]:
        if self.command_prefix is not None:
            return [*self.command_prefix, prompt]
        config = agent.provider_config
        executable = str(config.get("executable") or self.executable)
        model = config.get("model")
        if self.flavor == "codex":
            if agent.instructions and (
                not resume_session_id or "## Squad leader role" in agent.instructions
            ):
                prompt = f"Agent instructions:\n{agent.instructions}\n\n{prompt}"
            if resume_session_id:
                command = [executable, "exec", "resume", "--json"]
                if model:
                    command.extend(["--model", str(model)])
                command.extend([resume_session_id, prompt])
            else:
                command = [executable, "exec", "--json", "--sandbox", "workspace-write"]
                if model:
                    command.extend(["--model", str(model)])
                command.append(prompt)
            return command
        command = [
            executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
            "--disallowedTools",
            "AskUserQuestion",
        ]
        if model:
            command.extend(["--model", str(model)])
        if agent.instructions:
            command.extend(["--append-system-prompt", agent.instructions])
        if resume_session_id:
            command.extend(["--resume", resume_session_id])
        return command

    @staticmethod
    def _prompt(agent: Agent, issue: Issue, task: Task) -> str:
        return build_task_prompt(issue, task)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)
