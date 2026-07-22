# Proposal: add-mica-core

## Why

multiCa 生产仓库(Go + Next.js + PostgreSQL)功能完整但体量巨大,新手难以从一处入手理解"把 coding agent 变成数字队友"这条主线。claw0 已证明:用渐进式单文件章节、每章只加一个概念、前置代码原样保留,能把一个复杂系统拆成可读、可运行的台阶。

mica 复用这套范式,但主题换为 multiCa 的核心机制(issue 派活 + daemon 执行 + 实时回传),章节按 multiCa 自己的功能重排,不复刻 claw0 的网关路由。学完 mica,读者能顺藤摸到 multiCa 生产代码(`server/internal/dispatch`、`server/internal/daemon`、`server/internal/realtime` 等)。

## What Changes

新建 `mica/` 仓库,含 `sessions/zh/s01..s10`(+可选 `s11`)渐进章节、`workspace/` 运行时数据目录、`tests/` 纯逻辑单测、`docs/explain/` 架构说明、`openspec/` 本变更。

章节按 multiCa 核心功能重排(每章只加一个新概念):

- s01 Issue 模型 + 看板 + 多态 assignee + 状态机
- s02 Daemon 认领执行循环(stub runner,验证认领机制不花钱)
- s03 `claude` CLI runner,真实模型接入
- s04 JSONL 持久化 + 会话重放 + 溢出处理
- s05 WebSocket 实时进度推送
- s06 FastAPI 单页看板 + 实时
- s07 markdown 技能注入 CLI 调用
- s08 cron 建 issue 路由给 agent(autopilot)
- s09 多 CLI + PATH 探测(unified runtimes)
- s10 多 workspace 隔离
- s11*(可选附录)leader agent 委派(squads)

Agent 执行方式:AgentRunner 抽象 + 真实 coding-agent CLI(`claude -p --output-format stream-json`),模型连接由 CLI 自身完成;**不**用 Anthropic API 假装 agent。s02 用 stub runner 单独验证认领循环,不依赖 CLI、不花钱。

## Capabilities

### New Capabilities

- `mica-core`: 建立 mica 的主 capability,覆盖 issue 看板与状态机、多态 assignee、daemon 认领执行、AgentRunner(CLI runner)、JSONL 会话持久化、WebSocket 实时、FastAPI 看板、markdown 技能、cron autopilot、多 CLI PATH 探测、多 workspace 隔离。本变更首次建立该 capability 的主 spec,作为各章在生产形态下如何接线的契约。

### Modified Capabilities

<!-- 无,mica-core 为全新建立,不修改既有 capability。 -->

## Impact

- **代码**: 新增 `sessions/zh/s01..s10`(+可选 s11)每章一个 `.py` + `.md`,约 2500–3500 行 Python;`tests/` 纯逻辑单测。
- **配置**: `.env.example`(`MULTICA_DEFAULT_CLI`、`MULTICA_POLL_INTERVAL`、`MULTICA_DATA_DIR`、`MULTICA_WS_PORT`、`MULTICA_WEB_PORT`、`MULTICA_AGENT_ID`)+ `requirements.txt`(python-dotenv、websockets、croniter、fastapi、uvicorn、httpx)。
- **工作区**: `workspace/` 下 `issues.jsonl`、`sessions/`、`skills/`、`workspaces.jsonl`,由代码生成/读写。
- **依赖**: 无外部服务(PostgreSQL 不引);s03+ 端到端依赖本机 `claude` 已装并登录。
- **跨章关系**: 每章前置代码原样保留,后一章在前一章基础上加一个新概念。s03 起需 `claude` 在 PATH;此前章节不依赖 CLI。
- **非目标(本次不做)**: 见 design.md「非目标」。
