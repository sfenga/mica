# Proposal: add-mica-core

## Why

multiCa 生产仓库(Go + Next.js + PostgreSQL)功能完整但体量巨大,新手难以从一处入手理解"把 coding agent 变成数字队友"这条主线。claw0 已证明:用渐进式单文件章节、每章只加一个概念、前置代码原样保留,能把一个复杂系统拆成可读、可运行的台阶。

mica 复用这套范式,主题为 multiCa 的核心机制(issue 派活 + daemon 执行 + 实时回传),章节按 multiCa 功能重排。学完 mica,读者能顺藤摸到 multiCa 生产代码(`server/internal/handler/comment.go`、`server/internal/daemon`、`server/internal/realtime` 等)。

## What Changes

新建 `mica/` 仓库,含 `sessions/zh/s01..s11`(+可选 `s12`)渐进章节、`workspace/` 运行时数据目录、`tests/` 纯逻辑单测、`docs/` 说明、`openspec/` 本变更。

章节按 multiCa 核心功能重排(每章只加一个新概念):

- s01 Issue 模型 + 看板 + 7 态状态机(backlog/todo/in_progress/in_review/done/blocked/cancelled)+ 多态 assignee
- s02 Task 实体 + Daemon 认领 **task**(queued→dispatched→running)+ StubRunner;task 生命周期驱动 issue 状态
- s03 `claude` CLI runner on task,真实模型接入
- s04 JSONL 持久化(issues + tasks + sessions)+ 重放 + orphan 恢复 + 截断 summarize
- s05 评论建新 task + `--resume` 上一 task.session_id + resume 失效降级
- s06 WebSocket 实时进度
- s07 FastAPI 单页看板 + 创建/状态转移/评论
- s08 markdown 技能注入(frontmatter.keywords 关键词)
- s09 cron autopilot(建 issue + 入队 task)
- s10 多 CLI + PATH 探测(1 daemon : N agent)
- s11 多 workspace 隔离 + 成员资格闸门
- s12*(可选附录)leader agent 委派(squads)

Agent 执行方式:AgentRunner 抽象 + 真实 coding-agent CLI(`claude -p --verbose --output-format stream-json`),模型连接由 CLI 自身完成;不用 Anthropic API 假装 agent。s02 用 stub runner 单独验证认领循环,不依赖 CLI、不花钱。

## Capabilities

### New Capabilities
- `mica-core`: 建立 mica 的主 capability,覆盖 issue 看板(含 review/blocked)、Task 执行队列、daemon 认领 task、AgentRunner(CLI runner)、JSONL 持久化(issues+tasks+sessions)、WebSocket 实时、FastAPI 看板、markdown 技能、cron autopilot、多 CLI PATH 探测、多 workspace 隔离。本变更首次建立该 capability 的主 spec。

### Modified Capabilities
<!-- 无,mica-core 为全新建立。 -->

## Impact
- **代码**: 新增 `sessions/zh/s01..s11`(+可选 s12)每章一个 `.py` + `.md`;`tests/` 纯逻辑单测。
- **配置**: `.env.example`(`MULTICA_DEFAULT_CLI`/`POLL_INTERVAL`/`DATA_DIR`/`WS_PORT`/`WEB_PORT`/`AGENT_ID`)+ `requirements.txt`。
- **工作区**: `workspace/` 下 `issues.jsonl`、`tasks.jsonl`、`sessions/`、`skills/`、`workspaces.jsonl`、`members.jsonl`、`autopilots.jsonl`。
- **依赖**: 无外部服务(PostgreSQL 不引);s03+ 端到端依赖本机 `claude` 已装并登录。
- **跨章关系**: 每章前置代码原样保留,后一章在前一章基础上加一个新概念。s03 起需 `claude` 在 PATH。
- **非目标(本次不做)**: 见 design.md「非目标」。
