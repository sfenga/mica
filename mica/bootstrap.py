from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mica.application import (
    ChatBatcher,
    ChatIngressService,
    IngressService,
    IssueService,
    ProjectService,
    RuntimeDiscoveryService,
    SquadService,
    TaskNotificationProjector,
    TaskService,
    TriggerService,
)
from mica.application.deletion import IssueDeletionService
from mica.application.ports import (
    AgentRepository,
    ChannelRepository,
    EventRepository,
    IssueRepository,
    ProjectRepository,
    RuntimeCapabilityRepository,
    RuntimeRepository,
    SquadRepository,
    TaskRepository,
)
from mica.domain import Agent, Runtime
from mica.infrastructure import (
    InMemoryAgentRepository,
    InMemoryChannelRepository,
    InMemoryChatRepository,
    InMemoryEventRepository,
    InMemoryIssueRepository,
    InMemoryProjectRepository,
    InMemoryRuntimeCapabilityRepository,
    InMemoryRuntimeRepository,
    InMemorySquadRepository,
    InMemoryTaskRepository,
)
from mica.infrastructure.database import (
    PostgresAgentRepository,
    PostgresChannelRepository,
    PostgresChatRepository,
    PostgresDatabase,
    PostgresEventRepository,
    PostgresIssueRepository,
    PostgresProjectRepository,
    PostgresRuntimeCapabilityRepository,
    PostgresRuntimeRepository,
    PostgresSquadRepository,
    PostgresTaskRepository,
    migrate,
)
from mica.infrastructure.memory_transaction import MemoryTransactionManager
from mica.integrations import ChannelDeliveryWorker
from mica.integrations.feishu import (
    FeishuMessageSender,
    FeishuTypingReceipt,
    FeishuWebhook,
    config_from_env,
)
from mica.realtime import DatabaseEventRelay, EventBus
from mica.runtime import Daemon, DaemonWorker, RuntimeSlotPool, WorkspaceManager, WorktreeManager
from mica.runtime.providers import (
    A2aProvider,
    AcpProvider,
    CliProvider,
    PiProvider,
    ProviderRegistry,
    StubProvider,
)


@dataclass
class ApplicationContainer:
    issues: IssueRepository
    tasks: TaskRepository
    events: EventRepository
    agents: AgentRepository
    runtimes: RuntimeRepository
    runtime_capabilities: RuntimeCapabilityRepository
    runtime_discovery: RuntimeDiscoveryService
    projects: ProjectRepository
    project_service: ProjectService
    squads: SquadRepository
    event_bus: EventBus
    deletion_service: IssueDeletionService
    issue_service: IssueService
    task_service: TaskService
    trigger_service: TriggerService
    squad_service: SquadService
    channels: ChannelRepository
    ingress_service: IngressService
    channel_worker: ChannelDeliveryWorker
    feishu_webhook: Optional[FeishuWebhook]
    providers: ProviderRegistry
    slots: RuntimeSlotPool
    worktrees: WorktreeManager
    workspaces: WorkspaceManager
    daemon: Daemon
    worker: DaemonWorker
    chats: object
    chat_ingress: ChatIngressService
    chat_batcher: ChatBatcher
    database: Optional[PostgresDatabase] = None
    event_relay: Optional[DatabaseEventRelay] = None


def _provider_registry() -> ProviderRegistry:
    providers = ProviderRegistry()
    providers.register("stub", StubProvider())
    providers.register("codex", CliProvider("codex"))
    providers.register("claude", CliProvider("claude"))
    providers.register("pi", PiProvider())
    providers.register("acp", AcpProvider())
    providers.register("a2a", A2aProvider())
    return providers


def _worktree_manager() -> WorktreeManager:
    configured = os.getenv("MICA_WORKTREE_ROOT", str(Path.home() / ".mica" / "worktrees"))
    return WorktreeManager(str(Path(configured).expanduser()))


def _workspace_manager() -> WorkspaceManager:
    configured = os.getenv("MICA_WORKSPACE_ROOT", str(Path.home() / ".mica" / "workspaces"))
    return WorkspaceManager(str(Path(configured).expanduser()))


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _chat_window_seconds() -> float:
    return _positive_float("MICA_CHAT_BATCH_WINDOW", 3.0)


def _chat_transcript_limit() -> int:
    return _positive_int("MICA_CHAT_TRANSCRIPT_LIMIT", 20)


def build_memory_container(
    seed_defaults: bool = True, default_provider: str = "stub"
) -> ApplicationContainer:
    issues = InMemoryIssueRepository()
    tasks = InMemoryTaskRepository()
    events = InMemoryEventRepository()
    channels = InMemoryChannelRepository()
    chats = InMemoryChatRepository(tasks=tasks)
    tasks.attach_owner_stores(issues=issues, chats=chats)
    agents = InMemoryAgentRepository()
    runtimes = InMemoryRuntimeRepository()
    runtime_capabilities = InMemoryRuntimeCapabilityRepository()
    projects = InMemoryProjectRepository()
    squads = InMemorySquadRepository()
    event_bus = EventBus()

    transactions = MemoryTransactionManager([issues, tasks, events, channels, squads])
    issue_service = IssueService(issues, transactions=transactions)
    task_service = TaskService(
        tasks, issues, events, event_bus, transactions=transactions, chats=chats
    )
    squad_service = SquadService(
        squads, agents, issues, tasks, task_service, transactions=transactions
    )
    trigger_service = TriggerService(
        issues, agents, issue_service, task_service, squads=squads, transactions=transactions
    )
    ingress_service = IngressService(
        channels, trigger_service, issue_service, transactions=transactions
    )
    chat_ingress = ChatIngressService(
        chats, channels, agents, trigger_service, task_service, transactions=transactions
    )
    chat_batcher = ChatBatcher(chat_ingress.flush_session, window_seconds=_chat_window_seconds())
    chat_ingress.bind_batcher(chat_batcher)
    deletion_service = IssueDeletionService(issues, tasks, events, channels, transactions)
    providers = _provider_registry()
    slots = RuntimeSlotPool()
    worktrees = _worktree_manager()
    workspaces = _workspace_manager()
    runtime_discovery = RuntimeDiscoveryService(runtimes, runtime_capabilities)
    project_service = ProjectService(projects, runtimes)

    if seed_defaults:
        if not providers.contains(default_provider):
            raise ValueError(f"unknown default provider: {default_provider}")
        runtime = runtimes.add(Runtime(id="local", name="Local Runtime", max_slots=1))
        agents.add(
            Agent(
                id="local-assistant",
                name="Local Assistant",
                runtime_id=runtime.id,
                provider=default_provider,
            )
        )
        slots.configure(runtime.id, runtime.max_slots)
        from mica.domain import RuntimeCapability

        runtime_capabilities.replace_for_runtime(
            runtime.id,
            [
                RuntimeCapability(
                    runtime_id=runtime.id,
                    provider=default_provider,
                    executable_path=default_provider,
                    version="test",
                )
            ],
        )

    daemon = Daemon(
        worker_id="daemon-local-1",
        runtime_id="local",
        tasks=task_service,
        issues=issues,
        agents=agents,
        providers=providers,
        slots=slots,
        worktrees=worktrees,
        workspaces=workspaces,
        lease_seconds=_positive_int("MICA_LEASE_SECONDS", 120),
        heartbeat_seconds=_positive_int("MICA_HEARTBEAT_SECONDS", 30),
        squads=squad_service,
        chats=chats,
        chat_transcript_limit=_chat_transcript_limit(),
    )
    worker = DaemonWorker(daemon)
    feishu_config = config_from_env(os.environ)
    receipt = FeishuTypingReceipt(feishu_config) if feishu_config else None
    senders = (
        {"feishu": FeishuMessageSender(feishu_config, receipt=receipt)} if feishu_config else {}
    )
    channel_worker = ChannelDeliveryWorker(
        channels,
        TaskNotificationProjector(
            events, tasks, channels, transactions=transactions, issues=issues, chats=chats
        ),
        senders,
        max_attempts=_positive_int("MICA_CHANNEL_MAX_ATTEMPTS", 8),
    )
    return ApplicationContainer(
        issues=issues,
        tasks=tasks,
        events=events,
        agents=agents,
        runtimes=runtimes,
        runtime_capabilities=runtime_capabilities,
        runtime_discovery=runtime_discovery,
        projects=projects,
        project_service=project_service,
        squads=squads,
        event_bus=event_bus,
        issue_service=issue_service,
        deletion_service=deletion_service,
        task_service=task_service,
        trigger_service=trigger_service,
        squad_service=squad_service,
        channels=channels,
        ingress_service=ingress_service,
        channel_worker=channel_worker,
        feishu_webhook=(
            FeishuWebhook(feishu_config, chat_ingress, receipt=receipt) if feishu_config else None
        ),
        providers=providers,
        slots=slots,
        worktrees=worktrees,
        workspaces=workspaces,
        daemon=daemon,
        worker=worker,
        chats=chats,
        chat_ingress=chat_ingress,
        chat_batcher=chat_batcher,
    )


def build_postgres_container(
    dsn: str, seed_defaults: bool = True, default_provider: Optional[str] = None
) -> ApplicationContainer:
    database = PostgresDatabase(dsn)
    migrate(database)
    issues = PostgresIssueRepository(database)
    tasks = PostgresTaskRepository(database)
    events = PostgresEventRepository(database)
    channels = PostgresChannelRepository(database)
    chats = PostgresChatRepository(database)
    agents = PostgresAgentRepository(database)
    runtimes = PostgresRuntimeRepository(database)
    runtime_capabilities = PostgresRuntimeCapabilityRepository(database)
    projects = PostgresProjectRepository(database)
    squads = PostgresSquadRepository(database)
    event_bus = EventBus()
    deletion_service = IssueDeletionService(issues, tasks, events, channels, database)
    issue_service = IssueService(issues, transactions=database)
    # PostgreSQL events are published by DatabaseEventRelay after commit, so a
    # rolled-back transaction can never leak a realtime event.
    task_service = TaskService(tasks, issues, events, transactions=database, chats=chats)
    squad_service = SquadService(squads, agents, issues, tasks, task_service, transactions=database)
    trigger_service = TriggerService(
        issues,
        agents,
        issue_service,
        task_service,
        transactions=database,
        squads=squads,
    )
    ingress_service = IngressService(
        channels, trigger_service, issue_service, transactions=database
    )
    chat_ingress = ChatIngressService(
        chats, channels, agents, trigger_service, task_service, transactions=database
    )
    chat_batcher = ChatBatcher(chat_ingress.flush_session, window_seconds=_chat_window_seconds())
    chat_ingress.bind_batcher(chat_batcher)
    providers = _provider_registry()
    slots = RuntimeSlotPool()
    worktrees = _worktree_manager()
    workspaces = _workspace_manager()
    runtime_discovery = RuntimeDiscoveryService(runtimes, runtime_capabilities)
    project_service = ProjectService(projects, runtimes)
    if seed_defaults:
        runtime_id = os.getenv("MICA_RUNTIME_ID", "local")
        runtime = runtimes.add(
            Runtime(
                id=runtime_id,
                name=os.getenv("MICA_RUNTIME_NAME", "Local Runtime"),
                max_slots=_positive_int("MICA_MAX_SLOTS", 1),
            )
        )
        if agents.get("local-assistant") is None:
            provider = default_provider or os.getenv("MICA_DEFAULT_PROVIDER", "codex")
            if not providers.contains(provider):
                raise ValueError(f"unknown default provider: {provider}")
            agents.add(
                Agent(
                    id="local-assistant",
                    name="Local Assistant",
                    runtime_id=runtime.id,
                    provider=provider,
                )
            )
        slots.configure(runtime.id, runtime.max_slots)
        runtime_discovery.probe(runtime.id)
    runtime_id = os.getenv("MICA_RUNTIME_ID", "local")
    daemon = Daemon(
        worker_id=os.getenv("MICA_DAEMON_ID", "daemon-local-1"),
        runtime_id=runtime_id,
        tasks=task_service,
        issues=issues,
        agents=agents,
        providers=providers,
        slots=slots,
        worktrees=worktrees,
        workspaces=workspaces,
        lease_seconds=_positive_int("MICA_LEASE_SECONDS", 120),
        heartbeat_seconds=_positive_int("MICA_HEARTBEAT_SECONDS", 30),
        squads=squad_service,
        chats=chats,
        chat_transcript_limit=_chat_transcript_limit(),
    )
    worker = DaemonWorker(daemon, poll_interval=_positive_float("MICA_POLL_INTERVAL", 2))
    feishu_config = config_from_env(os.environ)
    receipt = FeishuTypingReceipt(feishu_config) if feishu_config else None
    senders = (
        {"feishu": FeishuMessageSender(feishu_config, receipt=receipt)} if feishu_config else {}
    )
    channel_worker = ChannelDeliveryWorker(
        channels,
        TaskNotificationProjector(
            events, tasks, channels, transactions=database, issues=issues, chats=chats
        ),
        senders,
        poll_interval=_positive_float("MICA_CHANNEL_POLL_INTERVAL", 2),
        max_attempts=_positive_int("MICA_CHANNEL_MAX_ATTEMPTS", 8),
    )
    return ApplicationContainer(
        issues=issues,
        tasks=tasks,
        events=events,
        agents=agents,
        runtimes=runtimes,
        runtime_capabilities=runtime_capabilities,
        runtime_discovery=runtime_discovery,
        projects=projects,
        project_service=project_service,
        squads=squads,
        event_bus=event_bus,
        issue_service=issue_service,
        deletion_service=deletion_service,
        task_service=task_service,
        trigger_service=trigger_service,
        squad_service=squad_service,
        channels=channels,
        ingress_service=ingress_service,
        channel_worker=channel_worker,
        feishu_webhook=(
            FeishuWebhook(feishu_config, chat_ingress, receipt=receipt) if feishu_config else None
        ),
        providers=providers,
        slots=slots,
        worktrees=worktrees,
        workspaces=workspaces,
        daemon=daemon,
        worker=worker,
        chats=chats,
        chat_ingress=chat_ingress,
        chat_batcher=chat_batcher,
        database=database,
        event_relay=DatabaseEventRelay(events, event_bus),
    )


def build_container_from_env() -> ApplicationContainer:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass
    dsn = os.getenv("DATABASE_URL")
    storage = os.getenv("MICA_STORAGE", "postgres" if dsn else "memory").lower()
    if storage not in {"memory", "postgres"}:
        raise ValueError("MICA_STORAGE must be memory or postgres")
    if storage == "postgres":
        if not dsn:
            raise ValueError("DATABASE_URL is required when MICA_STORAGE=postgres")
        return build_postgres_container(dsn)
    return build_memory_container(default_provider=os.getenv("MICA_DEFAULT_PROVIDER", "codex"))
