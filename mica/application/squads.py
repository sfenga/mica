from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any, Optional

from mica.domain import Agent, IssueStatus, Squad, SquadMember, Task, TaskStatus, utc_now

from .ports import (
    AgentRepository,
    IssueRepository,
    SquadRepository,
    TaskRepository,
    TransactionManager,
)
from .services import InvalidStateError, NotFoundError, TaskService


@dataclass(frozen=True)
class SquadAssignment:
    agent_id: str
    instruction: str


@dataclass(frozen=True)
class LeaderDecision:
    decision: str
    summary: str
    assignments: tuple[SquadAssignment, ...] = ()


class SquadService:
    def __init__(
        self,
        squads: SquadRepository,
        agents: AgentRepository,
        issues: IssueRepository,
        tasks: TaskRepository,
        task_service: TaskService,
        transactions: Optional[TransactionManager] = None,
    ) -> None:
        self.squads = squads
        self.agents = agents
        self.issues = issues
        self.tasks = tasks
        self.task_service = task_service
        self.transactions = transactions

    def _transaction(self):
        return self.transactions.transaction() if self.transactions else nullcontext()

    def create(
        self,
        name: str,
        workspace_id: str,
        leader_agent_id: str,
        members: list[dict[str, Any]],
        description: str = "",
        instructions: str = "",
    ) -> Squad:
        self._agent(leader_agent_id)
        normalized_members = []
        seen = {leader_agent_id}
        for item in members:
            agent_id = str(item["agent_id"])
            if agent_id in seen:
                continue
            self._agent(agent_id)
            seen.add(agent_id)
            normalized_members.append(
                SquadMember(
                    squad_id="",
                    agent_id=agent_id,
                    role=str(item.get("role") or ""),
                    capabilities=[str(value) for value in item.get("capabilities", [])],
                )
            )
        with self._transaction():
            squad = self.squads.add(
                Squad(
                    name=name,
                    workspace_id=workspace_id,
                    leader_agent_id=leader_agent_id,
                    description=description,
                    instructions=instructions,
                )
            )
            self.squads.add_member(
                SquadMember(squad_id=squad.id, agent_id=leader_agent_id, role="leader")
            )
            for member in normalized_members:
                self.squads.add_member(
                    SquadMember(
                        squad_id=squad.id,
                        agent_id=member.agent_id,
                        role=member.role,
                        capabilities=member.capabilities,
                    )
                )
        return squad

    def get(self, squad_id: str) -> Squad:
        squad = self.squads.get(squad_id)
        if squad is None:
            raise NotFoundError(f"squad not found: {squad_id}")
        return squad

    def list(self, workspace_id: Optional[str] = None) -> list[Squad]:
        return self.squads.list(workspace_id)

    def members(self, squad_id: str) -> list[SquadMember]:
        self.get(squad_id)
        return self.squads.list_members(squad_id)

    def add_member(
        self, squad_id: str, agent_id: str, role: str = "", capabilities: Optional[list[str]] = None
    ) -> SquadMember:
        squad = self.get(squad_id)
        self._agent(agent_id)
        return self.squads.add_member(
            SquadMember(
                squad_id=squad.id,
                agent_id=agent_id,
                role=role,
                capabilities=list(capabilities or []),
            )
        )

    def update(
        self,
        squad_id: str,
        name: str,
        leader_agent_id: str,
        description: str = "",
        instructions: str = "",
    ) -> Squad:
        squad = self.get(squad_id)
        self._ensure_idle(squad_id)
        self._agent(leader_agent_id)
        members = {member.agent_id: member for member in self.members(squad_id)}
        previous_leader = members.get(squad.leader_agent_id)
        with self._transaction():
            if previous_leader is not None and previous_leader.agent_id != leader_agent_id:
                self.squads.add_member(
                    replace(previous_leader, role="member")
                )
            new_leader = members.get(leader_agent_id)
            self.squads.add_member(
                replace(new_leader, role="leader")
                if new_leader is not None
                else SquadMember(squad_id=squad_id, agent_id=leader_agent_id, role="leader")
            )
            updated = replace(
                squad,
                name=name,
                leader_agent_id=leader_agent_id,
                description=description,
                instructions=instructions,
                updated_at=utc_now(),
            )
            return self.squads.save(updated)

    def remove_member(self, squad_id: str, agent_id: str) -> None:
        squad = self.get(squad_id)
        if agent_id == squad.leader_agent_id:
            raise ValueError("squad leader cannot be removed")
        self._ensure_idle(squad_id)
        if not self.squads.remove_member(squad_id, agent_id):
            raise NotFoundError(f"agent {agent_id} is not a member of squad {squad_id}")

    def _ensure_idle(self, squad_id: str) -> None:
        active = {
            TaskStatus.QUEUED,
            TaskStatus.DISPATCHED,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_INPUT,
        }
        if any(task.squad_id == squad_id and task.status in active for task in self.tasks.list()):
            raise InvalidStateError("squad configuration cannot change while it has active tasks")

    def briefing(self, squad_id: str, issue_id: Optional[str] = None) -> str:
        squad = self.get(squad_id)
        lines = [
            "## Squad leader role",
            "",
            "You coordinate this task. Choose suitable squad members; do not silently do all work yourself.",
            "Return exactly one JSON object, without Markdown fences:",
            '{"decision":"delegate|complete","summary":"...","assignments":[{"agent_id":"...","instruction":"..."}]}',
            "Use decision=delegate with one or more assignments, complete only after the overall result is ready for human review,",
            "and assign only agents listed below.",
            "",
            "## Squad roster",
        ]
        for member in self.squads.list_members(squad.id):
            agent = self.agents.get(member.agent_id)
            if agent is None:
                continue
            role = f"; role={member.role}" if member.role else ""
            capabilities = ", ".join(member.capabilities) or "not specified"
            leader = "; leader" if member.agent_id == squad.leader_agent_id else ""
            lines.append(
                f"- {agent.name} (agent_id={agent.id}{leader}{role}; capabilities={capabilities})"
            )
        if squad.instructions.strip():
            lines.extend(["", f"## Squad instructions ({squad.name})", squad.instructions.strip()])
        if issue_id:
            results = [
                task
                for task in self.tasks.list(issue_id)
                if task.squad_id == squad.id
                and not task.is_leader_task
                and task.status
                in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.WAITING_INPUT,
                }
            ][-20:]
            if results:
                lines.extend(["", "## Recent delegated results"])
                lines.extend(
                    f"- {task.agent_id}: {task.status.value}: "
                    f"{(task.output or task.error or '')[:500]}"
                    for task in results
                )
        return "\n".join(lines)

    def _parse_and_validate_leader_decision(self, task: Task, output: str) -> LeaderDecision:
        decision = self._parse_decision(output)
        squad = self.get(task.squad_id or "")
        allowed = {
            member.agent_id
            for member in self.squads.list_members(squad.id)
            if member.agent_id != squad.leader_agent_id
        }
        if decision.decision == "delegate":
            if not decision.assignments:
                raise InvalidStateError("squad leader delegate decision requires assignments")
            seen = set()
            for assignment in decision.assignments:
                if assignment.agent_id not in allowed:
                    raise InvalidStateError(
                        f"agent {assignment.agent_id} is not a delegatable member of squad {squad.id}"
                    )
                if assignment.agent_id in seen:
                    raise InvalidStateError(f"duplicate squad assignment: {assignment.agent_id}")
                if not assignment.instruction:
                    raise InvalidStateError("squad assignment instruction cannot be empty")
                seen.add(assignment.agent_id)
        elif decision.assignments:
            raise InvalidStateError(f"{decision.decision} decision cannot contain assignments")
        return decision

    def complete_task(
        self,
        task: Task,
        output: str,
        session_id: Optional[str],
        worker_id: str,
        attempt: int,
    ) -> None:
        decision = (
            self._parse_and_validate_leader_decision(task, output) if task.is_leader_task else None
        )
        with self._transaction():
            self.task_service.complete(
                task.id,
                output,
                session_id,
                worker_id,
                attempt,
                advance_issue=False,
                publish_comment=not task.is_leader_task or decision.decision == "complete",
                comment_body=decision.summary if task.is_leader_task else output,
            )
            if task.is_leader_task:
                assert decision is not None
                self._apply_leader_decision(task, decision)
            else:
                self._wake_leader_after_workers(task)

    def fail_task(self, task: Task, error: str, worker_id: str, attempt: int) -> None:
        with self._transaction():
            self.task_service.fail(
                task.id,
                error,
                worker_id,
                attempt,
                advance_issue=task.is_leader_task,
            )
            self.handle_worker_terminal(task)

    def handle_worker_terminal(self, task: Task) -> None:
        if task.squad_id and not task.is_leader_task:
            self._wake_leader_after_workers(task)

    def _apply_leader_decision(self, task: Task, decision: LeaderDecision) -> None:
        squad = self.get(task.squad_id or "")
        with self._transaction():
            issue = self.issues.get_for_update(task.issue_id)
            if issue is None:
                raise NotFoundError(f"issue not found: {task.issue_id}")
            self.task_service.record(
                task.id,
                "squad.leader_decision",
                {
                    "squad_id": squad.id,
                    "decision": decision.decision,
                    "summary": decision.summary,
                    "assignments": [assignment.__dict__ for assignment in decision.assignments],
                },
            )
            if decision.decision == "complete":
                issue.status = IssueStatus.IN_REVIEW
                issue.updated_at = utc_now()
                self.issues.save(issue)
                self.task_service.record(
                    task.id,
                    "squad.completed",
                    {"squad_id": squad.id, "summary": decision.summary},
                )
                return
            for assignment in decision.assignments:
                agent = self._agent(assignment.agent_id)
                self.task_service.enqueue(
                    issue,
                    agent.id,
                    agent.runtime_id,
                    "squad.delegated",
                    task.id,
                    squad_id=squad.id,
                    delegated_from_task_id=task.id,
                    handoff_note=assignment.instruction,
                )

    def _wake_leader_after_workers(self, task: Task) -> None:
        if not task.delegated_from_task_id:
            return
        squad = self.get(task.squad_id or "")
        with self._transaction():
            issue = self.issues.get_for_update(task.issue_id)
            if issue is None or issue.status in {IssueStatus.DONE, IssueStatus.CANCELLED}:
                return
            siblings = [
                item
                for item in self.tasks.list(task.issue_id)
                if item.delegated_from_task_id == task.delegated_from_task_id
            ]
            # A member waiting for input has yielded its slot. The leader must be
            # able to review that request and either ask the user or delegate a
            # continuation; treating it as active would deadlock the squad batch.
            active = {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.RUNNING}
            if any(item.status in active for item in siblings):
                return
            if self.task_service.has_pending(issue.id, squad.leader_agent_id, True):
                return
            leader = self._agent(squad.leader_agent_id)
            leader_task = self.task_service.enqueue(
                issue,
                leader.id,
                leader.runtime_id,
                "squad.workers_finished",
                task.delegated_from_task_id,
                squad_id=squad.id,
                is_leader_task=True,
                handoff_note="Delegated members finished. Review the latest delegated results.",
            )
            self.task_service.record(
                leader_task.id, "squad.leader_reawakened", {"squad_id": squad.id}
            )

    @staticmethod
    def _parse_decision(output: str) -> LeaderDecision:
        text = output.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidStateError("squad leader output must be one JSON object") from exc
        if not isinstance(value, dict) or value.get("decision") not in {"delegate", "complete"}:
            raise InvalidStateError("squad leader decision must be delegate or complete")
        raw_assignments = value.get("assignments") or []
        if not isinstance(raw_assignments, list):
            raise InvalidStateError("squad leader assignments must be an array")
        if not all(isinstance(item, dict) for item in raw_assignments):
            raise InvalidStateError("each squad leader assignment must be an object")
        assignments = tuple(
            SquadAssignment(str(item.get("agent_id", "")), str(item.get("instruction", "")).strip())
            for item in raw_assignments
        )
        return LeaderDecision(str(value["decision"]), str(value.get("summary") or ""), assignments)

    def _agent(self, agent_id: str) -> Agent:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise NotFoundError(f"agent not found: {agent_id}")
        return agent
