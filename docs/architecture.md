# Mica 架构基线

## 阶段 1 范围

阶段 1 将早期单文件原型整理为可扩展的后端骨架，并跑通一条不依赖真实 Agent 的任务链路：

```text
Web 请求 → TriggerService → Issue + Task → Daemon → AgentProvider
                                      ↓                 ↓
                                  TaskEvent ← 状态与执行事件
```

- `domain`：保存 Issue、Task、Agent、Runtime 及状态枚举，不依赖 Web 和存储框架。
- `application`：编排用户触发、任务状态流转和事件记录，只依赖仓储与事件发布接口。
- `infrastructure`：提供线程安全的内存适配器和正式 PostgreSQL 仓储，两者遵循同一领取与状态流转契约。
- `runtime`：Daemon 只领取与自身 Runtime 匹配的任务，并通过 Provider 接口运行 Agent。
- `api`：提供事项、评论、任务查询、单次 Daemon 执行和 WebSocket 事件接口。

## 已固定的核心语义

1. Issue 表示用户持续关注的事项；Task 表示由创建、追问或重试触发的一次具体执行。
2. Agent 绑定 Runtime 和 Provider。Runtime 决定任务由哪一类执行环境领取，Provider 决定如何调用 Agent。
3. 执行容量先于任务领取，避免任务已进入执行态却没有可用进程。
4. Agent 输出统一转换为 TaskEvent，API 不依赖具体 CLI 或远程协议的事件格式。
5. Web 或飞书中的连续补充先按显式 Agent/Squad、回复的 Task、Issue 默认负责人依次路由；同一 Issue/Agent 只保留一个 queued Task，Daemon 领取时固化本次执行要消费的评论快照。
5. Agent 成功只将 Issue 推进到 `in_review`，由用户确认后再进入 `done`。

## 阶段 2：PostgreSQL 核心

- 使用版本化 SQL migration 建立 Runtime、Agent、Issue、Comment、Task 与 TaskEvent 表。
- 创建 Issue 与首个 Task、Task 与 Issue 状态更新共享同一数据库事务。
- Daemon 使用 `FOR UPDATE SKIP LOCKED` 领取与自身 Runtime 匹配的 Task。
- Task 保存租约、worker、attempt 和取消标记；`worker_id + attempt` 阻止旧 Daemon 晚到回写。

## 阶段 3：本地 Agent 闭环

- Codex 与 Claude Code 通过 `CliProvider` 以 JSONL 流式运行，输出转换为统一 TaskEvent。
- Daemon 在有空闲 Slot 时才领取任务，领取后立即由心跳线程续租，Worktree/Session 准备阶段与执行阶段同样受租约保护；准备期间失去租约或停机时，放弃启动 Provider 并交回过期恢复。
- 取消请求由 Provider 轮询并终止整个本地进程组。
- 本地项目显式配置 `in_place` 或 `worktree`：前者串行操作当前目录，后者为 `(Issue, Agent)` 创建持久 Git Worktree 和独立分支。
- 未关联项目的本地任务在 Daemon 管理的隔离空目录中运行；远程 A2A 任务不分配本机工作目录。
- 后续评论产生的新 Task 复用上一次 CLI Session；Worktree 模式还会复用同一分支和 Worktree。
- Agent 尚未领取任务时，连续评论通过 PostgreSQL 部分唯一索引与 UPSERT 合并；Agent 已在执行时，只创建一个 queued 后续 Task，避免重复启动且不改写正在执行的输入。
- Daemon 启动或轮询时把租约过期的 Task 恢复到队列，再以新 attempt 执行。

## 阶段 4：异构 Agent 接入

- `AcpProvider` 以 ACP v1 JSON-RPC/stdio 驭动本地 Coding Agent，完成初始化、Session 新建/加载、流式更新、权限请求和取消。
- ACP 权限默认拒绝；只有 Agent 显式配置 `permission_policy=allow` 时才选择临时允许选项，并把决策写入 TaskEvent。
- `A2aProvider` 按 A2A 1.0 获取 Agent Card，选择 `HTTP+JSON` 接口并校验配置的 Skill。
- A2A 同步 Message 和异步 Task 轮询都转换为统一事件；本地取消会调用远端 Task cancel。
- A2A Session Token 保存 `contextId`、远端 `taskId` 和终止状态：普通追问复用上下文；`INPUT_REQUIRED`/`AUTH_REQUIRED` 映射为 `waiting_input`，Issue 进入 `blocked`，用户补充信息后继续原远端 Task。
- 业务 Agent 可配置 JSON Schema；结构化 Artifact 或 JSON 文本只有通过校验后才能完成本地 Task。
- A2A 凭证只通过 `token_env` 引用环境变量；生产地址强制 HTTPS，跨域 Agent Card endpoint 需要显式放行。

ACP v2 当前仍是实验草案，本阶段采用稳定 v1；A2A 采用已发布的 1.0 REST binding。真实环境验收项见 `docs/test-plan.md`。

## 阶段 5：Web 与飞书协作入口

```text
Web / 飞书事件 → IngressService → 外部事件去重 + 用户映射
                                      ↓（同一事务）
                                Issue + Task + Origin
                                      ↓
TaskEvent → NotificationProjector → Outbox → 飞书消息 API
```

- Web 与飞书只负责解析各自请求，创建任务统一经过 `IngressService`，避免渠道形成两套业务逻辑。
- Web 可从执行记录直接追问指定 Agent，也支持 `@agent:ID` / `@squad:ID`；飞书对原消息的线程回复追加到已有 Issue，而不是重复创建任务卡片。
- 外部事件以 `(channel, tenant_key, external_event_id)` 唯一约束保证幂等；同一 Key 携带不同业务内容会返回冲突。
- 外部身份以 `(channel, tenant_key, external_user_id)` 映射平台用户，Issue 保存创建人，飞书租户映射到独立 Workspace。
- `IssueOrigin` 保存任务来源会话，使执行结果能够回到原飞书群或单聊。
- 结果通知先由 TaskEvent 投影为 Outbox；投递线程通过租约认领、失败退避和消息 UUID 发送，进程重启后可继续处理。
- 飞书官方 SDK 负责回调 token、签名、加密事件与 tenant access token；平台负责业务幂等和事务一致性。

## 阶段 6：Agent Squad 编排

Mica 采用与 Multica 一致的 leader-first 核心语义：Squad 是 Workspace 内的路由与协调对象，本身不执行任务，也不会在收到 Issue 后自动广播所有成员。

```text
Issue → Squad → Leader Task（注入 roster 与协作规则）
                     ↓ 结构化委派决策
              Worker Task A / Worker Task B
                     ↓ 全部进入终态
              Leader Task（汇总与收口）→ in_review
```

- `Squad` 保存 Leader 和用户定义的协作规则；`SquadMember` 保存成员角色与能力标签。
- Issue 保持 `assignee_type=squad`，实际 Task 指向 Leader 或成员 Agent，因而本地 CLI、ACP 和 A2A Provider 可以混合编入同一个 Squad。
- Task 使用 `squad_id`、`is_leader_task` 和 `delegated_from_task_id` 记录本轮角色和委派来源，Daemon 不通过提示词或反向查询猜测 Leader 身份。
- Leader 必须输出 `delegate / complete` JSON；平台确定性校验成员归属、重复指派和空指令，不合规输出使 Leader Task 失败并阻止派发。
- 成员 Task 继承 Leader 的具体 handoff 指令。单个成员完成、失败或取消不会提前结束父 Issue；同批成员全部进入终态后，在 Issue 行锁内检查并只创建一个 Leader 汇总 Task。
- Leader 首次委派后 Issue 保持 `in_progress`，只有后续 Leader 确认整体完成才进入 `in_review`，最后仍由用户确认 `done`。

## 工程收尾

- 正式 Python 包只发布 `mica/`，历史原型不进入安装产物。
- 启动时校验存储模式、数值配置、默认 Provider、Runtime 与 Agent 引用；`/health/live` 和 `/health/ready` 分离进程存活与依赖就绪。
- API 与 Daemon 使用统一结构化日志；事件 Relay 和渠道投递线程遇到暂时异常时记录错误并继续运行。
- Outbox 使用租约、指数退避和最大尝试次数；永久失败进入 `failed`，避免无限重试。
- CI 在 Python 3.11/3.12 和 PostgreSQL 17 上执行编译、Ruff 静态检查与全量测试。

## 后续替换点

- 企业 SSO、Workspace 成员权限和飞书用户资料同步。
- 多阶段子 Issue、人工成员协作和 Squad 配置版本化。
- 多副本部署所需的 Runtime 在线心跳、投影游标持久化和集中式可观测性。
