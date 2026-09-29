# 高频追问、方案取舍与当前边界

这篇不是让面试者背答案，而是给出能经得住追问的边界。回答时先说明业务问题，再说明约束落在哪一层，最后主动指出当前实现范围。

## 1. 四个技术点分别在突出什么

| 技术点 | 后端能力 | AI 应用能力 | 核心业务价值 |
|---|---|---|---|
| 多渠道触发与合并 | 事务、幂等、UPSERT、部分唯一索引、并发领取 | 多轮上下文路由 | 减少重复 Agent 调用且不丢补充要求 |
| 本地隔离执行 | Daemon、Slot、租约、心跳、进程组、Git Worktree | Coding Agent 会话续接 | 安全、可恢复地操作本地项目 |
| 异构 Agent 接入 | Adapter、协议状态映射、取消、HTTP 安全、Schema 校验 | CLI/ACP/A2A、结构化结果 | 复用已有 Agent，而不是要求全部重写 |
| Squad 与飞书 | 批次 barrier、Outbox、重试、渠道幂等 | Leader-first、多 Agent 委派与汇总 | 用户按业务角色组合 Agent 并从协作入口使用 |

### 1.1 哪些参考 Multica，哪些是 Mica 的实现选择

| 主题 | Multica 中的对应设计 | Mica 当前实现 |
|---|---|---|
| 渠道入口 | 普通渠道消息进入 `ChatSession`，显式 `/issue` 才创建 Issue；Chat 有短时间 debounce | `/mica` 任务消息直接创建 Issue，Issue 评论按 Task 状态合并 |
| 评论续接 | 回复 Comment，并通过来源 Task 恢复 Agent 上下文 | 保留 `parent_comment_id + source_task_id`，交互上同样不要求用户回复 Task |
| 输入封口 | claim 后登记已投递评论；运行期新评论先记为 planned，完成时对账 | claim 的同一条 SQL 固化 `delivered_comment_ids`，运行期允许一条 queued successor |
| 异常恢复 | prepare lease；运行期结合 Runtime 心跳；旧记录失败后创建 retry child | 同一 Task 使用 `claimed_by + lease_expires_at + attempt` 过期重排并隔离旧写回 |
| 本地执行 | Runtime/Slot、仓库与 Worktree、会话续接 | 沿用这些核心思想，但采用稳定 Issue/Agent 分支和更保守的 in-place 串行 |
| Agent 协议 | 本地 CLI/ACP、Agent Message、`task_message` 和事件总线 | Provider + 持久化 TaskEvent；另外扩展标准 A2A 与结构化结果校验 |
| Squad | Leader 通过工具、@成员评论或子 Issue 委派；按子 Issue/stage 等待 | Leader 输出受约束 JSON；以同批成员 Task 的 sibling barrier 收口 |
| 飞书与投递 | WebSocket Channel Adapter，支持卡片、附件、编辑等；事件总线与投递账本 | HTTP Webhook 文本入口；TaskEvent 投影到事务 Outbox 后异步发送 |

面试时先讲 Mica 为业务问题做出的实现，再用 Multica 解释设计来源和可演进方向。不要把右列尚未采用的机制说成当前代码，也不要把 Mica 的扩展反向归因于 Multica。

## 2. 为什么同时需要 Issue 和 Task

Issue 是用户持续讨论的业务事项，保存目标、负责人、评论和人工确认状态；Task 是一次有明确输入边界和执行结果的 Agent 调用。

如果只有 Issue：重试、补充要求和不同 Agent 执行会覆盖同一状态，无法做 Session、租约和历史追踪。如果只有 Task：用户每次补充都变成孤立请求，无法表达“继续处理同一件事”。

最适合面试的例子是：一个 Issue 首次由 Claude 修改代码，用户评论要求补测试，系统创建后续 Task，复用同一分支和 Session，但两次执行各自有状态、事件和错误。

## 3. 为什么不是每条评论都创建独立 Task

模型调用和本地进程启动成本高，而且连续评论通常共同描述一个意图。queued 阶段尚未消耗执行资源，因此适合合并；dispatched 后输入已经封口，新评论进入 successor。这不是简单 debounce，而是以资源承诺和状态迁移为批次边界。

这是 Mica 当前的简化。Multica 的普通渠道 Chat 还有短时间 debounce；在 Issue 执行中，领取后到达的评论先登记为 planned input，当前执行完成后再对账并决定后续 Task，而不是立即依赖一条 queued successor。

## 4. 为什么不更新正在运行的 Task Prompt

运行中注入消息并非所有 Provider 都支持，也会让工具调用与用户消息的先后关系不可重放。Mica 选择“本轮输入不可变，下一轮续接 Session”，牺牲毫秒级即时插话，换取跨 CLI、ACP、A2A 的一致语义。

## 5. `FOR UPDATE SKIP LOCKED` 解决什么

它解决多个 Worker 同时扫描队列时的重复领取和队头阻塞。一个 Worker 锁住候选行时，另一个 Worker 跳过它继续寻找下一条，而不是等待同一行。

它不解决 Agent 归属问题。Agent 已经绑定 Runtime，SQL 只筛选当前 Runtime 的 Task；多个 Worker竞争的是“谁执行”，不是“由哪个 Agent 执行”。

## 6. 为什么 Slot 必须在 claim 之前获取

如果先 claim 再等 Slot，Task 已从 queued 进入 dispatched 并持有租约，但本机没有容量启动进程，用户会看到虚假的执行中状态。先拿 Slot 意味着只在真正具备本地执行能力时才改变数据库状态。

当前 Slot 是进程内计数；如果一个 Runtime 后面部署多个 Daemon，需要把全局容量做成数据库令牌、集中调度器或节点级资源上报。

## 7. 租约能否保证任务只执行一次

不能。租约提供的是故障后的可恢复和旧 Worker 写回隔离，本质上更接近至少一次执行。Agent 在崩溃前产生的 Git 修改或外部 API 调用可能已经发生。Git 场景可依赖同一 Worktree 继续检查；外部业务工具仍需幂等键或补偿机制。

不要在面试中声称 exactly-once。

还要避免把 Mica 的租约字段直接套到 Multica：Multica 启动前使用 prepare lease，运行后更多依据 Runtime 心跳判断失联，并通过“旧执行失败 + 新 retry child”恢复；Mica 是同一 Task 过期重排并用 attempt 阻止旧 Worker 写回。

## 8. 为什么不是所有本地任务都创建 Worktree

Worktree 解决的是 Git 项目并行修改隔离，不是所有 Agent 执行的通用前提：

- 代码修改适合 worktree。
- 只读分析可以选 in_place，避免额外分支和目录。
- 不关联项目的问答使用隔离空目录。
- A2A 远程 Agent 根本不使用本机工作目录。

当前 `in_place` 同一路径只在单 Daemon 进程内串行；跨进程部署前需要更强的目录锁。

## 9. Worktree 与容器沙箱有什么区别

Worktree 隔离的是 Git 工作副本和分支，不是操作系统权限。Agent 仍然以 Daemon 用户身份运行，理论上能访问该用户可访问的其他路径。Claude 的自动授权同样不等于系统沙箱。

当前项目适合描述为“工作目录隔离”，不能描述为“容器级安全沙箱”。后续可以把 Provider 进程放入容器、macOS sandbox 或受限系统用户，并建立工具级审批策略。

## 10. 为什么 ACP 权限默认拒绝，而 Claude 自动允许

两者处于不同接入阶段和协议约束：

- ACP 明确定义 `session/request_permission`，平台可以从 Agent 给出的选项中选择并记录决定，所以默认 deny 更安全。
- Claude CLI 在无人值守 stream-json 模式下没有人工终端，当前实现参考 Multica 固定自动授权，并通过工作目录、前台进程约束和可信本机边界降低风险。

从长期架构看，两者应统一到平台权限策略，而不是永远保留这种差异。这是可以主动指出的安全演进项。

Multica 的 ACP 处理也比 Mica 当前 `permission_policy=allow` 的回退更保守：它优先复用 Session 级安全授权，其次只选择 `allow_once`，否则 `reject_once`，并拒绝永久 `allow_always`。因此不能笼统地说“参考 Multica 就自动允许所有 ACP 权限”。

## 11. “统一事件模型”具体是什么意思

它不是修改底层协议，而是在 Adapter 出口形成稳定事件：

```text
agent.text       文本进度
agent.tool       工具调用
agent.status     远端任务状态
agent.data       结构化数据
agent.permission 权限决策
agent.error      Agent 明确错误
```

Web 时间线、审计、取消结果和通知投影只消费这些事件，不直接解析 Claude、ACP 或 A2A 报文。协议特有字段仍保存在 payload 中，因此没有为了统一而丢失诊断信息。

## 12. 为什么 A2A 还需要 JSON Schema

A2A 解决“如何发现和调用 Agent”，不保证返回结果满足业务约束。远端服务 HTTP 200 或 Task completed 只能说明协议成功。Geometry Agent 如果应该返回坐标数组，却返回一段解释文本，上层仍然无法使用。

因此 output schema 是 Provider 成功和业务成功之间的校验门。当前只做确定性结构校验，更复杂的几何和目录约束应进入领域 Validator，而不是全部写进 Prompt。

## 13. 为什么 Squad 不直接并行调用所有成员

广播无法根据任务选择成员，也缺少结果取舍者。Leader-first 把自然语言拆分交给 Leader，把成员合法性和状态推进交给平台。只有被选择的成员才启动，所有成员结束后再唤醒 Leader 汇总。

代价是多一次或多次 Leader 调用，并且当前编排是批次循环，不是任意 DAG。任务稳定、成本和并行度之间的取舍要根据业务衡量。

其中“成员 Task 全部结束后汇总”是 Mica 的 sibling barrier。Multica 更常通过成员评论或子 Issue 委派，并根据子 Issue 的阶段状态决定何时回到父 Issue；共同点是 Leader 收口，不是 barrier 的具体表结构。

## 14. 为什么 Leader 必须输出 JSON

如果依赖自然语言解析“请 Geometry 处理”，平台无法可靠判断这是描述还是执行命令。JSON 使 `delegate/complete` 成为机器可验证决策。更重要的是，平台会校验 roster、重复成员和空指令；不是收到 JSON 就盲目执行。

生产化时可进一步使用模型原生 structured output 或 JSON Schema，而不是只做 `json.loads`。

“必须输出 JSON”只适用于 Mica 当前版本。Multica 将操作协议和 roster 注入 Leader，由 Leader 实际调用平台工具、@成员或创建子 Issue；系统根据真实操作记录 Squad activity。后续如果 Mica 引入完整工具调用，可把 JSON 决策升级为更自然的 tool-call 编排。

## 15. Worker 失败为什么不立即让 Squad 失败

成员失败只是一个局部结果。Leader 可能改派其他成员、降低范围或向用户请求补充信息，所以成员进入终态后仍回到 Leader。Leader 无法给出合法决策，或者最终明确无法完成时，才阻塞整个 Issue。

## 16. 飞书为什么需要外部事件去重

Webhook 平台通常是至少一次投递，超时或网络失败会重试同一事件。Mica 以渠道、租户和外部事件 ID 做唯一键，并保存 payload hash：同一内容返回原 Issue，不同内容复用同一 key 则冲突。这样既避免重复任务，也避免错误吞掉真正不同的请求。

Multica 的渠道去重是 `(installation_id, message_id)` 两阶段 claim/mark/release，没有 Mica 的 payload hash 冲突校验。payload hash 是 Mica 的额外保护，不应说成 Multica 的现成实现。

## 17. 为什么结果回传使用 Outbox

数据库完成 Task 和调用飞书 API 无法处于同一个分布式事务。Outbox 先把“应该发送什么”持久化，再异步投递，通过唯一键、租约和退避实现可靠重试。这是本地事务与外部副作用之间的常见一致性方案。

当前不是严格的端到端 exactly-once，但 Outbox ID 同时作为飞书 UUID，能把重复概率和影响降到较低。

Multica 解决同类问题时使用内部事件总线、Channel delivery 状态和 outbound ledger，并非 Mica 的 `TaskEvent → Projector → Outbox Worker`。两者都在处理“数据库提交与外部发送无法原子完成”，但实现边界不同。

## 18. 飞书是否实时同步所有 Agent 进度

当前没有。Web 展示 TaskEvent 时间线；飞书接收受理、等待补充和终态结果，Squad 内部成员的完成、失败、取消消息被折叠。面试中如果说“实时同步所有进度”，继续追问实现时会和代码不一致。

如果要实现，可以对高价值事件做节流聚合，例如每 10 秒更新同一张飞书卡片，而不是每个 token 或工具事件都发送新消息。

这里的限制只指 Mica 当前版本。Multica 的飞书 Channel 已覆盖线程、交互卡片、附件、输入状态和消息编辑，并通过 WebSocket 长连接收取事件；这可以作为 Mica 下一阶段的参考，而不是当前已实现能力。

## 19. 当前实现中值得主动承认的工程边界

- Runtime Slot 和 in_place 锁是进程内的，多 Daemon 全局协调尚未完成。
- Worktree 是目录/分支隔离，不是 OS sandbox。
- A2A 使用 HTTP 轮询，未实现 SSE streaming 和 Card 缓存。
- 飞书仅支持文本，尚未实现文件、图片、交互卡片和完整身份资料同步。
- Squad 是 Leader 驱动的批次循环，不是通用 DAG 工作流引擎。
- TaskEvent 没有独立 schema version；Projector 游标未持久化。
- 企业 SSO、Workspace RBAC 和审批策略仍是后续生产化内容。

主动说出一到两个与问题相关的边界，通常比把项目描述成无所不能更可信。

## 20. 从需求到当前版本的总体演进

```text
单渠道直接调用 Agent
    ↓ 长任务阻塞、重复回调
Issue + 持久 Task + Daemon
    ↓ 连续评论重复启动、并发竞态
统一 Ingress + DB 幂等 + queued 合并 + 输入快照
    ↓ 本地代码互相覆盖、崩溃卡死
Runtime Slot + 租约/attempt + Worktree/in_place/空 workspace
    ↓ 每种 Agent 都写一套上层逻辑
Provider 契约 + CLI/ACP/A2A Adapter + TaskEvent
    ↓ 多 Agent 广播浪费、结果无人收口
可配置 Squad + Leader-first + worker barrier
    ↓ 外部渠道结果可能丢失
IssueOrigin + Outbox + 飞书可靠投递
```

## 21. 代码与测试索引

| 主题 | 主要代码 | 关键测试 |
|---|---|---|
| 渠道幂等 | `mica/application/channels.py`、`channel_repository.py` | `test_channel_ingress.py`、`test_postgres_channels_integration.py` |
| 评论路由与合并 | `mica/application/services.py`、migration 009 | `test_comment_routing_and_merge.py` |
| 原子领取与租约 | `repositories.py`、`runtime/daemon.py` | `test_task_claim.py`、`test_orphan_recovery.py` |
| Worktree 与工作目录 | `runtime/worktree.py`、`runtime/workspace.py` | `test_local_agent_runtime.py`、`test_execution_context.py` |
| CLI | `runtime/providers/cli.py` | `test_local_agent_runtime.py` |
| ACP/A2A | `runtime/providers/acp.py`、`a2a.py` | `test_protocol_providers.py` |
| Squad | `application/squads.py` | `test_squad_orchestration.py` |
| 飞书与 Outbox | `integrations/feishu.py`、`delivery.py` | `test_channel_ingress.py`、`test_postgres_channels_integration.py` |
