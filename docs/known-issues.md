# Mica 已知问题与后续改进

本文集中记录 Mica 当前已经确认、但尚未进入实现阶段的问题。它用于后续需求评审、技术设计和开发排期，不作为已完成功能说明。

## 记录约定

每个问题尽量包含以下信息：

- **状态**：待评估、待设计、待实现、验证中、已完成或暂不处理。
- **优先级**：P0、P1、P2 或 P3。
- **当前行为**：代码现在实际如何运行。
- **期望行为**：业务上更合理的结果。
- **影响**：不修改会产生什么问题。
- **待决策项**：进入实现前仍需确认的产品或技术选择。
- **验收条件**：如何判断问题已经解决。

完成的问题不直接删除，应补充实现链接、测试范围和完成时间后移入文末“已完成”章节。

---

## KI-001：Chat 与 Issue 的交互模型混用

- **状态**：待设计
- **优先级**：P1
- **涉及模块**：飞书接入、Web 入口、Issue、Comment、Task、消息回传

### 当前行为

Mica 目前没有独立的 `ChatSession`、`ChatMessage` 和 `Chat Task` 模型。飞书文本消息进入系统后：

```text
飞书文本消息
  → 使用 parent_id / root_id 查找 IssueOrigin
  ├─ 命中：创建 Issue Comment，并创建或合并 Comment Task
  └─ 未命中：创建新 Issue，并立即创建 Issue Task
```

这意味着普通咨询和正式待办共用 Issue 模型。例如“解释一下这个接口”和“修复该接口并提交代码”都会进入看板。

相关实现：

- `mica/integrations/feishu.py`
- `mica/application/channels.py`
- `mica/application/services.py`

### 问题一：普通咨询会污染 Issue 看板

一次性问答也会获得标题、负责人、状态和执行记录。用户只是希望和 Agent 进行即时交流时，仍然必须承担创建和管理正式事项的交互成本。

需要区分两类意图：

```text
Chat：我现在想和某个 Agent 聊一件事
Issue：这件事需要被持续负责、执行和验收
```

### 问题二：连续消息不能稳定延续同一上下文

`IssueOrigin` 当前只记录创建 Issue 的原始飞书 `message_id`。后续输入只有在 `parent_id` 或 `root_id` 命中该消息时才会进入原 Issue。

可能出现以下结果：

- 回复最初的用户消息：进入原 Issue。
- 在群里直接补充消息：创建新的 Issue。
- 回复“任务已受理”等机器人消息：如果没有对应映射，创建新的 Issue。
- 回复 Agent 后续结果：如果没有对应映射，同样可能创建新的 Issue。

因此，“在同一飞书会话中继续交流”并不等于“延续同一 Mica 上下文”。

### 问题三：飞书回复关系没有写入 Comment 树

飞书适配器使用 `parent_id/root_id` 找到原 Issue，但追加 Comment 时没有传递 `parent_comment_id`：

```python
self.triggers.add_comment(issue.id, command.body, user.id)
```

最终创建的是顶级 Comment，而不是对某条用户或 Agent Comment 的回复。由此带来：

- Web 端无法还原真实的飞书讨论关系。
- 评论路由无法准确判断用户回复了哪个 Agent。
- Squad 场景可能错误地回退到 Issue 默认负责人或 Leader。

### 问题四：已有 Issue 中的 Agent 指定可能失效

飞书文本支持：

```text
agent=<agent-id> 任务内容
squad=<squad-id> 任务内容
```

适配器解析后会从正文中移除该部分。新建 Issue 时，解析出的 `assignee_id` 和 `assignee_type` 可以决定负责人；但如果消息被识别为已有 Issue 的 Comment，这两个字段不会传给评论路由。

由于 Comment 正文中的 `agent=...` 已被移除，`TriggerService._comment_targets()` 也无法将其识别为显式提及。因此用户回复已有 Issue 并指定新 Agent 时，任务可能仍交给原负责人。

### 问题五：Chat 合并与 Comment Task 合并不是同一语义

Mica 当前只实现 Comment Task 合并：同一 `(issue_id, agent_id)` 存在 `queued` Task 时，新评论通过 PostgreSQL UPSERT 合并进去；Task 被领取后，新评论进入后续 Task。

这适合正式事项的有序处理，但不完全适合即时对话。用户在短时间内连续发送多条 Chat 消息时，更合理的方式是：

1. 每条消息分别持久化为 ChatMessage。
2. 等待一个较短的静默窗口。
3. 将窗口内尚未消费的消息封装为一个 Chat Task。

不能直接复用 Issue Comment 的 queued 状态合并来替代 Chat 输入批次。

### 对照 Multica

Multica 将普通飞书消息建模为 Chat：

```text
普通飞书消息
  → ChatSession
  → ChatMessage
  → 短窗口合并
  → Chat Task
  → Agent 回复原飞书会话
```

只有显式 `/issue <标题>` 才创建 Issue 和 Issue Task。普通飞书回复仍然属于 Chat 上下文，不会自动变成 Issue Comment。

这个设计说明 Chat 与 Issue 可以复用底层 Task 调度、Provider、事件和取消机制，但不应共用同一个上层业务语义。

### 期望行为

建议后续形成两条明确入口：

```text
普通消息 / @机器人
  → ChatSession + ChatMessage
  → Chat Task

显式 /issue、Web 创建事项、Chat 转任务
  → Issue
  → Issue Task
```

其中：

- ChatSession 保存用户、Agent、渠道会话、话题和上下文代次。
- ChatMessage 保存每条用户输入和 Agent 回复。
- Chat Task 表示一轮 Agent 调用，消费一组确定的 ChatMessage。
- Issue 保存需要持续跟踪和验收的事项。
- Comment 保存围绕 Issue 的协作记录，并按提及和回复关系决定是否触发 Issue Task。
- Chat 可以显式转为 Issue，但不应默认把每次咨询都创建成 Issue。

### 待决策项

进入实现前需要确认：

1. 普通飞书私聊是否始终进入 Chat。
2. 飞书群聊是仅在 @机器人时触发，还是允许绑定专属话题后免 @。
3. 创建 Issue 是否只支持 `/issue`，还是同时提供飞书卡片按钮和自然语言确认。
4. 从 Chat 转 Issue 时，是提取摘要，还是由用户编辑标题、描述和验收条件后确认。
5. 一个飞书 ChatSession 默认绑定安装对应的 Agent，还是允许会话内切换 Agent。
6. 飞书话题是否按 `chat_id + thread_id` 隔离 ChatSession。
7. 外发受理消息、Agent 回复和卡片如何保存到 `issue_id/comment_id/task_id` 的反向映射。
8. Chat Task 的连续消息合并窗口和崩溃恢复策略。
9. Chat Task 与 Issue Task 是共享一张 Task 表并使用可空来源字段，还是使用独立输入表。

### 初步数据模型方向

```text
ChatSession
├─ workspace_id
├─ agent_id
├─ creator_user_id
├─ status
└─ context_revision

ChannelChatBinding
├─ chat_session_id
├─ channel / tenant_key
├─ conversation_id
├─ thread_id
└─ route_revision

ChatMessage
├─ chat_session_id
├─ role
├─ body
├─ external_message_id
├─ parent_message_id
├─ context_revision
└─ input_task_id

Task
├─ chat_session_id / chat_input_task_id
└─ issue_id / trigger_comment_id
```

以上只是用于后续方案评审的方向，不是已经确定的数据库设计。

### 验收条件

后续实现至少应满足：

- 普通咨询不会出现在 Issue 看板中。
- 同一飞书单聊或话题中的连续输入能稳定进入同一 ChatSession。
- 短时间内连续发送多条消息只触发一次 Agent，但原始消息分别保存。
- 用户可以显式把 Chat 转成 Issue，并能追溯来源 Chat 和输入消息。
- `/issue` 不会同时创建 Chat Task 和 Issue Task。
- 回复 Agent 的飞书消息能够映射到正确的 ChatMessage 或 Issue Comment。
- Issue Comment 能保留真实父子关系，并路由给正确的 Agent/Squad 角色。
- 重复飞书事件不会重复创建 ChatMessage、Issue 或 Task。
- Agent 结果能够稳定回传到触发该 Task 时确定的飞书会话或话题。

---

## KI-002：多 Agent 上下文不完整且 Session 作用域过粗

- **状态**：待设计（阶段一简化版已于 2026-10-06 落地：CLI Agent 的任务级只读通道 `mica-ctx`）
- **优先级**：P1
- **涉及模块**：Task Prompt、Provider Session、Squad、Worktree

### 当前行为

Mica 采用“平台拼接本轮 Prompt + 复用 Provider Session”的方式：

- 首次执行只传 Issue 标题和正文。
- 评论触发只增加 `delivered_comment_ids` 对应的本轮评论。
- Squad Worker 只得到标题、正文和 `handoff_note`。
- Squad Leader 额外得到成员清单，以及最近 20 条终态 Worker Task 的 `output/error` 摘要，每条最多 500 字符。
- Daemon 按 `(issue_id, agent_id)` 查找最近的 Session、分支和 Worktree 继续执行。

> **2026-10-06 进展**：CLI Provider（Codex/Claude）的任务现在额外携带 `mica-ctx` 读取通道：Daemon 为子进程注入 `MICA_API_URL`/`MICA_ISSUE_ID`/`MICA_TASK_ID` 并把入口前置进 PATH，Prompt 按冷/热恢复两档追加读取指令，Agent 可用 `mica-ctx comments` / `mica-ctx comment <id>` 按需拉取本 Issue 完整评论。实现见 `mica/ctx.py`、`mica/runtime/providers/base.py`（`build_child_env`、`build_task_prompt`）与提交 `c848ae2`；测试见 `tests/test_ctx_channel.py`、`tests/test_squad_orchestration.py::test_squad_member_prompt_carries_ctx_channel`。
>
> 剩余缺口不变：通道"可达"而非"必读"（无读取回执，取决于模型行为）；拉取的是读取时刻 live 现场，不冻结版本；ACP/A2A Provider 未接入；`MICA_TASK_TOKEN` 仅占位透传；文件级 Artifact 交接、Session 角色隔离、已读游标均未实现。

### 核心缺陷

1. **Worker 缺少共享上下文**：不会自动获得 Issue 历史评论、用户后续补充、其他 Worker 结果和 Artifact；Leader 如果没有在 `handoff_note` 中复述，Worker 就可能遗漏约束。（2026-10-06 起 CLI Worker 可经 `mica-ctx` 自助拉取评论全文，属部分缓解；ACP/A2A Worker 与"确保读到"仍未解决。）
2. **Leader 摘要不能作为事实源**：最近 20 条、每条 500 字符的 Task 输出可能截断 JSON、混入旧批次，也无法稳定引用附件和结构化结果。
3. **Session 作用域不区分角色**：仅使用 `(issue_id, agent_id)`，没有区分普通执行、Squad Leader、Squad Worker 和不同 Squad；同一 Agent 切换角色时可能恢复带有旧角色要求的 Session。
4. **Session 恢复缺少安全判断**：当前没有按 Runtime 和失败原因过滤不可恢复 Session；上下文溢出、Provider 拒绝旧历史或 Runtime 变化后可能继续使用无效 Session。
5. **Session 丢失后无法完整重建**：新执行只能重新得到标题、正文和本轮输入，之前只存在于 Session 或截断 Task output 中的结论可能丢失。
6. **缺少确定的跨 Agent 输入边界**：系统有评论的 `delivered_comment_ids`，但没有统一的 TaskResult、Artifact 引用、Issue context revision 和按 Agent 记录的已读游标。

### 典型场景：显式 @ Agent 时缺少上游上下文

Issue-A 的负责人是 Agent A，用户在评论中输入“`@agent:b 请审核 A 的方案`”后，系统会为 Agent B 创建 Task。B 当前只会得到自己的 Agent instructions、Issue 标题和正文，以及本轮指向 B 的评论；如果 B 曾处理过该 Issue，还会尝试恢复 B 自己的 Session。

B 不会自动得到 A 的 Session、A 已发布的结果评论、Issue 的其他历史评论或相关 Artifact。因此，“审核 A 的方案”虽然完成了路由，但没有建立对“A 的方案”的确定引用；用户必须重复粘贴内容，否则 B 可能在缺少审核对象的情况下执行。这说明显式 @ 目前只解决了**由谁执行**，没有解决**执行时依赖哪些上游上下文**。

### 期望方向

- Issue 继续作为可恢复的共享事实源，Provider Session 只保存某个 Agent 的私有连续记忆。
- 为 Task 保存本轮依赖的 Comment Thread、TaskResult 和 Artifact 引用，不依赖 Leader 复制长文本。
- Session 至少按 `(issue_id, agent_id, role, squad_id)` 隔离，并在恢复前校验 Runtime 和失败状态；Worktree 是否采用相同作用域单独决策。
- Worker 结果以可查询的 Comment、TaskResult 或 Artifact 持久化，Leader 使用引用读取，不再只依赖截断摘要。
- 引入 Issue context revision 和 Agent 已读游标：Session 正常时只提供上次已读版本之后的变化，Session 丢失或版本断裂时再重建完整上下文，避免照搬每轮全量读取造成的重复 Token 消耗。

### 验收条件

- Worker 能获得本轮明确依赖的用户评论和上游 Agent 结果。
- 显式 @ 其他 Agent 时，Task 能通过稳定引用获得被讨论的上游结果，而不要求用户重复粘贴内容。
- Leader 能通过稳定 ID 读取完整结构化结果，不受 20 条或 500 字符限制。
- 同一 Agent 的 Leader、Worker 和普通执行 Session 不会相互污染。
- 不可恢复 Session 会安全降级为上下文重建，而不是持续恢复失败。
- 能追踪每条 Task 实际消费的评论、结果和 Artifact。
- Leader 正常续接时只读取上下文增量，完整读取仅用于首次执行或恢复路径。

---

## KI-003：Task 准备阶段存在租约空窗

- **状态**：已修复（2026-10-08；"Worktree 准备可中断与失败清理"拆出为独立后续项，见待决策项 4）
- **优先级**：P1
- **涉及模块**：Daemon、Task Lease、Worktree/Session 准备、PostgreSQL Task Repository、Provider 取消

### 当前行为

- Daemon 领取 Task 时写入 `claimed_by` 和 `lease_expires_at`，并递增 `attempt`。
- 领取成功后立即启动心跳线程，`dispatched` 准备阶段（恢复 Session、创建或复用 Worktree、准备工作目录）与 `running` 执行阶段都处于续租保护下；心跳线程在 `run_once` 的 `finally` 中统一停止（2026-10-08 修复，见 `mica/runtime/daemon.py` `run_once()`）。
- 准备完成后、绑定执行上下文之前，若 `lease_lost` 或停机标记已置位，则放弃启动 Provider 且不做终态写回，任务交由过期恢复重排（2026-10-08 修复）。
- 心跳按固定周期调用 `renew_lease()`。返回 `False` 时立即设置 `lease_lost`；续约抛异常时记录日志并按心跳周期继续重试，距上次成功续约超过 `lease_seconds` 仍失败才设置 `lease_lost`。Provider 通过取消检查停止执行（2026-10-01 修复，见 `mica/runtime/daemon.py` `_heartbeat()`）。
- 轮询时会把租约已过期的 `dispatched/running` Task 恢复为 `queued`，供其他 Daemon 重新领取。
- Task 完成或失败写回时校验 `worker_id + attempt`，防止旧 Worker 覆盖新一轮结果。

### 核心缺陷

1. **准备阶段存在租约空窗**：初始租约从领取时开始计算，但心跳要等准备工作全部完成后才启动。如果 Worktree 或 Session 准备时间超过租约，其他 Daemon 可能把仍在准备的 Task 恢复并重新领取。
2. **旧代次校验只能保护最终状态**：`worker_id + attempt` 可以拒绝旧 Worker 的完成写回，但无法撤销已经发生的 Git 操作、文件修改或外部 API 调用，因此仍可能产生重复副作用和状态抖动。

> 原第 2 条"续租异常不会可靠触发停止"已于 2026-10-01 修复：心跳线程捕获续约异常后按本地租约截止时间重试，超时判定失租并设置 `lease_lost`。测试覆盖见 `tests/test_local_agent_runtime.py` 的 `test_heartbeat_renewal_errors_stop_execution_at_lease_deadline` 与 `test_transient_renewal_error_does_not_interrupt_execution`。
>
> 原第 1 条"准备阶段存在租约空窗"已于 2026-10-08 修复：心跳线程在 Task 领取后立即启动，准备阶段同样续租（`renew_lease` 本就覆盖 `dispatched` 状态，无 schema 变更）；准备完成后若已失租或停机，放弃启动 Provider 且不做终态写回。测试覆盖见 `tests/test_local_agent_runtime.py` 的 `test_heartbeat_renews_lease_while_preparation_runs`、`test_lease_lost_during_preparation_aborts_before_provider` 与 `test_second_daemon_does_not_reclaim_a_slowly_preparing_task`。

### 典型场景

Daemon A 领取 Task，得到 `attempt=1` 和 120 秒租约，随后开始创建较大的 Worktree。准备超过 120 秒且尚未启动心跳时，Daemon B 在轮询中将该 Task 恢复为 `queued`，再以 `attempt=2` 领取。此时 A、B 可能同时进行准备或执行；A 最终无法以旧 attempt 写回结果，但两边已经发生的文件系统或外部调用副作用不会自动消失。

### 期望方向

- ~~在 Task 成功领取后立即启动心跳，使 `dispatched` 准备阶段和 `running` 执行阶段都处于续租保护下；或显式区分 prepare lease 与 running lease。~~（已采用单一租约 + 领取后立即心跳。）
- 保留 `worker_id + attempt` 的条件写回，作为旧 Worker 晚到写回的最后一道保护。
- 明确 Worktree 准备被取消或失去租约后的清理策略，避免残留目录和分支。

### 待决策项

1. ~~准备阶段和运行阶段共用一个租约，还是分别设置 prepare lease 与 running lease。~~（已定：共用单一租约，领取后立即心跳；`renew_lease` 的 SQL 与内存实现本就覆盖 `dispatched`，无 schema 变更。）
2. ~~数据库短暂不可用时允许多少次失败、采用什么退避策略，以及何时判定租约丢失。~~（已定：窗口内按心跳周期重试，超过"上次成功续约 + `lease_seconds`"判定丢失。）
3. ~~无法确认租约时采用立即停止的 fail-closed 策略，还是允许在有限窗口内继续运行。~~（已定：窗口内继续以容忍瞬时抖动，窗口耗尽即停止，等价于租约失效点 fail-closed。）
4. Worktree 创建过程中如何响应取消，以及失败后的目录、分支清理边界。（2026-10-08 拆出为独立后续项：`prepare()` 保持原子完成，取消与失租仅在完成点检查；残留目录与分支按现状保留，作为同 Issue/Agent 复用语义的一部分。）
5. ~~多 Daemon 并发测试是否作为启用生产模式前的强制验收项。~~（已定：已补双 Daemon 并发测试，见 `test_second_daemon_does_not_reclaim_a_slowly_preparing_task`。）

### 验收条件

- 健康 Daemon 的准备时间超过初始租约时，Task 不会被其他 Daemon 重复领取。（已覆盖：`test_heartbeat_renews_lease_while_preparation_runs`）
- 短暂数据库异常在容忍范围内恢复后，Task 可以继续执行且不会重复领取。（已覆盖：`test_transient_renewal_error_does_not_interrupt_execution`）
- 续租长期失败时，Daemon 会在租约失效前后及时停止 Provider 和子进程。（已覆盖：`test_heartbeat_renewal_errors_stop_execution_at_lease_deadline`）
- Task 被新 Daemon 以更高 attempt 领取后，旧 Worker 无法写回终态。
- 两个 Daemon 并发运行且人为延长准备阶段时，不会同时执行同一 Task。（已覆盖：`test_second_daemon_does_not_reclaim_a_slowly_preparing_task`）
- 准备期间失去租约时，Provider 不会启动，任务被过期恢复重排。（已覆盖：`test_lease_lost_during_preparation_aborts_before_provider`）
- 心跳线程在成功、失败、取消和租约丢失路径中都能正常退出，不产生线程泄漏。（心跳在 `run_once` 的 `finally` 中统一停止，覆盖领取后提前失败的路径。）

---

## KI-004：Squad 成员代码工作区按 (Issue, Agent) 隔离，缺少协作共享与集成路径

- **状态**：已解决（2026-10-08，见文末"解决记录"；下文"当前行为"等小节保留对齐前的历史分析）
- **优先级**：P2（只影响"多个本地 Coding Agent 接力改同一份代码"的 Squad 场景；只读分析与远程 A2A 成员不受影响）
- **涉及模块**：Worktree、Daemon 执行上下文、Squad 编排；与 KI-002（共享上下文）联动

### 当前行为

- `WorktreeManager` 按 `(source, issue_id, agent_id)` 派生锁、分支名与目录（`mica/runtime/worktree.py`），Daemon 执行 Squad 成员任务时以成员自己的 `agent_id` 准备执行上下文。
- 同一 Issue 下，Leader 与各成员各自持有独立分支和目录，互不可见；远程 A2A 成员不分配本地工作目录。
- Worktree 在 Issue/Agent 间持久复用，首轮 fork 自当时的 HEAD；此后主干有新的合并提交，已存在的成员 Worktree 也不会吸收。
- 源仓库存在未提交修改时拒绝 prepare。

### 核心缺陷

1. **接力修改场景断裂**：Leader 在自己分支上的改动，成员的分支里看不到；成员之间 likewise。委派只携带 `handoff_note` 文本，代码状态不随委派流动。
2. **没有集成回路**：成员分支交付后没有合并或 rebase 机制；即便用户手工把某成员分支合入主干，其他成员已存在的 Worktree 仍停留在旧基线上。
3. **隔离粒度与协作单位不匹配**：工作区隔离单位是 `(Issue, Agent)`，而 Squad 协作的语义单位是 `(Issue, Squad)`。

### 典型场景

Issue 指派给 Squad：Leader（本地 Coding Agent）在自己分支搭好接口骨架并提交，随后委派成员实现模块。成员任务在 `(Issue, 成员)` 的 Worktree 中从旧 HEAD 建立/复用自己的分支——看不到 Leader 的骨架，只能凭 `handoff_note` 的文字描述重新发明接口，双方产出分叉，需要人工合并。

### Multica 参照（本机源码，HEAD ca47495）

- 分支归属"会话线"（Issue 或 Chat Session）而非 `(Issue, Agent)`：同一 Issue 的后续轮次交付到同一分支，分支名用 Issue identifier（用户在 `git branch` 里可辨认）。
- 但分支归属记录是 `(workspace, agent, conversation)` 三元组加 checkpoint，写在隐藏 ref（`refs/multica/local-state/`）里；**不同 Agent 不会领养同名分支**——同一 Issue 换 Agent 同样各走各的分支，Multica 也没有做 Squad 成员间的自动代码共享。
- 集成回路落在用户仓库：Worktree 是每个任务一次性的（env root 内，用完即回收），交付物是落在用户自己仓库里的分支；用户合并后，下一个任务从用户目录当前状态 fork（含未提交修改的快照重放），主干进展由此流入后续任务。
- 兄弟任务已占用会话分支时，降级 fork 任务级分支而不是拒绝运行（git 单分支单 Worktree 限制）。
- Squad 委派走子 Issue：每个子 Issue 是独立会话线，天然各自分支，集成由 Leader 汇总和用户合并完成。

### 期望方向

- 先明确 Squad 代码协作语义：成员独立交付 + Leader/用户合并（现状显式化），还是 Issue 级共享分支。
- 若共享：分支归属提升到 `(Issue)` 或 `(Issue, Squad)`，并发成员用目录锁串行（复用 in_place 思路）或各 fork 后合并；成员任务 fork 点改为共享分支 tip。
- 若不共享：补集成路径——成员分支合回主干后，既有成员 Worktree 的基线刷新策略（rebase、重建或明确提示）。
- 可借鉴 Multica：分支即交付物落在用户仓库、隐藏 ref 记录归属与 checkpoint、同名不领养、sibling 冲突降级为任务级分支。

### 实施路径（参考 Multica，分两期）

**第一期：先修通集成回路，不改变现有隔离语义。** 目标是让"用户合并成员分支 → 后续任务看到这份进展"这条回路闭合并可验证。

1. **合并检测与基线重置**：`WorktreeManager.prepare()` 复用已有分支前，先执行 `git merge-base --is-ancestor <branch> HEAD`。若分支已被用户合入主干，将分支重置到最新 HEAD 再挂出 Worktree（对应 Multica 的 `plan.reset` 路径，`git worktree add -B`）。这是回路的最小闭环。
2. **基线落后可见**：分支未合并但主干已前进时，在 TaskEvent 中记录基线落后主干的提交数（`git rev-list --count`），使用户和 Leader 能感知分叉程度；是否自动 rebase 由待决策项 2 决定。
3. **归属与 checkpoint 校验**：仿照 `refs/multica/local-state/`，为分支记录 `(agent_id, issue_id, tip_commit)`（隐藏 ref 或 Task 字段）；复用前校验记录的 tip 仍是分支祖先，防止用户删除并重建同名分支后发生误续接。
4. **未提交修改的处理**（独立工作量，可延后）：参考 Multica `captureUserSnapshot`，把用户的 tracked 修改与未跟踪文件打成临时 tree 注入新 Worktree（私有 `GIT_INDEX_FILE` 避免触碰用户索引，并以文件数/字节数上限兜底），替代当前直接拒绝的行为；不做则保持明确报错。

**第二期：Squad 代码协作语义落地，依赖待决策项 1 的结论。**

- 若走 Multica 路线（成员独立交付 + 用户/Leader 集成）：把"成员看不到彼此分支"写进 Leader briefing 与 `handoff_note` 约定，并将成员的 `branch_name` 与 tip commit 记入 Task 结果，供 KI-002 的依赖引用消费。
- 若走共享分支路线：分支归属提升到 `(Issue)`，并发成员复用 `LocalDirectoryLockPool` 的目录锁串行执行（daemon.py 已有机制），成员 fork 点改为共享分支 tip。
- 无论哪条路线，补 sibling 冲突降级：同一分支已被活跃任务占用时，fork 任务级分支继续执行（Multica `addLocalWorktree` 的兜底），而不是让任务失败。

### 待决策项

1. Squad 是否承诺成员间代码可见性，还是把集成责任留给用户与 Leader。
2. 共享分支模式下并发成员的互斥策略（目录锁串行 vs 各 fork 后合并）。
3. Worktree 持久复用 vs 每任务一次性 Worktree + 分支持久（Multica 模式）的取舍。
4. 远程 A2A 成员与本地成员混合 Squad 时，代码产物的传递方式（Artifact 引用？）。

### 验收条件

- Leader 与成员在同一 Issue 上的代码交付路径有明确语义，并有场景测试验证。
- 若支持共享：成员任务能看到 Leader 或前序成员在同一 Issue 下已交付的改动。
- 若不共享：委派时平台明确告知成员无法看到其他成员分支，并给出集成路径说明。
- 与 KI-002 的 Task 依赖引用联动：代码产物引用（分支、commit）可成为 Task 依赖声明的一部分。

### 解决记录（2026-10-08）

按"待决策项 1 = 成员独立交付 + 用户/Leader 合并、待决策项 3 = 每任务一次性 Worktree + 分支持久（Multica 模式）"落地，worktree 执行模式整体重写：

- **一次性副本 + 分支交付**：副本位于 Daemon env root（`<worktree-root>/<repo-hash>/<task-id>/worktree`），任务收尾后回收；唯一持久成果是用户仓库里的 `mica/<issue>/<agent>` 分支。集成回路闭合：分支被用户合入主干后，下一轮 `prepare` 经 `merge-base --is-ancestor` 检测并以 `worktree add -B` 重置到最新 HEAD（对应上文"实施路径"第一期第 1 条）。
- **归属与 checkpoint**（第一期第 3 条）：分支 tip checkpoint 存 `agent_task.recorded_tip`（migration 010），隐藏 ref `refs/mica/local-state/<branch>` 记录快照锚点与 finalize 时的 tip（第二父提交）。续接前校验 checkpoint 仍是分支祖先；DB 没有 checkpoint 时回落到隐藏 ref 的记录（覆盖 finalize 与回写之间的崩溃窗口，也让失败重试沿用原分支名）；两者皆无的同名分支（旧模型遗留、用户自建、删除重建）一律冻结、以 task 短后缀另开，不会被误续接。
- **脏仓库快照重放**（第一期第 4 条）：私有 `GIT_INDEX_FILE` 对用户工作区 `add -A` + `write-tree` + `commit-tree` 生成 user state 快照，重放进新副本（2000 文件 / 200MiB 上限，gitignore 自动排除）；用户目录全程不被写入。两轮之间用户改动与分支内容冲突时，冲突留在副本内、文件列表写入下一轮 prompt，Agent 未解决前 finalize 拒绝交付并保留现场。
- **交付契约**：finalize 把残留改动自动提交进分支（提交前剔除 Agent 工具落在副本里的会话状态目录如 `.omc/`，`git add -A` 对瞬态文件竞争有重试），结果评论自动附分支名与 tip（`git diff HEAD...<branch>` 即可审核），打通"A 生成 → B 审核"的引用传递——成员间代码不可见的语义不变，但引用传递不再需要共享分支。只读任务不留分支。
- **失败/取消**：副本保留现场（`worktree-preserved`），分支注册随即释放，Daemon 启动时 `sweep()` 清理任务已终结的 env root 与孤儿 ref。

残留边界：

- 依赖（node_modules 等）不跨轮保留，每轮副本需重装——已接受的代价，依赖全局包缓存摊销。
- finalize（提交分支）与任务状态回写之间存在崩溃窗口：分支已提交但任务标失败时，下一轮从分支 tip 续接不丢工作，符合既有 at-least-once 语义。
- 旧模型遗留的 `/tmp/mica-worktrees` 目录与 `mica/<issue>/<agent>` 分支全部冻结不续接，可手动删除。
- 待决策项 2（共享分支并发互斥）随"不共享"结论作废；待决策项 4（A2A 混合 Squad 的产物传递）仍开放。

---

## KI-005：Squad 批次屏障按血统识别成员任务，用户直接回复成员产生的任务完成后无人收口

- **状态**：待评估
- **优先级**：P2（不死锁、不丢信息，存在明确人工恢复路径；但会导致 Issue 停滞且无人感知）
- **涉及模块**：Squad 编排、评论路由、Issue 状态推进；与 KI-002（已读游标）、KI-004（Squad 协作闭环）联动

### 当前行为

- Leader 委派产生的成员任务带 `delegated_from_task_id=leader_task.id`（`mica/application/squads.py` `_apply_leader_decision`）；批次屏障 `_wake_leader_after_workers()` 以此为血统标记：同批兄弟全部落定（`waiting_input` 刻意算落定）后唤醒 Leader。
- 用户直接回复成员的结果/问题评论时，评论路由经 `source_task_id` 追溯到成员任务，新任务继承 `squad_id` 与 `is_leader_task`（`mica/application/services.py` `_comment_targets`），但 `delegated_from_task_id` 为空。
- 屏障的第一道门就是 `if not task.delegated_from_task_id: return`（squads.py `_wake_leader_after_workers`）：这类"用户直达"任务完成时直接返回，Leader 不会被唤醒。
- 成员任务完成走 `squads.complete_task()`，固定 `advance_issue=False`：Issue 状态也不会随任务完成推进。
- 信息层面没有丢失：该任务带 `squad_id`，会出现在 Leader 简报的 "Recent delegated results" 中；但 Leader 需要一个唤醒理由才会去读。

### 典型场景

1. 成员 B 缺信息挂起：任务 `waiting_input`，问题以评论发出，Issue → `blocked`，批次屏障正常唤醒 Leader。
2. Leader 也不知道答案，以 `decision=complete` 升级：汇总评论请用户确认，Issue → `in_review`。
3. 用户没有回复 Leader 的汇总评论，而是直接回复 B 的问题评论给出答案 → 新任务派给 B（继承 squad 上下文，但无血统标记）。
4. B 恢复 Session 完成工作并发布结果评论；任务完成不唤醒 Leader，Issue 停在 `in_progress`（任务开始时被推入）。
5. 结果：工作已完成、结果已发布，但 Leader 不知道要收口，Issue 状态无人推进，直到有人主动回复 Leader 或新发评论。

### 核心缺陷

1. **屏障语义是血统驱动而非状态驱动**：只认"哪次 Leader 委派产生了这个任务"，不认"Squad 成员产出了 Leader 尚未消化的结果"。用户在 Issue 上的自然动作（哪里有问题就在哪里回复）恰好落在血统之外。
2. **唤醒时机缺失而非信息缺失**：成员结果对 Leader 可见（简报拉取），但没有任何机制保证 Leader 会被叫醒去看。
3. **Issue 状态滞留**：`advance_issue=False` 加上 Leader 不醒，已完成的工作停留在中间状态，看板上表现为"永远在进行中"。

### Multica 参照（本机源码，HEAD ca47495）

Multica 的父子联动是状态驱动：`issue_child_event` 由数据库触发器在"子 Issue 关闭/重开/改 stage"这一事实上生成（migration 558），与谁触发、经由哪个任务无关；`child_done` 系统规则据此唤醒父 Issue 负责人，仅用 `source_task_id` 防止 agent 被自己的动作回声唤醒。用户直接推进子 Issue 同样会触发父级收口。

### 期望方向

- 将 Leader 收口条件从"批次血统"扩展为"存在 Leader 尚未确认的成员终态结果"：可复用 KI-002 规划的 Agent 已读游标表达"Leader 已看到哪"，成员任务完成/挂起时若结果未被消费则唤醒 Leader。
- 或在路由层分流：用户回复 `waiting_input` 成员的问题评论时，除派给成员外同时给 Leader 记一条待确认标记（不额外执行，只保证后续收口）。
- 无论哪条路线，明确"用户直达"场景下 Issue 状态的推进责任（任务完成后自动回到 `in_review`，还是留给 Leader 收口时处理）。

### 待决策项

1. 用户直达任务完成后是否应唤醒 Leader（实时收口），还是仅置"未确认"标记等下次唤醒时合并消费。
2. "Leader 已确认"的表达方式：已读游标、结果确认事件，还是复用现有评论线程。
3. 用户直达场景下 Issue 状态的自动推进规则。
4. 防回声边界：Leader 自己委派链产生的结果不应因新条件被重复唤醒（现有 `has_pending` 门仍需保留）。

### 验收条件

- 复现"典型场景"时间线：用户直接回答挂起成员后，Leader 能在可预期时机收口，Issue 不停留在中间状态。
- 纯批次流程行为不回归：同批成员全部落定仍只唤醒一次 Leader，无重复唤醒。
- Leader 连续因不同原因被唤醒时，不会因同一成员结果重复收口。

---

## KI-006：@mention 目标无效导致整条评论创建失败

- **状态**：待评估
- **优先级**：P2（用户输入丢失，但仅限提及解析失败的少数情况）
- **涉及模块**：评论路由、Web/API、渠道接入

### 当前行为

`TriggerService.add_comment()` 把评论落库与路由放在同一事务内（`mica/application/services.py`）：评论先写入，随后 `_comment_targets()` 解析 `@agent:ID` / `@squad:ID` / `mention://...`。目标 Agent 不存在、Squad 不存在或不属于本工作区、未配置 squad 支持时使用 @squad，都会抛出 NotFoundError，事务整体回滚——**评论本身也不会保存**。

### 影响

- 用户输错 ID（或复制了已删除、其他工作区的 ID）会丢失整条评论内容。
- 飞书等渠道侧表现为"发了没反应"或整条报错，用户无法区分是渠道故障还是提及无效。

### Multica 参照（本机源码，HEAD ca47495）

评论照常保存，响应里用 `trigger_outcomes` 逐目标报告每个显式提及的派发结果（queued / coalesced / blocked 及原因码），被阻止的提及不会拖垮评论本身（`server/internal/handler/comment.go` 的 `CommentTriggerOutcome`，MUL-4525）。

### 期望方向

- 落库与路由解耦：评论先持久化，提及派发失败降级为逐目标的结果记录（可借鉴 `trigger_outcomes`），并在 UI/渠道提示"评论已保存，但 N 个目标未触发"。
- 或至少在保存前做轻量预校验，只让真正无法落库的情况失败。

### 待决策项

1. 解耦后失败提及的补偿方式：自动重试、界面提示后手动重发，还是仅记录。
2. 渠道侧（飞书）如何向用户回传"部分目标未触发"。
3. 是否为内部 API 调用方保留整体回滚的严格模式。

### 验收条件

- 评论内容不因提及解析失败而丢失。
- 每个显式提及的派发结果可见、可追踪。
- 现有合法提及的路由行为不回归。

---

## KI-007：回复路由的线程归属只认根评论，中途参与的 Agent 不接管线程

- **状态**：待评估
- **优先级**：P3（边界场景；存在明确变通：直接回复 Agent 评论或重新 @）
- **涉及模块**：评论路由；与 KI-005（Squad 收口）联动评估

### 当前行为

回复追溯的第三子规则（`_comment_targets()` 2c）沿 `parent_comment_id` 一路爬到线程根，中途不停，然后仅以**根评论**匹配 Task（`trigger_comment_id == 根.id` 或根 id 属于 `merged_comment_ids`）。中途通过 @mention 进入线程的 Agent，其 Task 的触发评论是中间的某条，根匹配不到。

### 典型场景

1. Issue 在 backlog 期间存在一条纯人工讨论线（状态门禁，未触发任务）。
2. Issue 激活后，有人在线程中间 `@agent:b` 提问：B 执行任务并发布结果评论。
3. 用户随后回复该线程中另一条**人写的**评论（不是回复 B 的结果）。
4. 爬根到顶，根从未触发任务 → `return []`，不触发任何 Agent——尽管 B 正在这条线上活跃。

### 设计语义

当前实现选择"**根归属**"：一条线程归其根评论当年触发的 Agent 所有；中途被 @ 进来的 Agent 只对**直接回复其结果**的评论负责（由 2a 兜住）。另一种语义是"**最近 Agent 祖先归属**"：根无匹配时，在祖先链上找最近的带 `source_task_id` 或 agent 作者的评论。两者没有绝对对错，前者简单可预测，后者更贴合"谁在这线活跃就交给谁"的直觉。

### 期望方向

- 二选一或折中（根优先、落空再试最近 Agent 祖先），并把选定语义写进路由文档。
- 若维持现状，至少在文档中明确这是有意为之的边界。

### 待决策项

1. 是否变更线程归属语义。
2. 折中方案的优先级顺序（根匹配与最近祖先的先后）。
3. 与 KI-005 的收口问题联动：归属变化是否影响 Squad 批次屏障。

### 验收条件

- 混合线程（人工讨论 + 中途 @ 引入 Agent）中各类回复的路由结果与文档描述一致。
- 纯人工讨论不触发 Agent 的现有行为不回归。

---

## KI-009：租约恢复与 queued 唯一索引冲突，已有后续任务时无法恢复过期执行

- **状态**：待设计（已在 PostgreSQL 复现；2026-10-06 核对 Multica 方案并补充设计方向）
- **优先级**：P1
- **发现日期**：2026-10-02
- **涉及模块**：PostgreSQL Task Repository、Daemon 恢复、评论合并、任务优先级；关联 KI-003

### 当前行为与复现

`PostgresTaskRepository.recover_expired()` 将本 Runtime 下所有租约过期的 `dispatched/running` Task 直接更新为 `queued`。migration 009 同时定义了部分唯一索引 `agent_task_one_queued_per_issue_agent_idx`，要求同一 `(issue_id, agent_id)` 最多存在一个 queued Task。

正常执行期间收到追问时，平台允许保留一个 queued successor；原任务一旦过期，恢复更新便会与这个 successor 冲突。

2026-10-02 在独立临时 PostgreSQL 中按以下步骤复现，无真实 Agent 调用：

1. 创建 Issue，领取并启动首 Task。
2. 在首 Task 运行期间追加评论，生成 queued successor。
3. 将首 Task 的 `lease_expires_at` 设为过去时间。
4. 调用 `recover_expired(runtime_id)`。

实际结果：抛出 `UniqueViolation: duplicate key value violates unique constraint "agent_task_one_queued_per_issue_agent_idx"`，恢复事务回滚，两条 Task 仍为 `running + queued`。

### 影响

- successor 因原任务仍处于活动状态而不能领取，原任务也不能恢复，形成持续阻塞。
- 恢复 SQL 是批量更新，一条冲突可能使同 Runtime 其他过期任务的恢复一起回滚。
- `Daemon.run_once()` 在领取新任务之前执行恢复，因此该冲突也可能阻断本 Runtime 的正常领取；异常返回 `True` 后，Worker 可能持续重试并反复输出错误日志。
- 内存仓储未实现这个唯一约束，现有内存恢复测试无法发现 PostgreSQL 的冲突。

### Multica 参照（本机源码，HEAD 2ea01ae，2026-10-06 核对）

Multica 用"旧行终态化 + 新子行重试"取代"旧行改回 queued"，并用优先级保证恢复轮次先于新输入执行：

1. **终态化 + 同事务子行**：租约过期/Runtime 失联的任务由 sweeper 判 `failed`（`failure_reason` 分类：`queued_expired`/`runtime_offline`/`timeout`/`runtime_recovery`），终态不在唯一索引内，槽位自然释放。重试是新子行（`CreateRetryTask`），与 fail 同一事务创建（MUL-4351），关闭"旧行已死、重试未出现"的插队窗口。子行经 `parent_task_id` 记录血缘，继承 trigger/coalesced 评论、squad 血缘与 `attempt+1`；`delivered_comment_ids` 刻意清空，领取时重新固化自己的输入快照。
2. **撞索引让位而非报错**：`CreateRetryTask` 用 `ON CONFLICT DO NOTHING`——槽位已被 queued 后继占据时不报错、不创建，调用方视为正常结果（策略：谁在槽位谁优先，人工 rerun 赢过自动重试）。需要退避的重试先放 `deferred` 状态（刻意排除在索引外，可与 queued 共存），由 `PromoteDueDeferredTasksForRuntime` 提升：槽位被占则跳过、每 `(issue, agent, thread)` 分区 `row_number()` 最多提升一行（MUL-4257；其注释证实曾发生"一条冲突阻断整个 Runtime 领取"的同类事故）。跳过的行保持 deferred 等下一 tick，不丢任务。
3. **防重复子行**：`parent_task_id` 上只有普通索引，没有唯一约束。"一个父任务最多一个子任务"靠状态流转一次性（标失败的 UPDATE 带状态谓词，只赢一次）+ 事务原子性保证；另有 `HasRetryTaskForParent` 检查与 `CancelSupersededDeferredRetries` 定期清理兜底。重试次数按失败原因分级设上限（`retryAttemptCeiling`）。
4. **优先级体系**：`agent_task_queue.priority INT NOT NULL DEFAULT 0`，无专门索引。赋值：Issue 任务映射业务优先级 urgent=4/high=3/medium=2/low=1/none=0；Chat 固定 2；Autopilot 固定 0；**Chat 重试子任务 `GREATEST(父, 3)`**——动机是用户在失败轮次运行期间发的新消息会排成 priority 2 的新任务，不提级则同级按 `created_at` 让新输入先跑，旧轮次的上下文顺序颠倒。消费点：领取 `ORDER BY priority DESC, created_at ASC`；deferred 提升与 reclaim 同序。无优先级变更传播；评论合并（coalesce）不改动已有排队任务的优先级。
5. **Sweeper 工程化**：`FOR UPDATE SKIP LOCKED` 选 victim + UPDATE 时复查谓词 + 每 tick `LIMIT` 上限，单条冲突不拖垮整批。

**不能照抄的一点**：Multica 让位安全，是因为 chat 重试链经 `chat_input_task_id` 继承读同一批根输入；Mica 的 queued successor 只携带新评论，过期任务已消费的 `delivered_comment_ids` 不在其中。重试让位时必须把原输入并入 successor 的合并集合，否则首轮输入丢失。

### 期望方向与待决策项

按 Multica 方案对齐（2026-10-06 评估，暂未实现）：

- 恢复不再 UPDATE 旧行为 queued；改为逐行事务：旧行 `failed`（带状态谓词只赢一次）+ INSERT 重试子行（血缘列、`attempt+1`、继承输入快照），`ON CONFLICT DO NOTHING` 让位；**让位时把原 delivered 输入并入已有 successor**。
- 引入任务优先级，先只做内部排序键、不做用户可见功能（YAGNI）：`agent_task` 加 `priority INT NOT NULL DEFAULT 0`；普通任务 0、恢复重试 1；`claim_next` 改 `ORDER BY priority DESC, created_at`；`enqueue_or_merge` 的 `DO UPDATE` 不动 priority（合并不改变已有排队的紧迫度）；内存仓储排序同步保持一致。改动评估：migration 约 10 行，任务创建已收口在 `TaskService.enqueue()` 一处，赋值只改一处；存量数据 priority=0 时排序行为与现状完全等价。
- 恢复重试设次数上限，避免无限复活。
- 明确“恢复旧执行”和“执行后续输入”的优先级，保证恢复路径与 queued 唯一约束兼容。
- 保留原 Task 的执行快照、结果历史与 attempt 防护，明确取消标记在恢复时的语义。
- 单个任务恢复失败不应无限阻断无关任务的调度。

待决策项：

1. 优先级做内部排序键（建议，两档起步）还是用户可见功能（需要 Issue 加业务优先级字段，外延大）。
2. 重试让位时原输入并入 successor 的具体方式与去重规则。
3. 恢复重试次数上限与失败原因分类粒度。
4. 是否需要 `deferred` 缓办状态（可先不做，直接 ON CONFLICT 让位 + 优先级）。

### 验收条件

- PostgreSQL 场景覆盖“原任务过期 + 已有 queued successor”，恢复成功且后续输入不丢失、不重复消费。
- 同一 Issue/Agent 仍不并发执行两个 Task。
- 旧 Worker 的晚到结果不能覆盖恢复后的执行。
- 一个冲突任务不会导致同 Runtime 所有任务持续停止领取。
- 恢复重试排在同 Issue/Agent 新输入之前被领取；不同 Issue/Agent 之间高优先级先领取、同优先级保持 FIFO。
- 评论合并不改变已有排队任务的优先级；内存与 PostgreSQL 仓储的领取排序行为一致。
- 内存与 PostgreSQL 仓储在该场景下的可观察行为一致。

---

## KI-010：事件时间游标无法处理事务乱序提交，实时推送与通知投影可能漏消费

- **状态**：待设计（已在 PostgreSQL 复现）
- **优先级**：P1
- **发现日期**：2026-10-02
- **涉及模块**：PostgresEventRepository、DatabaseEventRelay、TaskNotificationProjector、Outbox

### 当前行为与复现

`TaskEvent.created_at` 在 Python 创建对象时生成，并不代表数据库提交时间。`PostgresEventRepository.list_after()` 按 `(created_at, id)` 查询和排序；实时 Relay 与通知 Projector 消费后均将游标推进到最后一条可见事件。

2026-10-02 在独立临时 PostgreSQL 中用两个事务复现：

1. 事务 A 创建并插入事件 A，保持事务未提交。
2. 事务 B 创建时间更晚的事件 B，并先提交。
3. 消费者读到 B，将游标推进至 B。
4. 提交事务 A，再调用 `list_after(B.created_at, B.id)`。

实际结果：A 已存在于事件表，但游标后查询结果为空，消费者无法再读到 A。

### 影响

- WebSocket 的增量事件可能遗漏，重新查询历史才会看到事件。
- 终态事件若被跳过，通知 Projector 不会创建对应 Outbox，飞书用户可能收不到完成、失败或等待输入通知。
- Projector 重启会从头扫描，可能补回遗漏事件；当前进程内没有自动补偿保证。Relay 启动时从最新历史事件初始化游标，也不提供遗漏事件的实时补发保证。
- “只消费已提交事件”已实现，但不等于“每条提交事件都能被消费”。

### 期望方向与待决策项

- 为需要可靠投递的消费者建立持久化消费记录或事务内投影/Outbox，避免将对象创建时间当作提交顺序。
- 若使用重叠窗口扫描，需定义幂等、窗口之外晚提交事件的补偿，以及扫描成本。
- 单纯改成自增 ID 不能自动解决问题：序列分配顺序同样可能与事务提交顺序不同。
- 区分实时展示允许的延迟与终态通知必须保证的投递语义。

### 验收条件

- 用两个独立连接和同步屏障控制 A 晚于 B 提交，确认两条事件都最终被消费。
- 被延迟提交的终态事件最终生成一个 Outbox；重复扫描或重启不生成重复通知。
- 回滚事件不会对外发布，消费者失败恢复后不漏事件。
- 不依赖真实 Agent 或飞书服务即可稳定复现和验证。

---

## KI-011：Web 修改 Runtime 并发容量不会同步到独立 Daemon

- **状态**：待设计（代码确认）
- **优先级**：P2
- **发现日期**：2026-10-02
- **涉及模块**：Runtime API、ApplicationContainer、RuntimeSlotPool、Daemon 配置

### 当前行为与影响

`PATCH /api/runtimes/{runtime_id}` 保存 `max_slots` 后，只调用 API 进程容器内的 `state.slots.configure()`。正式 PostgreSQL 部署默认由独立 `mica-daemon` 进程执行任务，该进程的 SlotPool 与 API 不共享。

Daemon 的定期探测会读取 Runtime 并更新发现信息，但没有将数据库里的新 `max_slots` 应用到自身 SlotPool。启动时默认 Runtime 又由 `MICA_MAX_SLOTS` 构造并写回，单纯重启也不保证保留 Web 中保存的容量。

因此界面显示已修改容量，实际执行并发仍按 Daemon 原容量运行；嵌入式 Daemon 与独立进程模式行为不同。

### 期望方向与待决策项

- 明确数据库配置与环境变量的优先级，定义启动初始化、运行中刷新及容量下调的行为。
- 独立 Daemon 定期刷新容量，或接收配置变更信号，并报告实际生效值。
- 下调容量时保留正在运行的任务，只限制后续领取。

### 验收条件

- 两个独立容器/进程共享数据库时，API 修改容量后，Daemon 在明确时限内生效。
- 提高容量可以增加后续并发；降低容量不取消已有任务且不继续超额领取。
- 重启后的容量符合已约定的配置优先级，界面展示与实际容量一致。

---

## KI-012：Daemon 按整批任务等待，空闲 Slot 不能及时补位

- **状态**：待设计（代码确认）
- **优先级**：P2
- **发现日期**：2026-10-02
- **涉及模块**：DaemonWorker、RuntimeSlotPool、任务调度

### 当前行为与影响

`DaemonWorker._run_batch()` 按当前容量创建一批线程，每个线程仅执行一次 `run_once()`，然后依次 `join()` 全部线程；只有整批结束，才开始下一批领取。

例如容量为 2，Task A 长时间运行、Task B 很快结束：B 释放的 Slot 不会立即领取 queued Task C，必须等 A 结束。若本批启动时只有 A，另一线程领取为空后退出，稍后到达的任务同样要等待 A；`wake()` 不能打断正在等待线程结束的批次。

### 期望方向与待决策项

- 使用持续消费的 Slot Worker 或完成后补位的调度循环，使空闲容量可以独立领取任务。
- 保留先获取 Slot 再领取、同 Issue/Agent 串行及 Squad 屏障约束。
- 定义运行中容量调整、关闭与唤醒如何协调。

### 验收条件

- 容量为 2，A 被屏障阻塞、B 完成后，C 能在 A 释放前开始执行。
- 本批开始后新增任务也能利用空闲 Slot，无需等待其他长任务结束。
- 不超过配置容量，停止 Worker 后不再领取新任务。

---

## KI-013：Pi Provider 未注入 Agent instructions，无法获得配置角色及 Leader 简报

- **状态**：待实现（代码确认）
- **优先级**：P2
- **发现日期**：2026-10-02
- **涉及模块**：PiProvider、Agent 指令、Squad Leader

### 当前行为与影响

Daemon 将 Agent instructions 与 Squad Leader briefing 合并后传给 Provider。Codex、Claude、ACP、A2A 均有对应的指令注入路径，但 `PiProvider.run()` 调用 `_prompt(issue, task)`，该方法只返回 `build_task_prompt()`，完全没有使用 `agent.instructions`。

- 普通 Pi Agent 的自定义角色、约束与协作规则不进入本轮输入。
- Pi 作为 Leader 时，无法通过平台注入获得 roster、成员能力和 `delegate|complete` 输出协议，可能返回普通文本并被 Squad 决策校验拒绝。

### 期望方向

- 为 Pi 定义与其他 Provider 一致的指令传递机制，并确保每次 Leader 唤醒时获得最新简报。
- 兼容会话续接，避免遗漏最新规则或无控制地重复堆积指令。

### 验收条件

- 伪 Pi CLI 捕获的实际输入包含 Agent instructions 与本轮任务内容。
- Pi Leader 输入包含 roster、Squad instructions 及结构化决策要求。
- 会话续接、评论快照与取消行为不回归。

---

## KI-014：就绪检查没有验证执行节点存活和 Provider 实际可执行性

- **状态**：待设计（代码确认）
- **优先级**：P2
- **发现日期**：2026-10-02
- **涉及模块**：健康检查 API、RuntimeDiscoveryService、Web 服务状态

### 当前行为与影响

`/health/ready` 当前检查数据库连接、Runtime 是否存在、Agent 引用的 Runtime 和 Provider 是否已注册。它没有检查独立 Daemon 是否仍在运行、`last_seen_at` 是否过期、已探测 CLI 是否可用或认证是否有效。

`RuntimeDiscoveryService.probe()` 会把 Runtime 标为 `online`；该标记没有过期判定，并且 API 的探测接口也能更新它，不能直接等同于执行 Worker 存活。

因此 Daemon 已停止或 CLI 不可执行时，API 仍可能返回 ready，Web 显示“服务已就绪”。README 对“同时验证数据库、Runtime 与 Provider 配置”的表述需要明确检查深度。

### 期望方向与待决策项

- 区分 API 可接收请求与任务执行链可用，明确 `/health/ready` 的契约。
- 执行节点存活由 Daemon 自己报告，并对过期报告判定离线，避免 API 探测伪装成 Worker 心跳。
- 定义 CLI 发现、登录状态与远程 Provider 可达性的检查方式、缓存和成本；健康检查不应启动真实模型任务。

### 验收条件

- 独立 Daemon 停止超过约定时限后，执行可用性状态可准确反映故障。
- Runtime 存在但没有可执行 Provider 时，界面不会误报任务执行就绪。
- API-only 模式、嵌入式模式和独立 Daemon 模式有一致且明确的健康契约。

---

## KI-016：贡献与安全文档仍以教学原型为基线，与正式包定位不一致

- **状态**：待实现（文档确认）
- **优先级**：P3
- **发现日期**：2026-10-02
- **涉及模块**：CONTRIBUTING.md、SECURITY.md、README、早期 OpenSpec

### 当前行为与影响

README 已将 `mica/` 定义为可独立安装的正式后端，并将 `sessions/zh/` 明确为历史原型，但：

- `CONTRIBUTING.md` 仍要求“新增概念优先放到新章节”，本地编译命令也只覆盖 `sessions tests`。
- `SECURITY.md` 仍主要描述教学 demo、`ClaudeCliRunner` 和 `/api/fs/browse` 等历史接口/类名，未完整反映正式包的渠道、远程 Provider 与执行权限边界。
- 早期 OpenSpec 的勾选项包含 SkillLoader、Autopilot 等历史原型能力，读者可能将其误认为正式包已实现。

这些差异容易使贡献者改错实现位置、遗漏正式包检查，或误解当前可用能力及部署边界。

### 期望方向与验收条件

- 贡献文档分别说明正式包开发与历史章节维护规则，验证命令对齐现有 CI。
- 安全文档以正式入口、Provider 权限和可信本机部署假设为基线；历史说明明确标注作用范围。
- 为早期 OpenSpec 标注历史原型范围，并链接当前架构与验收清单。
- 阅读项目入口文档即可区分当前实现、历史原型和待开发能力，不把未验收联调当作已验证能力。

---


---

## 已完成

## KI-015：正式包格式检查失败，现有 CI 门禁无法通过

- **状态**：已完成正式包格式门禁修复（2026-10-04）；测试目录 lint 债务单独保留。
- **优先级**：P2（阻断现有 CI 格式门禁）
- **发现日期**：2026-10-02
- **涉及模块**：代码格式、CI、测试目录维护

### 修复前行为与证据

本次使用仓库 `.venv/bin/ruff` 检查：

- `ruff check mica` 通过。
- `ruff format --check mica` 失败：8 个文件需要格式化，34 个文件已符合格式。涉及 `api/app.py`、`application/projects.py`、`application/services.py`、`application/squads.py`、`infrastructure/database/repositories.py`、`infrastructure/memory.py`、`runtime/daemon.py`、`runtime/providers/cli.py`。
- `ruff check tests` 报告 48 项问题，包含导入排序及未使用导入/变量等；当前 CI 未对 tests 运行 lint，因此这部分属于额外维护债务。

`.github/workflows/tests.yml` 明确包含 `ruff format --check mica`，故当时工作树无法通过该门禁。检查结果针对 2026-10-02 工作树，包含当时已有未提交修改，不代表所有问题均由本轮修改引入。

### 期望方向与验收条件

- 独立提交正式包格式整理，避免与行为修复混在一起。
- 明确历史原型测试是否纳入统一 lint，以及增量清理边界。
- CI 中的 `ruff check mica`、`ruff format --check mica` 均通过，行为测试不回归。

### 完成记录

- 2026-10-04：正式包剩余格式问题已处理，`ruff check mica` 与 `ruff format --check mica` 均通过（44 个文件符合格式）。
- CI 同步改为运行 `tests/web*.test.mjs`，覆盖既有前端逻辑与新增删除提交状态测试，共 15 项通过。
- 全量 Python 测试 277 项通过；依赖独立数据库的 5 项测试已在临时 PostgreSQL 中另行通过。
- 格式及 CI 调整提交：`e27c0cf`；测试目录的统一 lint 范围与历史原型维护仍属于后续独立工作。

---

## KI-017：缺少事项删除能力及对应交互

- **状态**：已实现第一版（2026-10-04）。
- **优先级**：待评估。
- **涉及模块**：Issue 生命周期、Web 菜单与确认弹窗、Task 取消与调度、Squad 协作、持久化数据清理。
- **记录日期**：2026-10-03。
- **方案确认日期**：2026-10-03；2026-10-04 简化为参考 Multica 的第一版。
- **当前确定的方向**：参考 Multica 的交互和前端设计，第一期仅做 Web 单项永久删除；所有状态均可发起删除，不做归档、回收站或批量删除。
- **实现范围**：Web 单项删除、关联记录事务清理和执行器异步停止；未新增表结构。
- **参考版本**：本机 Multica 源码，HEAD `2ea01ae4e`。

### 实现前缺口

Mica 的 `done` 事项保留在已完成列，`cancelled` 事项被默认看板隐藏。实现前没有事项归档字段、事项删除 API 或前端删除入口，用户无法通过界面清理误建或不需要保留的事项。

### Multica 参考行为

- 详情页右上角更多菜单、列表/卡片右键菜单均提供红色“删除任务”操作，复用同一确认弹窗。
- 确认弹窗说明永久删除及不可撤销的后果，提供“取消”和“删除”按钮。
- 单项删除期间显示“删除中…”，禁用按钮与关闭操作；成功后提示“已删除任务”，失败可重试。
- 详情页删除成功后返回此前列表，无应用内历史时回到默认事项列表；列表内删除留在当前列表。
- 多选事项后，底部浮动工具栏提供批量删除，确认弹窗展示数量。
- 后端将关联活动任务在数据库中标记取消，随即硬删除事项，不等待执行器停止；删除接口也未检查取消函数返回的错误。Daemon 随后发现任务取消或不存在，再中断执行，生产默认轮询间隔为 5 秒。
- 后端清理关联数据及附件，解除子事项的父级关系，并广播删除事件。
- Multica 删除事项来源上下文，不保留已删除 Issue 的专用消息标记。普通渠道回复进入 Chat Session，显式 `/issue` 命令才创建事项；独立消息去重不随 Issue 删除。去重清理 SQL 当前没有调用，不能把注释中的 24 小时视为已启用的保留期限。
- 当前查阅的 Multica 事项功能没有独立归档操作；隐藏状态列属于视图偏好，不等于归档或删除。

参考源码（相对于 Multica 仓库根目录）：

- `packages/views/issues/actions/issue-actions-menu-items.tsx`
- `packages/views/modals/delete-issue-confirm.tsx`
- `packages/views/issues/components/batch-action-toolbar.tsx`
- `server/internal/handler/issue.go` 的 `DeleteIssue()`
- `server/internal/service/task.go` 的 `CancelTasksForIssue()`
- `server/internal/daemon/daemon.go` 的取消检测与执行结束确认
- `server/internal/integrations/channel/engine/router.go`
- `server/pkg/db/queries/channel.sql` 的消息去重

### 已确认的前端方案

1. 详情右上角增加 `⋯` 按钮，菜单底部提供红色“删除任务…”及垃圾桶图标。
2. 看板卡片悬停或获得焦点时显示 `⋯`；点击不进入详情，复用删除菜单。右键菜单提供同样操作。
3. 第一阶段仅实现单项删除，批量选择与删除后续评估。
4. 菜单、确认弹窗沿用 Mica 现有明暗主题与组件样式，布局、危险操作层级和操作路径参考 Multica。
5. 所有状态均允许进入删除流程，不要求用户先手动取消；存在活动执行时，弹窗补充说明关联 Agent 执行将被取消。

确认弹窗文案：

```text
删除任务？

「修复订单导出异常」

将永久删除该任务及其讨论、执行记录和结果。
此操作不可撤销。
代码分支和 Worktree 将保留。

                         [取消] [删除]
```

有活动执行时补充：“关联的 Agent 执行将被取消。”

操作反馈：

- 提交后显示“删除中…”，禁用重复提交及关闭弹窗。
- 只有实际删除完成才提示“已删除任务”。详情页返回此前看板并保留搜索、滚动位置；直接链接进入时返回默认看板。
- 卡片入口删除成功后留在看板，移除卡片并更新统计。
- 删除接口失败时保留弹窗，显示错误并恢复按钮，允许重试或关闭；第一版不增加“删除未完成”事项状态，也不因此锁定事项。
- 接口成功表示平台记录已删除，不表示关联执行器已经退出；执行器随后异步感知取消或任务不存在并停止。

### 已确认的后端行为（2026-10-04 简化）

- 第一版参考 Multica：先将关联活动任务标记取消，随后直接删除事项及历史，不等待执行器真正停止。
- 新增 `DELETE /api/issues/{issue_id}`，成功删除平台记录后返回 `204`；事项不存在返回 `404`，数据删除失败返回错误，由弹窗提供普通重试。
- 取消属于删除前的尽力处理，不引入执行停止等待、停止确认超时或持久化恢复流程。取消处理的异常需记录，不以等待执行退出作为删除前提。
- 第一版不新增“删除中 / 删除未完成”的持久化状态、删除意图或后台重启恢复机制。“删除中…”仅是前端当前请求的加载状态。
- 在事务中永久删除 Issue、Comment、Task、TaskEvent、IssueOrigin 和关联投递记录，并处理 `external_event.issue_id` 等外键引用；清理失败应回滚数据删除。之前已完成的任务取消不随删除失败自动恢复。
- Daemon 感知任务取消或不存在后异步中断；任务/事项消失属于预期场景，应安全结束执行与释放资源。晚到事件、完成/失败回写、Squad 回调不得恢复事项、重建历史或派发新任务。
- 第一期只考虑 Web，不新增飞书消息删除标记，不设计旧消息回复或重复渠道事件的删除交互；现有渠道表仅做删除必需的引用及关联数据清理。
- 不自动删除分支、Worktree、CLI Session 文件或外部远程业务产物。

### 后端实现范围

1. `mica/api/app.py`、`mica/application/deletion.py`：单项删除接口、独立应用服务及普通成功/失败响应，不新增持久化删除状态输出。
2. `mica/application/services.py`、`mica/application/ports.py`：取消关联活动任务、事务删除的应用层编排及仓库接口；处理事项已不存在时的晚到回写。
3. `mica/infrastructure/database/repositories.py` 及内存实现：取消标记、事务删除、删除与入队/领取并发时的锁定或存在性校验。没有新增删除状态字段的要求；是否需要 migration 取决于外键与清理 SQL 的实现。
4. `mica/infrastructure/database/channel_repository.py`、`mica/application/channels.py` 及内存实现：现有外键与关联 Outbox 清理，避免重新生成已删除事项的通知，不新增渠道删除交互。
5. `mica/runtime/daemon.py`：任务或事项不存在时安全中断，处理异常、资源释放及晚到回调；不增加删除恢复调度。
6. `mica/application/squads.py`：事项删除后终态回调不再唤醒 Leader、派发任务或更新事项。
7. Provider：沿用现有取消能力，仅在任务消失的中断路径需要时调整；第一版不统一停止确认协议、不等待 A2A 远程终态。
8. `mica/bootstrap.py`：按需接入删除编排，不引入后台删除恢复服务。

上述第一版替代此前“先确认停止再删除、失败保留删除未完成状态”的方案。删除成功后执行可能继续短暂运行，与 Multica 的异步停止语义一致。

### 验收条件

- 所有状态的事项可从三个约定入口删除，确认弹窗及反馈符合上述设计。
- 删除请求先尝试取消关联活动任务，再删除平台记录；不等待执行器停止。
- 删除成功后移除卡片并更新计数与导航；普通失败在弹窗显示原因，支持重试和关闭，不产生“删除未完成”状态。
- 任务或事项已不存在时，Daemon 和 Squad 回调安全结束，不复活事项、补发任务或重建历史。
- 关联数据删除具有事务性，Web 创建事项的 `external_event` 引用不阻塞删除；重复点击不产生重复副作用。
- 明暗主题、键盘操作、窄屏布局及删除后的搜索/滚动位置符合预期。
- 分支、Worktree、CLI Session 文件和远程业务产物不被隐式删除。
- 本期不增加归档、回收站、批量删除、持久化删除状态、后台删除恢复或渠道消息删除交互。

### 实现链接

- [删除应用服务](../mica/application/deletion.py)
- [Web 菜单](../mica/web/lib/issue-actions.js)及[确认弹窗](../mica/web/lib/forms.js)
- [后端删除测试](../tests/test_issue_deletion.py)及[前端提交状态测试](../tests/web_issue_delete.test.mjs)

### 实现验证

- 新增删除 API、历史清理、事务失败回滚、运行中删除不等待、晚到输出/评论、Squad 回调及代码产物保留测试。
- 临时独立 PostgreSQL 集成测试验证外键、Outbox 清理及事务回滚，不接触现有项目数据库。
- 前端测试覆盖提交期间禁止关闭与重复提交、失败后重试，以及成功后的刷新错误不误报删除失败。
- 浏览器核对详情菜单、卡片菜单、右键菜单和共享确认弹窗；390px 窄屏弹窗不超出屏幕。
- 第一版仍采用异步停止语义：删除成功表示平台记录已删除，执行器可能随后才退出。

### 后续调整记录

- 2026-10-06（`c69422f`）：交互打磨，偏离原"已确认的前端方案"第 2 条——看板卡片不再渲染 `⋯` 按钮，右键菜单与键盘 ContextMenu/Shift+F10 直接锚定卡片本身；确认弹窗改为无 `×` 关闭按钮的变体，背景点击不再关闭、焦点圈定在弹窗内、`alertdialog` 语义、提交中 `aria-busy`。弹窗文案精简为"将永久删除该任务及其讨论和执行记录。此操作不可撤销。"（后端行为不变：分支与 Worktree 仍保留）。前端测试同步更新，仍为 15 项通过。

---

## KI-018：API 与 Daemon 并发启动探测在 runtime_capability 唯一键上互撞退出

- **状态**：已完成（2026-10-06）
- **优先级**：P1（两个进程同时启动必现其一崩溃退出，正常启动顺序依赖运气）
- **发现日期**：2026-10-06（服务重启时实际触发，非测试发现）
- **涉及模块**：`RuntimeDiscoveryService`、`PostgresRuntimeCapabilityRepository`、启动引导

### 修复前行为与证据

`mica.main`（`bootstrap.py`）与 `mica.daemon_main` 启动时都会执行 `runtime_discovery.probe()`，写入路径是 `replace_for_runtime()` 的"事务内 DELETE + 逐行 INSERT"。两个进程并发执行时，后插入的一方在 `(runtime_id, provider)` 主键上阻塞等待，对方提交后抛出：

```text
psycopg.errors.UniqueViolation: duplicate key value violates
unique constraint "runtime_capability_pkey"
DETAIL: Key (runtime_id, provider)=(local, claude) already exists.
```

进程随即退出。2026-10-06 同时启动 API 与 Daemon 时实测复现；错开启动顺序则不会触发。

### 修复方式

INSERT 改为 `ON CONFLICT (runtime_id, provider) DO UPDATE`（`mica/infrastructure/database/repositories.py`）：并发探测中落后的一方把冲突行更新为自己的探测结果，而不是报错退出。DELETE 保留，用于清理不再被探测到的 Provider 行。两个进程在同一台机器探测结果一致，并发收敛后的最终状态等价。

### 完成记录

- 2026-10-06：修复提交 `65e1f36`。
- 测试：`tests/test_runtime_discovery.py::test_concurrent_probe_replace_converges_without_unique_violation` 用两个真实 PostgreSQL 连接复现"一个事务保持 DELETE+INSERT 未提交，另一线程并发执行 replace"的交错。双向验证：旧代码下该测试抛出与线上一致的 UniqueViolation，修复后通过。
- 全量测试 294 项通过（含 `MICA_TEST_DATABASE_URL` 指向的临时库的 PostgreSQL 集成测试）。
- 内存仓储无此约束，不受影响；该问题原先未收录进本文档，本次直接以已完成条目补录。

---

## KI-008：飞书出站消息不落库、不进话题，回复 bot 受理/完成消息会误开新 Issue

- **状态**：已完成（2026-10-09；方案一 + `thread_id` 落库，方案二留作可选加固，见待决策项）
- **优先级**：P1（用户高频动作触发静默错误：补充要求丢失且凭空多出无上下文的新 Issue）
- **涉及模块**：飞书接入、Outbox 投递、IssueOrigin、评论路由；与 KI-001（问题二，Chat/Issue 模型）联动但可独立修复

### 修复前行为

- `issue_origin` 只记录**用户首条消息**的 `message_id`（`mica/application/channels.py` `add_origin`）；飞书回复通过 `parent_id/root_id` 命中该记录才会接回原 Issue（`mica/integrations/feishu.py` `_on_message`）。
- bot 的受理消息（`issue.accepted`/`comment.accepted`）与终态通知（TaskNotificationProjector）经 `FeishuMessageSender.send()` 以 `receive_id=chat_id` 发送群层面新消息，**响应中的 `message_id` 被直接丢弃**，且消息不在用户原消息的话题内。
- 用户回复 bot 的受理/完成消息时，`parent_id = root_id = bot 消息 id`，`find_origin` 查不到 → `related_issue_id=None` → **创建新 Issue**。

### 消息链

```text
用户发 M1 → 建 Issue I，origin 记 M1 ✅
bot 发受理 B1（群层面新消息，id 不落库，不进 M1 的话题）✗
用户回复 B1「另外只看昨天的数据」→ root_id=B1，查不到 → 新建 Issue J ✗
```

### 影响

- 补充内容（通常是"对，另外只看昨天的"这类残句）被当成新任务的完整输入，派给默认 Agent 执行。
- 真正在跑的原 Issue 永远收不到这条补充：无评论合并、无 successor task，全程静默。
- 用户视角"我补充了要求"，系统视角"来了个没头没尾的新任务"，双向错误。

### Multica 参照（本机源码，HEAD 2ea01ae）

飞书事件的 message 对象带三个回指字段：`parent_id`（消息级，直接回复的那条）、`root_id`（消息级，回复链根消息）、`thread_id`（容器级，话题/topic id，形如 `omt_xxx`，仅话题内消息携带）。Multica 对 `thread_id` 有三层用法：

1. **会话隔离键**：群内话题消息按 `chat_id:thread_id` 键控出独立 chat_session（`larkSessionRouting`），同一群里两个 @bot 话题不合并成一份对话记录；私聊忽略 thread_id，普通群仍按 chat_id。与 Slack 的 `channel:threadRoot` 同一模型。
2. **话题级上下文窗口（fail-closed）**：@bot 触发消息在话题内时，最近上下文拉取用 `container_id_type=thread` 限定在该话题；返回项缺 `thread_id` 或不匹配直接丢弃，失败绝不回退到全群拉取（#5835，话题间内容互不可见）。
3. **出站回复进原话题**：migration 122 给 `lark_chat_session_binding` 增加 `last_lark_message_id` / `last_lark_thread_id`，出站走 `/im/v1/messages/{id}/reply` + `reply_in_thread=true` 回到原话题，而非群层面发新消息。

### 修复方式（2026-10-09，按方案一实施）

- **受理 Outbox 带 `reply_to`**：`IngressService.submit()` 两处 `add_outbox`（`issue.accepted` / `comment.accepted`）的 payload 增加 `reply_to=command.external_message_id` 与 `reply_in_thread=bool(command.thread_id)`（`mica/application/channels.py`）。
- **终态通知带 `reply_to`**：`TaskNotificationProjector._project()` 的 payload 增加 `reply_to=origin.external_message_id` 与 `reply_in_thread=bool(origin.thread_id)`。
- **发送端支持 reply**：`FeishuMessageSender.send()` 在 payload 含 `reply_to` 时走 `/im/v1/messages/{id}/reply`（话题场景 `reply_in_thread=true`），否则退回 chat 级 `create`；`uuid` 幂等参数两个接口一致。reply 失败仅在确定为"目标不可回复且未发送"的业务码（230011/230019/230050/230071/230072/230111，对齐 Multica `threadReplyUnsupportedCodes`）时回退 chat 级 `create`；限流、5xx、超时等模糊错误依旧抛错走 Outbox 退避重试，避免重复投递或把话题回复漏进主群。
- **`thread_id` 落库**：migration 011 给 `issue_origin` 增加 `thread_id TEXT`；`_on_message` 透传 `message.thread_id` 到 `IngressCommand` 并写入 origin，为 KI-001 的话题级 ChatSession 预留数据结构。
- 效果：bot 消息回复进用户首条消息的话题/引用链后，话题内所有消息的 `root_id` 均为用户首条消息，现有 `find_origin` 不改路由逻辑即可命中。

### 待决策项

1. ~~是否一期只做方案一，方案二作为后续加固。~~（已定：一期只做方案一。方案二"出站 message_id 落库作为路由锚点"留作可选加固，覆盖必须发群层面通知或历史数据的场景。）
2. 修复后受理文案是否提示"在本话题中回复可继续补充"。（未做，文案现状不变。）
3. 群层面通知（非任务回传）是否也需要锚点落库。（未做，随方案二一并评估。）
4. ~~`thread_id` 的持久化位置。~~（已定：`issue_origin` 扩展列，migration 011。）

### 验收条件

- 模拟"回复受理消息"：同一 Issue 新增评论并按既有规则合并/排队，不创建新 Issue。（机制覆盖：受理消息回复进话题后 `root_id` 链命中 origin；路由行为由既有 `test_channel_reply_becomes_a_comment_instead_of_a_second_issue` 保持不回归。）
- 模拟"回复完成消息"：同样接回原 Issue。（终态通知 payload 的 `reply_to` 由 `test_terminal_task_event_is_projected_to_feishu_outbox` 覆盖。）
- 话题内回复用户自己、回复 bot、回复话题中任意消息，路由结果一致。（依赖飞书话题内 `root_id` 一致的机制，未做真实环境联调。）
- 话题内消息（带 `thread_id`）与非话题消息的路由行为一致，且 `thread_id` 被持久化。（内存与 PostgreSQL 双仓储覆盖：`test_issue_acceptance_outbox_carries_reply_target_and_thread`、`test_feishu_webhook_persists_topic_thread_id_on_origin`、`test_postgres_origin_persists_thread_id_and_outbox_reply_target`。）
- 伪 sender 返回固定 message_id 即可覆盖，无需真实飞书环境。（`test_sender_*` 四项：reply 路径、不可回复码回退、模糊错误不回退、无 reply_to 时 chat 级发送。）
- ~~面试手册 07 篇边界陈述同步更新。~~（已更新；问题三、四的边界陈述保留。）

### 完成记录

- 2026-10-09：实施完成，待提交。改动文件：`mica/application/channels.py`、`mica/integrations/feishu.py`、`mica/domain/models.py`、`mica/infrastructure/database/channel_repository.py`、`mica/infrastructure/database/sql/011_origin_thread_id.{up,down}.sql`；测试见 `tests/test_channel_ingress.py`（新增 6 项）与 `tests/test_postgres_channels_integration.py`（新增 1 项）。
- 验证：全量 Python 测试 316 项通过（含一次性 PostgreSQL 库的集成测试）；`ruff check mica` 通过；本次改动文件 `ruff format --check` 通过。
- 残留边界：方案二（出站 message_id 落库回退查询）未实施；真实飞书话题联调未做。
