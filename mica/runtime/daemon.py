from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Dict, Optional

from mica.application.chat import build_chat_prompt, resolve_chat_resume
from mica.application.ports import AgentRepository, ChatRepository, IssueRepository
from mica.application.services import InvalidStateError, NotFoundError, TaskService
from mica.application.squads import SquadService
from mica.domain import TaskStatus

from .providers import ChatBrief, ProviderCancelled, ProviderRegistry
from .workspace import WorkspaceManager
from .worktree import WorktreeManager

LOCAL_WORKDIR_PROVIDERS = {"codex", "claude", "pi", "acp"}


class ChatInputUnavailable(RuntimeError):
    """The chat batch could not be read: the task is preserved for redelivery
    instead of executing on an empty input (a read failure must never
    masquerade as zero input)."""


class LocalDirectoryLockPool:
    """Serializes in-place tasks that share the same local directory."""

    def __init__(self) -> None:
        self._locks: Dict[str, Lock] = {}
        self._guard = Lock()

    def acquire(self, path: str, is_cancelled) -> Lock:
        path = str(Path(path).expanduser().resolve())
        with self._guard:
            lock = self._locks.setdefault(path, Lock())
        while not lock.acquire(timeout=0.2):
            if is_cancelled():
                raise ProviderCancelled("task cancelled while waiting for the project directory")
        return lock


class RuntimeSlotPool:
    """Reserves execution capacity before a task is claimed."""

    def __init__(self, capacities: Optional[Dict[str, int]] = None) -> None:
        self._capacities = dict(capacities or {})
        self._in_use: Dict[str, int] = {}
        self._lock = Lock()

    def configure(self, runtime_id: str, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("runtime capacity must be at least 1")
        with self._lock:
            self._capacities[runtime_id] = capacity

    def capacity(self, runtime_id: str) -> int:
        with self._lock:
            return self._capacities.get(runtime_id, 1)

    def acquire(self, runtime_id: str) -> bool:
        with self._lock:
            capacity = self._capacities.get(runtime_id, 1)
            in_use = self._in_use.get(runtime_id, 0)
            if in_use >= capacity:
                return False
            self._in_use[runtime_id] = in_use + 1
            return True

    def release(self, runtime_id: str) -> None:
        with self._lock:
            in_use = self._in_use.get(runtime_id, 0)
            if in_use <= 1:
                self._in_use.pop(runtime_id, None)
            else:
                self._in_use[runtime_id] = in_use - 1


class Daemon:
    def __init__(
        self,
        worker_id: str,
        runtime_id: str,
        tasks: TaskService,
        issues: IssueRepository,
        agents: AgentRepository,
        providers: ProviderRegistry,
        slots: RuntimeSlotPool,
        worktrees: Optional[WorktreeManager] = None,
        workspaces: Optional[WorkspaceManager] = None,
        directory_locks: Optional[LocalDirectoryLockPool] = None,
        lease_seconds: int = 120,
        heartbeat_seconds: int = 30,
        squads: Optional[SquadService] = None,
        chats: Optional[ChatRepository] = None,
        chat_transcript_limit: int = 20,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than the lease")
        self.worker_id = worker_id
        self.runtime_id = runtime_id
        self.tasks = tasks
        self.issues = issues
        self.agents = agents
        self.providers = providers
        self.slots = slots
        self.worktrees = worktrees
        self.workspaces = workspaces
        self.directory_locks = directory_locks or LocalDirectoryLockPool()
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.squads = squads
        self.chats = chats
        self.chat_transcript_limit = chat_transcript_limit
        self._shutdown = Event()

    def resume(self) -> None:
        self._shutdown.clear()

    def shutdown(self) -> None:
        self._shutdown.set()

    def run_once(self) -> bool:
        if self._shutdown.is_set():
            return False
        if not self.slots.acquire(self.runtime_id):
            return False
        task = None
        prepared = None
        heartbeat_stop = Event()
        lease_lost = Event()
        heartbeat_thread = None
        directory_lock = None
        try:
            self.tasks.recover_expired(self.runtime_id)
            task = self.tasks.claim_next(self.runtime_id, self.worker_id, self.lease_seconds)
            if task is None:
                return False
            # Renewals must cover the preparation phase too: worktree or
            # session setup can outlast the initial lease, and an unrenewed
            # lease would let another daemon reclaim the task mid-preparation.
            heartbeat_thread = Thread(
                target=self._heartbeat,
                args=(task.id, heartbeat_stop, lease_lost),
                daemon=True,
            )
            heartbeat_thread.start()
            issue = None
            if task.chat_session_id is None:
                issue = self.issues.get(task.issue_id)
                if issue is None:
                    raise NotFoundError(f"issue not found: {task.issue_id}")
            agent = self.agents.get(task.agent_id)
            if agent is None:
                raise NotFoundError(f"agent not found: {task.agent_id}")
            if agent.runtime_id != self.runtime_id or task.runtime_id != agent.runtime_id:
                raise RuntimeError(
                    f"agent {agent.id} is now bound to runtime {agent.runtime_id}; "
                    f"daemon {self.worker_id} cannot execute task {task.id}"
                )
            if task.chat_session_id is not None:
                return self._run_chat(task, agent, lease_lost)
            previous = self.tasks.previous_execution(task)
            prior_session_id = previous.session_id if previous is not None else None
            branch_name = previous.branch_name if previous is not None else None
            previous_tip = previous.recorded_tip if previous is not None else None
            workdir = None
            worktree_path = None
            in_place_path = None
            prepared = None
            if issue.project_path:
                execution_mode = issue.project_execution_mode or "worktree"
                if execution_mode == "worktree":
                    if self.worktrees is None:
                        raise RuntimeError("worktree manager is not configured")
                    prepared = self.worktrees.prepare(
                        issue.project_path,
                        issue.id,
                        agent.id,
                        task.id,
                        branch_name,
                        previous_tip,
                    )
                    branch_name = prepared.branch
                    worktree_path = prepared.workdir
                    workdir = prepared.workdir
                elif execution_mode == "in_place":
                    branch_name = None
                    worktree_path = None
                    workdir = issue.project_path
                    in_place_path = issue.project_path
                else:
                    raise RuntimeError(f"unknown project execution mode: {execution_mode}")
            elif agent.provider in LOCAL_WORKDIR_PROVIDERS:
                if self.workspaces is None:
                    raise RuntimeError("workspace manager is not configured")
                branch_name = None
                worktree_path = None
                workdir = self.workspaces.prepare(issue.id, agent.id)
            if lease_lost.is_set() or self._shutdown.is_set():
                # The lease was lost while preparing or the daemon is
                # stopping; leave the row for lease-expiry recovery instead
                # of writing a state we may no longer own.
                return True
            task = self.tasks.bind_execution_context(
                task.id,
                prior_session_id,
                branch_name,
                worktree_path,
                self.worker_id,
                task.attempt,
            )
            if self.tasks.is_cancel_requested(task.id):
                if prepared is not None:
                    self.worktrees.release(prepared)
                    prepared = None
                self._cancel_task(task)
                return True
            self.tasks.start(task.id, self.worker_id, task.attempt)

            def cancellation_check() -> bool:
                return (
                    self._shutdown.is_set()
                    or lease_lost.is_set()
                    or self.tasks.is_cancel_requested(task.id)
                )

            if in_place_path is not None:
                directory_lock = self.directory_locks.acquire(in_place_path, cancellation_check)
            provider = self.providers.get(agent.provider)
            run_agent = agent
            if task.is_leader_task:
                if self.squads is None or not task.squad_id:
                    raise RuntimeError("squad leader task has no squad coordinator")
                briefing = self.squads.briefing(task.squad_id, task.issue_id)
                run_agent = replace(
                    agent,
                    instructions=f"{agent.instructions}\n\n{briefing}".strip(),
                )
            result = provider.run(
                run_agent,
                issue,
                task,
                lambda event_type, payload: self._record_event(task.id, event_type, payload),
                workdir=workdir,
                resume_session_id=prior_session_id,
                is_cancelled=cancellation_check,
                replay_conflicts=prepared.replay_conflicts if prepared is not None else None,
            )
            delivery = None
            if prepared is not None:
                # Finalize before any terminal writeback: the branch must
                # carry this turn's work before the task reports an outcome.
                delivery = self.worktrees.finalize(prepared)
                if delivery.branch:
                    self.tasks.record_delivery(
                        task.id, delivery.branch, delivery.tip, self.worker_id, task.attempt
                    )
                prepared = None
            if self.tasks.is_cancel_requested(task.id):
                self._cancel_task(task)
            elif result.requires_input:
                self.tasks.wait_for_input(
                    task.id,
                    result.output,
                    result.session_id,
                    result.wait_reason or "input required",
                    self.worker_id,
                    task.attempt,
                )
                if task.squad_id and not task.is_leader_task and self.squads is not None:
                    self.squads.handle_worker_terminal(task)
            else:
                output = result.output
                if delivery is not None and delivery.branch:
                    # The branch reference is what lets a later agent review
                    # this turn's code: git diff HEAD...<branch> works from any
                    # worktree of the same repository.
                    output = (output or "") + (
                        f"\n\n---\n交付分支 `{delivery.branch}`（tip `{delivery.tip[:12]}`）。"
                        f"可用 `git diff HEAD...{delivery.branch}` 审核本轮改动。"
                    )
                if task.squad_id and self.squads is not None:
                    self.squads.complete_task(
                        task, output, result.session_id, self.worker_id, task.attempt
                    )
                else:
                    self.tasks.complete(
                        task.id,
                        output,
                        result.session_id,
                        self.worker_id,
                        task.attempt,
                    )
            return True
        except ChatInputUnavailable as exc:
            # Preserve the task: a read failure must not masquerade as zero
            # input. Leave it dispatched; lease expiry requeues for redelivery.
            logging.getLogger(__name__).warning(
                "chat input unavailable; task preserved for redelivery",
                extra={"task_id": getattr(task, "id", None), "reason": str(exc)},
            )
            return True
        except ProviderCancelled:
            if prepared is not None:
                self.worktrees.release(prepared)
                prepared = None
            if task is not None:
                if self.tasks.is_cancel_requested(task.id):
                    try:
                        self._cancel_task(task)
                    except (InvalidStateError, NotFoundError):
                        pass
                else:
                    self.tasks.record(task.id, "task.interrupted", {"worker_id": self.worker_id})
            return True
        except NotFoundError:
            if task is None or self.tasks.tasks.get(task.id) is not None:
                raise
            # Deletion raced with setup, a provider event, or its final callback.
            return True
        except Exception as exc:
            if prepared is not None:
                self.worktrees.release(prepared)
                prepared = None
            logging.getLogger(__name__).exception(
                "task execution failed",
                extra={"task_id": getattr(task, "id", None), "worker_id": self.worker_id},
            )
            if task is not None:
                current = self.tasks.tasks.get(task.id)
                if current is not None and current.status in {
                    TaskStatus.DISPATCHED,
                    TaskStatus.RUNNING,
                }:
                    try:
                        if task.squad_id and self.squads is not None:
                            self.squads.fail_task(task, str(exc), self.worker_id, task.attempt)
                        else:
                            self.tasks.fail(task.id, str(exc), self.worker_id, task.attempt)
                    except (InvalidStateError, NotFoundError):
                        pass
            return True
        finally:
            if directory_lock is not None:
                directory_lock.release()
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1)
            self.slots.release(self.runtime_id)

    def _run_chat(self, task, agent, lease_lost) -> bool:
        """Execute a chat task: assemble the conversation brief, run the
        provider, write the terminal state back. No worktree, no issue."""
        if self.chats is None:
            raise RuntimeError("chat repository is not configured")
        session = self.chats.get_session(task.chat_session_id)
        if session is None:
            raise NotFoundError(f"chat session not found: {task.chat_session_id}")
        input_task_id = task.chat_input_task_id or task.id
        batch = self.chats.input_messages(input_task_id)
        if not batch:
            raise ChatInputUnavailable(
                f"task {task.id} has no sealed input batch (chat_input_task_id={input_task_id})"
            )
        transcript = self.chats.list_messages(
            session.id, limit=self.chat_transcript_limit, exclude_task_id=input_task_id
        )
        prompt = build_chat_prompt(session, transcript, batch)
        workdir = session.work_dir
        if workdir is None or not Path(workdir).exists():
            if self.workspaces is None:
                raise RuntimeError("workspace manager is not configured")
            workdir = self.workspaces.prepare_chat(session.id)
        resume_session_id = resolve_chat_resume(self.chats, session, task)
        if lease_lost.is_set() or self._shutdown.is_set():
            # Lost the lease while preparing; leave the row to recovery.
            return True
        task = self.tasks.bind_execution_context(
            task.id, resume_session_id, None, None, self.worker_id, task.attempt
        )
        if self.tasks.is_cancel_requested(task.id):
            self._cancel_task(task)
            return True
        self.tasks.start(task.id, self.worker_id, task.attempt, advance_issue=False)

        def cancellation_check() -> bool:
            return (
                self._shutdown.is_set()
                or lease_lost.is_set()
                or self.tasks.is_cancel_requested(task.id)
            )

        provider = self.providers.get(agent.provider)
        result = provider.run(
            agent,
            None,
            task,
            lambda event_type, payload: self._record_event(task.id, event_type, payload),
            workdir=workdir,
            resume_session_id=resume_session_id,
            is_cancelled=cancellation_check,
            chat=ChatBrief(chat_session_id=session.id, prompt=prompt),
        )
        if self.tasks.is_cancel_requested(task.id):
            self._cancel_task(task)
        else:
            # requires_input is meaningless in a conversation: the agent's
            # question IS the reply, so every outcome completes the turn.
            self.tasks.complete_chat(
                task.id,
                result.output or "",
                result.session_id,
                workdir,
                self.worker_id,
                task.attempt,
            )
        return True

    def _record_event(self, task_id, event_type, payload):
        try:
            return self.tasks.record(task_id, event_type, payload)
        except NotFoundError:
            if self.tasks.tasks.get(task_id) is not None:
                raise
            raise ProviderCancelled("task deleted") from None

    def sweep_environments(self) -> None:
        """Reclaim env roots left behind by terminal or deleted tasks."""
        if self.worktrees is None:
            return
        active_statuses = {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.RUNNING}

        def is_task_active(task_id: str) -> bool:
            if not task_id:
                return False
            try:
                task = self.tasks.tasks.get(task_id)
            except Exception:
                logging.getLogger(__name__).exception("sweep: task lookup failed")
                return True  # unknown state: keep the directory rather than guess
            return task is not None and task.status in active_statuses

        self.worktrees.sweep(is_task_active)

    def _cancel_task(self, task) -> None:
        worker_role = bool(task.squad_id and not task.is_leader_task)
        self.tasks.cancel_running(
            task.id,
            self.worker_id,
            task.attempt,
            advance_issue=not worker_role,
        )
        if worker_role and self.squads is not None:
            self.squads.handle_worker_terminal(task)

    def _heartbeat(self, task_id: str, stop: Event, lease_lost: Event) -> None:
        # The lease stays valid for lease_seconds past the last successful
        # renewal. A renewal error leaves ownership unknown, so keep retrying
        # until that window closes; a False return means the task was already
        # reclaimed, which is definitive.
        deadline = time.monotonic() + self.lease_seconds
        while not stop.wait(self.heartbeat_seconds):
            try:
                renewed = self.tasks.renew_lease(task_id, self.worker_id, self.lease_seconds)
            except Exception:
                logging.getLogger(__name__).exception(
                    "lease renewal failed",
                    extra={"task_id": task_id, "worker_id": self.worker_id},
                )
                if time.monotonic() < deadline:
                    continue
                renewed = False
            if not renewed:
                lease_lost.set()
                return
            deadline = time.monotonic() + self.lease_seconds


class DaemonWorker:
    """Keeps a Daemon alive, with wakeups for low-latency local execution."""

    def __init__(self, daemon: Daemon, poll_interval: float = 2.0) -> None:
        self.daemon = daemon
        self.poll_interval = poll_interval
        self._stop = Event()
        self._wakeup = Event()
        self._thread: Optional[Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.daemon.resume()
        try:
            self.daemon.sweep_environments()
        except Exception:
            logging.getLogger(__name__).exception("environment sweep failed")
        self._thread = Thread(target=self._run, name=f"mica-{self.daemon.worker_id}", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wakeup.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.daemon.shutdown()
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            while not self._stop.is_set() and self._run_batch():
                continue
            self._wakeup.wait(self.poll_interval)
            self._wakeup.clear()

    def _run_batch(self) -> bool:
        results = []
        result_lock = Lock()

        def execute_one() -> None:
            executed = self.daemon.run_once()
            with result_lock:
                results.append(executed)

        threads = [
            Thread(target=execute_one, name=f"mica-slot-{index}", daemon=True)
            for index in range(self.daemon.slots.capacity(self.daemon.runtime_id))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return any(results)
