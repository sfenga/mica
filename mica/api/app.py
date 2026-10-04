from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from importlib.resources import files
from queue import Empty
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mica.application import IdempotencyConflictError, IngressCommand
from mica.application.services import InvalidStateError, NotFoundError
from mica.bootstrap import ApplicationContainer, build_container_from_env
from mica.domain import Agent, Runtime, TaskStatus

from .schemas import (
    CreateAgentRequest,
    CreateCommentRequest,
    CreateIssueRequest,
    CreateProjectRequest,
    CreateRuntimeRequest,
    CreateSquadRequest,
    RunDaemonResponse,
    SquadMemberRequest,
    UpdateIssueStatusRequest,
    UpdateProjectRequest,
    UpdateRuntimeRequest,
    UpdateSquadRequest,
)


def _document(value: object) -> Dict[str, Any]:
    return asdict(value)  # FastAPI's encoder handles enums and datetimes.


def create_app(container: Optional[ApplicationContainer] = None) -> FastAPI:
    state = container or build_container_from_env()
    embedded_default = "1" if state.database is None else "0"
    embedded_daemon = os.getenv("MICA_EMBEDDED_DAEMON", embedded_default) == "1"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if state.event_relay is not None:
            state.event_relay.start()
        state.channel_worker.start()
        if embedded_daemon:
            state.worker.start()
        try:
            yield
        finally:
            if embedded_daemon:
                state.worker.stop()
            state.channel_worker.stop()
            if state.event_relay is not None:
                state.event_relay.stop()

    app = FastAPI(title="Mica", version="0.5.0", lifespan=lifespan)
    app.state.container = state
    web_root = files("mica.web")
    app.mount("/assets", StaticFiles(directory=str(web_root)), name="web-assets")

    @app.get("/")
    def root() -> Dict[str, str]:
        return {"service": "mica", "status": "ok"}

    @app.get("/health/live")
    def liveness() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness() -> Response:
        try:
            if state.database is not None:
                state.database.ping()
            if state.runtimes.get(state.daemon.runtime_id) is None:
                raise RuntimeError(f"runtime is not configured: {state.daemon.runtime_id}")
            for agent in state.agents.list():
                if state.runtimes.get(agent.runtime_id) is None:
                    raise RuntimeError(
                        f"agent {agent.id} references missing runtime {agent.runtime_id}"
                    )
                if not state.providers.contains(agent.provider):
                    raise RuntimeError(
                        f"agent {agent.id} references unknown provider {agent.provider}"
                    )
        except Exception as exc:
            return JSONResponse(
                status_code=503, content={"status": "not_ready", "detail": str(exc)}
            )
        return JSONResponse(content={"status": "ready"})

    @app.get("/app", response_class=HTMLResponse)
    def web_console() -> str:
        return files("mica.web").joinpath("index.html").read_text(encoding="utf-8")

    @app.post("/api/issues", status_code=201)
    def create_issue(
        request: CreateIssueRequest,
        response: Response,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        user_id: str = Header(default="local-user", alias="X-Mica-User"),
        user_name: str = Header(default="Local User", alias="X-Mica-User-Name"),
    ) -> Dict[str, Any]:
        try:
            project_path = request.project_path
            project_execution_mode = "worktree" if project_path else None
            if request.project_id:
                project = state.project_service.get(request.project_id)
                if project.workspace_id != request.workspace_id:
                    raise NotFoundError(
                        f"project not found in workspace: {request.project_id}"
                    )
                project_path = project.local_path
                project_execution_mode = project.execution_mode
            if request.assignee_type.value == "agent":
                assigned_agent = state.agents.get(request.assignee_id)
            else:
                squad = state.squads.get(request.assignee_id)
                assigned_agent = state.agents.get(squad.leader_agent_id) if squad else None
            if assigned_agent is None:
                raise NotFoundError(f"assignee not found: {request.assignee_id}")
            if assigned_agent.provider in {"codex", "claude", "pi", "acp"}:
                if request.project_id and project.runtime_id != assigned_agent.runtime_id:
                    raise ValueError("project and agent must use the same runtime")
            issue, created = state.ingress_service.submit(
                IngressCommand(
                    channel="web",
                    tenant_key=request.workspace_id,
                    external_event_id=idempotency_key or f"web-{uuid4().hex}",
                    external_user_id=user_id,
                    display_name=user_name,
                    conversation_id=user_id,
                    title=request.title,
                    body=request.body,
                    workspace_id=request.workspace_id,
                    assignee_id=request.assignee_id,
                    assignee_type=request.assignee_type,
                    project_path=project_path,
                    project_execution_mode=project_execution_mode,
                )
            )
            if not created:
                response.status_code = 200
            state.worker.wake()
            return _document(issue)
        except NotFoundError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/integrations/feishu/events")
    async def receive_feishu_event(request: Request) -> Response:
        if state.feishu_webhook is None:
            raise HTTPException(status_code=503, detail="Feishu integration is not configured")
        body = await request.body()
        status, headers, content = await asyncio.to_thread(
            state.feishu_webhook.handle, str(request.url), request.headers, body
        )
        if status < 300:
            state.worker.wake()
            state.channel_worker.wake()
        return Response(content=content, status_code=status, headers=headers)

    @app.get("/api/issues")
    def list_issues():
        return [_document(issue) for issue in state.issue_service.list()]

    @app.get("/api/issues/{issue_id}")
    def get_issue(issue_id: str) -> Dict[str, Any]:
        try:
            return _document(state.issue_service.get(issue_id))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/issues/{issue_id}/comments", status_code=201)
    def add_comment(issue_id: str, request: CreateCommentRequest) -> Dict[str, Any]:
        try:
            comment = state.trigger_service.add_comment(
                issue_id,
                request.body,
                request.author_id,
                request.parent_comment_id,
            )
            state.worker.wake()
            return _document(comment)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/api/issues/{issue_id}/status")
    def update_issue_status(issue_id: str, request: UpdateIssueStatusRequest) -> Dict[str, Any]:
        try:
            return _document(state.trigger_service.transition_issue(issue_id, request.status))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/tasks")
    def list_tasks(issue_id: Optional[str] = None):
        return [_document(task) for task in state.tasks.list(issue_id)]

    @app.get("/api/projects")
    def list_projects(workspace_id: Optional[str] = None):
        return [_document(project) for project in state.project_service.list(workspace_id)]

    @app.get("/api/project-candidates")
    def list_project_candidates():
        return [_document(candidate) for candidate in state.project_service.discover()]

    @app.post("/api/projects", status_code=201)
    def create_project(request: CreateProjectRequest):
        try:
            project = state.project_service.register(
                request.name,
                request.local_path,
                request.workspace_id,
                request.runtime_id,
                request.execution_mode,
            )
            return _document(project)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: str, request: UpdateProjectRequest):
        try:
            return _document(
                state.project_service.set_execution_mode(project_id, request.execution_mode)
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/runtimes")
    def list_runtimes():
        return [_document(runtime) for runtime in state.runtimes.list()]

    @app.get("/api/runtime-capabilities")
    def list_runtime_capabilities(runtime_id: Optional[str] = None):
        return [
            _document(item) for item in state.runtime_capabilities.list(runtime_id)
        ]

    @app.post("/api/runtimes", status_code=201)
    def create_runtime(request: CreateRuntimeRequest) -> Dict[str, Any]:
        runtime = state.runtimes.add(
            Runtime(id=request.id, name=request.name, max_slots=request.max_slots)
        )
        state.slots.configure(runtime.id, runtime.max_slots)
        return _document(runtime)

    @app.patch("/api/runtimes/{runtime_id}")
    def update_runtime(runtime_id: str, request: UpdateRuntimeRequest) -> Dict[str, Any]:
        runtime = state.runtimes.get(runtime_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail=f"runtime not found: {runtime_id}")
        updated = state.runtimes.add(
            replace(runtime, name=request.name, max_slots=request.max_slots)
        )
        state.slots.configure(updated.id, updated.max_slots)
        return _document(updated)

    @app.post("/api/runtimes/{runtime_id}/probe")
    def probe_runtime(runtime_id: str):
        try:
            return [
                _document(item) for item in state.runtime_discovery.probe(runtime_id)
            ]
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/agents")
    def list_agents():
        return [_document(agent) for agent in state.agents.list()]

    @app.post("/api/agents", status_code=201)
    def create_agent(request: CreateAgentRequest) -> Dict[str, Any]:
        if state.runtimes.get(request.runtime_id) is None:
            raise HTTPException(status_code=422, detail=f"runtime not found: {request.runtime_id}")
        if not state.providers.contains(request.provider):
            raise HTTPException(
                status_code=422, detail=f"provider not registered: {request.provider}"
            )
        existing = state.agents.get(request.id)
        active_statuses = {
            TaskStatus.QUEUED,
            TaskStatus.DISPATCHED,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_INPUT,
        }
        if existing is not None and any(
            task.agent_id == request.id and task.status in active_statuses
            for task in state.tasks.list()
        ):
            requested_identity = (
                request.name,
                request.runtime_id,
                request.provider,
                request.instructions,
            )
            existing_identity = (
                existing.name,
                existing.runtime_id,
                existing.provider,
                existing.instructions,
            )
            if requested_identity != existing_identity:
                raise HTTPException(
                    status_code=409,
                    detail="agent configuration cannot change while it has active tasks",
                )
        provider_config = dict(request.provider_config)
        if request.provider == "claude":
            # Claude runs non-interactively. The adapter owns its tool approval
            # protocol; persisted per-agent modes would imply an unavailable UI.
            provider_config.pop("permission_mode", None)
        capability = state.runtime_capabilities.get(request.runtime_id, request.provider)
        if request.provider in {"codex", "claude", "pi"}:
            if capability is None or capability.status != "available":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"provider {request.provider} is not available on runtime "
                        f"{request.runtime_id}; probe the runtime first"
                    ),
                )
            provider_config.setdefault("executable", capability.executable_path)
        candidate = Agent(
            id=request.id,
            name=request.name,
            runtime_id=request.runtime_id,
            provider=request.provider,
            instructions=request.instructions,
            provider_config=provider_config,
        )
        if (
            existing is not None
            and existing != candidate
            and any(
                task.agent_id == request.id and task.status in active_statuses
                for task in state.tasks.list()
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="agent configuration cannot change while it has active tasks",
            )
        agent = state.agents.add(candidate)
        return _document(agent)

    @app.get("/api/squads")
    def list_squads(workspace_id: Optional[str] = None):
        return [
            {
                **_document(squad),
                "members": [_document(item) for item in state.squad_service.members(squad.id)],
            }
            for squad in state.squad_service.list(workspace_id)
        ]

    @app.get("/api/squads/{squad_id}")
    def get_squad(squad_id: str):
        try:
            squad = state.squad_service.get(squad_id)
            return {
                **_document(squad),
                "members": [_document(item) for item in state.squad_service.members(squad.id)],
            }
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/squads", status_code=201)
    def create_squad(request: CreateSquadRequest):
        try:
            squad = state.squad_service.create(
                name=request.name,
                workspace_id=request.workspace_id,
                leader_agent_id=request.leader_agent_id,
                description=request.description,
                instructions=request.instructions,
                members=[item.model_dump() for item in request.members],
            )
            return {
                **_document(squad),
                "members": [_document(item) for item in state.squad_service.members(squad.id)],
            }
        except (NotFoundError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/squads/{squad_id}/members", status_code=201)
    def add_squad_member(squad_id: str, request: SquadMemberRequest):
        try:
            return _document(
                state.squad_service.add_member(
                    squad_id,
                    request.agent_id,
                    request.role,
                    request.capabilities,
                )
            )
        except (NotFoundError, InvalidStateError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/squads/{squad_id}")
    def update_squad(squad_id: str, request: UpdateSquadRequest):
        try:
            squad = state.squad_service.update(
                squad_id,
                name=request.name,
                leader_agent_id=request.leader_agent_id,
                description=request.description,
                instructions=request.instructions,
            )
            return {
                **_document(squad),
                "members": [_document(item) for item in state.squad_service.members(squad.id)],
            }
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidStateError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/squads/{squad_id}/members/{agent_id}", status_code=204)
    def remove_squad_member(squad_id: str, agent_id: str) -> Response:
        try:
            state.squad_service.remove_member(squad_id, agent_id)
            return Response(status_code=204)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidStateError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}/events")
    def list_task_events(
        task_id: str,
        include_internal: bool = True,
        limit: Optional[int] = None,
    ):
        if state.tasks.get(task_id) is None:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        events = state.events.list(task_id)
        if not include_internal:
            events = [
                event
                for event in events
                if not (
                    event.event_type == "agent.system"
                    and event.payload.get("subtype")
                    in {"thinking_tokens", "hook_started", "hook_response"}
                )
            ]
        if limit is not None:
            normalized_limit = max(0, min(limit, 1000))
            events = events[-normalized_limit:] if normalized_limit else []
        return [_document(event) for event in events]

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> Dict[str, Any]:
        try:
            task = state.task_service.request_cancel(task_id)
            if task.status.value == "cancelled" and task.squad_id:
                state.squad_service.handle_worker_terminal(task)
            return _document(task)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/daemon/run-once", response_model=RunDaemonResponse)
    def run_daemon_once() -> RunDaemonResponse:
        return RunDaemonResponse(executed=state.daemon.run_once())

    @app.websocket("/api/tasks/{task_id}/events")
    async def task_events(websocket: WebSocket, task_id: str) -> None:
        if state.tasks.get(task_id) is None:
            await websocket.close(code=4404, reason="task not found")
            return
        await websocket.accept()
        channel = state.event_bus.subscribe(task_id)
        delivered = set()
        try:
            for event in state.events.list(task_id):
                await websocket.send_json(jsonable_encoder(_document(event)))
                delivered.add(event.id)
            while True:
                try:
                    event = await asyncio.to_thread(channel.get, True, 15)
                    if event.id in delivered:
                        continue
                    delivered.add(event.id)
                    await websocket.send_json(jsonable_encoder(_document(event)))
                except Empty:
                    await websocket.send_json({"event_type": "heartbeat", "task_id": task_id})
        except WebSocketDisconnect:
            pass
        finally:
            state.event_bus.unsubscribe(task_id, channel)

    return app
