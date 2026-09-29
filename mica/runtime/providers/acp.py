from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mica.domain import Agent, Issue, Task

from .base import (
    AgentEventHandler,
    CancellationCheck,
    ProviderCancelled,
    ProviderRunResult,
    build_task_prompt,
)


class _AcpProcess:
    def __init__(self, command: List[str], cwd: str, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.started_at = time.monotonic()
        try:
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"ACP agent executable not found: {command[0]}") from exc
        self.lines: queue.Queue[Optional[str]] = queue.Queue()
        self.stderr_lines: List[str] = []
        self.next_id = 1
        self._write_lock = threading.Lock()
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def request(
        self,
        method: str,
        params: Dict[str, Any],
        on_message,
        is_cancelled: Optional[CancellationCheck] = None,
        cancel_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            if is_cancelled is not None and is_cancelled():
                if cancel_session_id:
                    self.notify("session/cancel", {"sessionId": cancel_session_id})
                raise ProviderCancelled("ACP task cancelled")
            if time.monotonic() - self.started_at > self.timeout_seconds:
                if cancel_session_id:
                    self.notify("session/cancel", {"sessionId": cancel_session_id})
                raise RuntimeError(f"ACP agent timed out after {self.timeout_seconds:g}s")
            try:
                line = self.lines.get(timeout=0.2)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError(self._closed_message(method))
                continue
            if line is None:
                raise RuntimeError(self._closed_message(method))
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            messages = payload if isinstance(payload, list) else [payload]
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if message.get("id") == request_id and ("result" in message or "error" in message):
                    if "error" in message:
                        error = message.get("error") or {}
                        raise RuntimeError(
                            f"ACP {method} failed ({error.get('code', 'unknown')}): "
                            f"{error.get('message', 'unknown error')}"
                        )
                    result = message.get("result")
                    return result if isinstance(result, dict) else {}
                response = on_message(message)
                if response is not None and "id" in message:
                    if "__jsonrpc_error__" in response:
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                                "error": response["__jsonrpc_error__"],
                            }
                        )
                    else:
                        self._send({"jsonrpc": "2.0", "id": message["id"], "result": response})

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                if os.name != "nt":
                    os.killpg(self.process.pid, signal.SIGTERM)
                else:
                    self.process.terminate()
                self.process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait(timeout=1)
        self._stdout_thread.join(timeout=1)
        self._stderr_thread.join(timeout=1)
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()

    def _send(self, payload: Dict[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError(self._closed_message(str(payload.get("method", "request"))))
        with self._write_lock:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_lines.append(line)
            if len(self.stderr_lines) > 200:
                del self.stderr_lines[:100]

    def _closed_message(self, method: str) -> str:
        stderr = "".join(self.stderr_lines).strip()
        return stderr or f"ACP agent closed before responding to {method}"


class AcpProvider:
    """ACP v1 client for local coding agents exposed over JSON-RPC stdio."""

    def __init__(self, timeout_seconds: int = 1800) -> None:
        self.timeout_seconds = timeout_seconds

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
        if not workdir or not Path(workdir).is_absolute():
            raise RuntimeError("ACP tasks require an absolute working directory")
        config = agent.provider_config
        command = config.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) for item in command)
        ):
            raise RuntimeError("ACP provider_config.command must be a non-empty string array")
        timeout = float(config.get("timeout_seconds", self.timeout_seconds))
        rpc = _AcpProcess(command, workdir, timeout)
        session_id: Optional[str] = None
        text_parts: List[str] = []

        def handle(message: Dict[str, Any]):
            method = message.get("method")
            params = message.get("params") or {}
            if method == "session/update":
                self._handle_update(params.get("update") or {}, on_event, text_parts)
                return None
            if method == "session/request_permission":
                return self._permission_response(config, params, on_event)
            if method:
                on_event("agent.protocol", {"protocol": "acp", "method": method})
                if "id" in message:
                    return {
                        "__jsonrpc_error__": {
                            "code": -32601,
                            "message": f"client method is not supported: {method}",
                        }
                    }
            return None

        try:
            initialized = rpc.request(
                "initialize",
                {"protocolVersion": 1, "clientCapabilities": {}},
                handle,
                is_cancelled,
            )
            negotiated = initialized.get("protocolVersion", 1)
            if negotiated != 1:
                raise RuntimeError(
                    f"ACP protocol version mismatch: expected 1, received {negotiated}"
                )
            capabilities = initialized.get("agentCapabilities") or {}
            on_event(
                "agent.system",
                {
                    "protocol": "acp",
                    "protocol_version": 1,
                    "agent_info": initialized.get("agentInfo") or {},
                },
            )
            session_params = {
                "cwd": str(Path(workdir).resolve()),
                "mcpServers": self._mcp_servers(config),
            }
            if resume_session_id:
                if not capabilities.get("loadSession"):
                    raise RuntimeError(
                        "ACP agent does not advertise loadSession; cannot continue prior session"
                    )
                session_params["sessionId"] = resume_session_id
                rpc.request("session/load", session_params, handle, is_cancelled)
                session_id = resume_session_id
            else:
                created = rpc.request("session/new", session_params, handle, is_cancelled)
                session_id = created.get("sessionId")
                if not session_id:
                    raise RuntimeError("ACP session/new response is missing sessionId")
            prompt = self._prompt(agent, issue, task)
            result = rpc.request(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
                handle,
                is_cancelled,
                cancel_session_id=session_id,
            )
            stop_reason = result.get("stopReason", "unknown")
            on_event("agent.stop", {"protocol": "acp", "reason": stop_reason})
            output = "".join(text_parts).strip()
            if not output:
                output = f"ACP turn stopped: {stop_reason}"
            return ProviderRunResult(output=output, session_id=session_id)
        finally:
            rpc.close()

    @staticmethod
    def _mcp_servers(config: Dict[str, Any]) -> List[Dict[str, Any]]:
        servers = config.get("mcp_servers", [])
        if not isinstance(servers, list):
            raise RuntimeError("ACP provider_config.mcp_servers must be an array")
        return servers

    @staticmethod
    def _prompt(agent: Agent, issue: Issue, task: Task) -> str:
        body = build_task_prompt(issue, task)
        return f"{agent.instructions}\n\n{body}".strip() if agent.instructions else body

    @staticmethod
    def _permission_response(
        config: Dict[str, Any], params: Dict[str, Any], on_event: AgentEventHandler
    ) -> Dict[str, Any]:
        policy = str(config.get("permission_policy", "deny"))
        options = params.get("options") or []
        selected = None
        if policy == "allow":
            preferred = ("allow_once", "allow_always")
            selected = next(
                (option for kind in preferred for option in options if option.get("kind") == kind),
                options[0] if options else None,
            )
        on_event(
            "agent.permission",
            {
                "protocol": "acp",
                "policy": policy,
                "tool_call_id": params.get("toolCall", {}).get("toolCallId"),
                "outcome": "selected" if selected else "cancelled",
                "option_id": selected.get("optionId") if selected else None,
            },
        )
        if selected:
            return {"outcome": {"outcome": "selected", "optionId": selected.get("optionId")}}
        return {"outcome": {"outcome": "cancelled"}}

    @staticmethod
    def _handle_update(
        update: Dict[str, Any], on_event: AgentEventHandler, text_parts: List[str]
    ) -> None:
        kind = str(update.get("sessionUpdate", "unknown"))
        content = update.get("content") or {}
        if kind == "agent_message_chunk" and content.get("type") == "text":
            text = str(content.get("text", ""))
            text_parts.append(text)
            on_event("agent.text", {"text": text, "protocol": "acp"})
        elif kind == "agent_thought_chunk":
            on_event("agent.thought", {"content": content, "protocol": "acp"})
        elif kind in {"tool_call", "tool_call_update"}:
            on_event("agent.tool", {"update": update, "protocol": "acp"})
        elif kind == "plan":
            on_event("agent.plan", {"update": update, "protocol": "acp"})
        elif kind == "usage_update":
            on_event("agent.usage", {"update": update, "protocol": "acp"})
        else:
            on_event("agent.update", {"kind": kind, "update": update, "protocol": "acp"})
