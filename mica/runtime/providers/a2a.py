from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse
from uuid import uuid4

import httpx

from mica.domain import Agent, Issue, Task

from .base import (
    AgentEventHandler,
    CancellationCheck,
    ChatBrief,
    ProviderCancelled,
    ProviderRunResult,
    build_task_prompt,
)

TERMINAL_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
}
CONTINUABLE_STATES = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
FAILED_STATES = {"TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}


class A2aProvider:
    """A2A 1.0 HTTP+JSON client for remote enterprise agents."""

    def __init__(self, client: Optional[httpx.Client] = None) -> None:
        self._client = client

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
        config = agent.provider_config
        timeout = float(config.get("timeout_seconds", 300))
        client = self._client or httpx.Client(timeout=min(timeout, 30), follow_redirects=False)
        close_client = self._client is None
        try:
            card_url = self._card_url(config)
            self._validate_url(card_url, config, "Agent Card")
            headers = self._headers(config)
            card = self._json_request(client, "GET", card_url, headers, config, is_cancelled)
            endpoint = self._select_rest_endpoint(card, card_url, config)
            self._validate_skill(card, config.get("skill_id"))
            on_event(
                "agent.system",
                {
                    "protocol": "a2a",
                    "protocol_version": "1.0",
                    "agent_name": card.get("name"),
                    "endpoint": endpoint,
                    "streaming": bool((card.get("capabilities") or {}).get("streaming")),
                },
            )

            previous = self._decode_session(resume_session_id)
            message: Dict[str, Any] = {
                "messageId": str(uuid4()),
                "role": "ROLE_USER",
                "parts": [self._input_part(agent, issue, task, chat=chat)],
            }
            if previous.get("context_id"):
                message["contextId"] = previous["context_id"]
            if previous.get("state") in CONTINUABLE_STATES and previous.get("task_id"):
                message["taskId"] = previous["task_id"]
            request_body = {
                "message": message,
                "configuration": {"acceptedOutputModes": ["application/json", "text/plain"]},
            }
            payload = self._json_request(
                client,
                "POST",
                endpoint.rstrip("/") + "/message:send",
                headers,
                config,
                is_cancelled,
                json_body=request_body,
            )
            remote_task, direct_message = self._unwrap(payload)
            seen: set[str] = set()
            if direct_message is not None:
                output_value = self._output_from_parts(
                    direct_message.get("parts") or [], on_event, seen
                )
                context_id = direct_message.get("contextId") or previous.get("context_id")
                result = self._validated_output(output_value, config)
                return ProviderRunResult(
                    output=result,
                    session_id=self._encode_session(context_id, None, "MESSAGE"),
                )
            if remote_task is None or not remote_task.get("id"):
                raise RuntimeError("A2A response contains neither task nor message")

            remote_task_id = str(remote_task["id"])
            context_id = remote_task.get("contextId") or previous.get("context_id")
            deadline = time.monotonic() + timeout
            cancelled_remotely = False
            try:
                while True:
                    state = self._record_task(remote_task, on_event, seen)
                    if state in TERMINAL_STATES:
                        break
                    if is_cancelled is not None and is_cancelled():
                        self._cancel_remote(client, endpoint, remote_task_id, headers, config)
                        cancelled_remotely = True
                        raise ProviderCancelled("A2A task cancelled")
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"A2A task {remote_task_id} timed out after {timeout:g}s"
                        )
                    self._wait(float(config.get("poll_interval", 1)), is_cancelled)
                    remote_task = self._json_request(
                        client,
                        "GET",
                        endpoint.rstrip("/") + f"/tasks/{quote(remote_task_id, safe='')}",
                        headers,
                        config,
                        is_cancelled,
                    )
            except ProviderCancelled:
                if not cancelled_remotely:
                    self._cancel_remote(client, endpoint, remote_task_id, headers, config)
                raise

            if state in FAILED_STATES:
                detail = self._status_text(remote_task) or state
                raise RuntimeError(f"remote A2A task ended in {state}: {detail}")
            output_value = self._task_output(remote_task, on_event, seen)
            requires_input = state in CONTINUABLE_STATES
            result = (
                self._string_output(output_value)
                if requires_input
                else self._validated_output(output_value, config)
            )
            return ProviderRunResult(
                output=result,
                session_id=self._encode_session(context_id, remote_task_id, state),
                requires_input=requires_input,
                wait_reason=state if requires_input else None,
            )
        finally:
            if close_client:
                client.close()

    @staticmethod
    def _card_url(config: Dict[str, Any]) -> str:
        configured = config.get("agent_card_url")
        if not configured:
            raise RuntimeError("A2A provider_config.agent_card_url is required")
        return str(configured)

    @staticmethod
    def _headers(config: Dict[str, Any]) -> Dict[str, str]:
        headers = {
            "A2A-Version": "1.0",
            "Accept": "application/a2a+json, application/json",
            "Content-Type": "application/a2a+json",
        }
        token_env = config.get("token_env")
        if token_env:
            token = os.getenv(str(token_env))
            if not token:
                raise RuntimeError(
                    f"A2A authentication environment variable is not set: {token_env}"
                )
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _validate_url(url: str, config: Dict[str, Any], label: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError(f"{label} URL must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and not config.get("allow_insecure_http", False):
            raise RuntimeError(
                f"{label} URL must use HTTPS; enable allow_insecure_http only for local testing"
            )

    def _select_rest_endpoint(
        self, card: Dict[str, Any], card_url: str, config: Dict[str, Any]
    ) -> str:
        interfaces = card.get("supportedInterfaces")
        if not isinstance(interfaces, list):
            raise RuntimeError("A2A Agent Card is missing supportedInterfaces")
        selected = next(
            (
                item
                for item in interfaces
                if str(item.get("protocolBinding", "")).upper() == "HTTP+JSON"
                and str(item.get("protocolVersion", "")) == "1.0"
            ),
            None,
        )
        if selected is None or not selected.get("url"):
            raise RuntimeError("A2A Agent Card does not advertise an HTTP+JSON 1.0 interface")
        endpoint = urljoin(card_url, str(selected["url"]))
        self._validate_url(endpoint, config, "A2A endpoint")
        card_origin = urlparse(card_url)
        endpoint_origin = urlparse(endpoint)
        if (card_origin.scheme, card_origin.hostname, card_origin.port) != (
            endpoint_origin.scheme,
            endpoint_origin.hostname,
            endpoint_origin.port,
        ) and not config.get("allow_cross_origin_endpoint", False):
            raise RuntimeError("A2A endpoint origin differs from Agent Card origin")
        return endpoint

    @staticmethod
    def _validate_skill(card: Dict[str, Any], skill_id: Any) -> None:
        if not skill_id:
            return
        skills = card.get("skills") or []
        if not any(item.get("id") == skill_id for item in skills):
            raise RuntimeError(f"A2A Agent Card does not advertise configured skill: {skill_id}")

    def _json_request(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        headers: Dict[str, str],
        config: Dict[str, Any],
        is_cancelled: Optional[CancellationCheck],
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        retries = int(config.get("max_retries", 2))
        for attempt in range(retries + 1):
            if is_cancelled is not None and is_cancelled():
                raise ProviderCancelled("A2A task cancelled")
            try:
                response = client.request(method, url, headers=headers, json=json_body)
                if response.status_code not in {429, 502, 503, 504}:
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise RuntimeError("A2A response must be a JSON object")
                    return payload
                error = f"HTTP {response.status_code}"
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:500].strip()
                raise RuntimeError(
                    f"A2A {method} {url} failed with HTTP {exc.response.status_code}: {detail}"
                ) from exc
            except (ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"A2A {method} {url} returned invalid JSON") from exc
            if attempt == retries:
                raise RuntimeError(
                    f"A2A {method} {url} failed after {attempt + 1} attempts: {error}"
                )
            self._wait(float(config.get("retry_backoff", 0.25)) * (2**attempt), is_cancelled)
        raise AssertionError("unreachable")

    @staticmethod
    def _wait(seconds: float, is_cancelled: Optional[CancellationCheck]) -> None:
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            if is_cancelled is not None and is_cancelled():
                raise ProviderCancelled("A2A task cancelled")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def _cancel_remote(
        self,
        client: httpx.Client,
        endpoint: str,
        task_id: str,
        headers: Dict[str, str],
        config: Dict[str, Any],
    ) -> None:
        self._json_request(
            client,
            "POST",
            endpoint.rstrip("/") + f"/tasks/{quote(task_id, safe='')}:cancel",
            headers,
            {**config, "max_retries": 0},
            None,
            json_body={},
        )

    @staticmethod
    def _unwrap(
        payload: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        task = payload.get("task")
        message = payload.get("message")
        return (
            task if isinstance(task, dict) else None,
            message if isinstance(message, dict) else None,
        )

    @staticmethod
    def _record_task(
        remote_task: Dict[str, Any], on_event: AgentEventHandler, seen: set[str]
    ) -> str:
        status = remote_task.get("status") or {}
        state = str(status.get("state", "TASK_STATE_UNSPECIFIED"))
        fingerprint = f"status:{state}:{status.get('timestamp', '')}"
        if fingerprint not in seen:
            seen.add(fingerprint)
            on_event(
                "agent.status",
                {
                    "protocol": "a2a",
                    "remote_task_id": remote_task.get("id"),
                    "context_id": remote_task.get("contextId"),
                    "state": state,
                },
            )
        return state

    def _task_output(
        self, remote_task: Dict[str, Any], on_event: AgentEventHandler, seen: set[str]
    ) -> Any:
        parts: List[Dict[str, Any]] = []
        for artifact in remote_task.get("artifacts") or []:
            parts.extend(artifact.get("parts") or [])
        if not parts:
            parts = ((remote_task.get("status") or {}).get("message") or {}).get("parts") or []
        return self._output_from_parts(parts, on_event, seen)

    @staticmethod
    def _output_from_parts(
        parts: Iterable[Dict[str, Any]], on_event: AgentEventHandler, seen: set[str]
    ) -> Any:
        structured: List[Any] = []
        texts: List[str] = []
        for part in parts:
            fingerprint = json.dumps(part, sort_keys=True, ensure_ascii=False)
            if fingerprint not in seen:
                seen.add(fingerprint)
                if "data" in part:
                    on_event("agent.data", {"protocol": "a2a", "data": part["data"]})
                elif "text" in part:
                    on_event("agent.text", {"protocol": "a2a", "text": str(part["text"])})
                else:
                    on_event("agent.artifact", {"protocol": "a2a", "part": part})
            if "data" in part:
                structured.append(part["data"])
            elif "text" in part:
                texts.append(str(part["text"]))
        if structured:
            return structured[0] if len(structured) == 1 else structured
        return "\n".join(texts).strip()

    @staticmethod
    def _validated_output(value: Any, config: Dict[str, Any]) -> str:
        schema = config.get("output_schema")
        if schema is not None:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "A2A output is not valid JSON required by output_schema"
                    ) from exc
            try:
                from jsonschema import ValidationError, validate
            except ModuleNotFoundError as exc:
                raise RuntimeError("A2A output validation requires the jsonschema package") from exc
            try:
                validate(instance=value, schema=schema)
            except ValidationError as exc:
                path = ".".join(str(item) for item in exc.absolute_path) or "<root>"
                raise RuntimeError(
                    f"A2A output schema validation failed at {path}: {exc.message}"
                ) from exc
        return A2aProvider._string_output(value)

    @staticmethod
    def _string_output(value: Any) -> str:
        if isinstance(value, str):
            if not value:
                raise RuntimeError("A2A response contains no text or structured artifact")
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _status_text(remote_task: Dict[str, Any]) -> str:
        parts = ((remote_task.get("status") or {}).get("message") or {}).get("parts") or []
        return "\n".join(str(part["text"]) for part in parts if "text" in part)

    @staticmethod
    def _input_part(
        agent: Agent,
        issue: Optional[Issue],
        task: Task,
        chat: Optional[ChatBrief] = None,
    ) -> Dict[str, Any]:
        prompt = chat.prompt if chat is not None else build_task_prompt(issue, task)
        if agent.instructions:
            prompt = f"{agent.instructions}\n\n{prompt}"
        part: Dict[str, Any] = {"text": prompt}
        schema = agent.provider_config.get("output_schema")
        if schema is not None:
            part["metadata"] = {"mediaType": "application/json", "schema": schema}
        return part

    @staticmethod
    def _encode_session(context_id: Any, task_id: Any, state: str) -> Optional[str]:
        if not context_id and not task_id:
            return None
        raw = json.dumps(
            {"context_id": context_id, "task_id": task_id, "state": state},
            separators=(",", ":"),
        ).encode("utf-8")
        return "a2a1:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_session(value: Optional[str]) -> Dict[str, Any]:
        if not value:
            return {}
        if not value.startswith("a2a1:"):
            return {"context_id": value}
        encoded = value.removeprefix("a2a1:")
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored A2A session token is invalid") from exc
        return payload if isinstance(payload, dict) else {}
