from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from mica.domain import AssigneeType, IssueStatus


class CreateIssueRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    assignee_id: str = "local-assistant"
    assignee_type: AssigneeType = AssigneeType.AGENT
    project_path: Optional[str] = None
    project_id: Optional[str] = None


class CreateCommentRequest(BaseModel):
    body: str = Field(min_length=1)
    author_id: str = Field(min_length=1)
    parent_comment_id: Optional[str] = None


class UpdateIssueStatusRequest(BaseModel):
    status: IssueStatus


class RunDaemonResponse(BaseModel):
    executed: bool


class CreateRuntimeRequest(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    max_slots: int = Field(default=1, ge=1, le=32)


class UpdateRuntimeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    max_slots: int = Field(default=1, ge=1, le=32)


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    local_path: str = Field(min_length=1)
    workspace_id: str = Field(default="local", min_length=1)
    runtime_id: str = Field(default="local", min_length=1)
    execution_mode: str = Field(default="worktree", pattern="^(in_place|worktree)$")


class UpdateProjectRequest(BaseModel):
    execution_mode: str = Field(pattern="^(in_place|worktree)$")


class CreateAgentRequest(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    runtime_id: str = Field(min_length=1)
    provider: str = Field(pattern="^(codex|claude|pi|acp|a2a|stub)$")
    instructions: str = ""
    provider_config: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_provider_config(self):
        config = self.provider_config
        if self.provider == "acp":
            command = config.get("command")
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(item, str) and item for item in command)
            ):
                raise ValueError("ACP provider_config.command must be a non-empty string array")
            if config.get("permission_policy", "deny") not in {"allow", "deny"}:
                raise ValueError("ACP permission_policy must be allow or deny")
        if self.provider == "a2a":
            if not isinstance(config.get("agent_card_url"), str):
                raise ValueError("A2A provider_config.agent_card_url is required")
            schema = config.get("output_schema")
            if schema is not None:
                if not isinstance(schema, dict):
                    raise ValueError("A2A output_schema must be a JSON object")
                try:
                    from jsonschema.validators import validator_for

                    validator_for(schema).check_schema(schema)
                except Exception as exc:
                    raise ValueError(f"invalid A2A output_schema: {exc}") from exc
        return self


class SquadMemberRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=100)
    role: str = Field(default="", max_length=100)
    capabilities: List[str] = Field(default_factory=list, max_length=30)


class CreateSquadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1)
    leader_agent_id: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    instructions: str = Field(default="", max_length=5000)
    members: List[SquadMemberRequest] = Field(default_factory=list, max_length=30)


class UpdateSquadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    leader_agent_id: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    instructions: str = Field(default="", max_length=5000)
