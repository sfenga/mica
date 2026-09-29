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

- **状态**：待设计
- **优先级**：P1
- **涉及模块**：Task Prompt、Provider Session、Squad、Worktree

### 当前行为

Mica 采用“平台拼接本轮 Prompt + 复用 Provider Session”的方式：

- 首次执行只传 Issue 标题和正文。
- 评论触发只增加 `delivered_comment_ids` 对应的本轮评论。
- Squad Worker 只得到标题、正文和 `handoff_note`。
- Squad Leader 额外得到成员清单，以及最近 20 条终态 Worker Task 的 `output/error` 摘要，每条最多 500 字符。
- Daemon 按 `(issue_id, agent_id)` 查找最近的 Session、分支和 Worktree 继续执行。

### 核心缺陷

1. **Worker 缺少共享上下文**：不会自动获得 Issue 历史评论、用户后续补充、其他 Worker 结果和 Artifact；Leader 如果没有在 `handoff_note` 中复述，Worker 就可能遗漏约束。
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

## KI-003：Task 准备阶段存在租约空窗，续租异常无法可靠中止执行

- **状态**：待设计
- **优先级**：P1
- **涉及模块**：Daemon、Task Lease、Worktree/Session 准备、PostgreSQL Task Repository、Provider 取消

### 当前行为

- Daemon 领取 Task 时写入 `claimed_by` 和 `lease_expires_at`，并递增 `attempt`。
- 领取成功后，Daemon 先恢复 Session、创建或复用 Worktree、准备工作目录并绑定执行上下文；Task 进入 `running` 后才启动心跳线程。
- 心跳按固定周期调用 `renew_lease()`。返回 `False` 时设置 `lease_lost`，Provider 通过取消检查停止执行。
- 轮询时会把租约已过期的 `dispatched/running` Task 恢复为 `queued`，供其他 Daemon 重新领取。
- Task 完成或失败写回时校验 `worker_id + attempt`，防止旧 Worker 覆盖新一轮结果。

### 核心缺陷

1. **准备阶段存在租约空窗**：初始租约从领取时开始计算，但心跳要等准备工作全部完成后才启动。如果 Worktree 或 Session 准备时间超过租约，其他 Daemon 可能把仍在准备的 Task 恢复并重新领取。
2. **续租异常不会可靠触发停止**：当前只处理 `renew_lease()` 返回 `False` 的情况。若 PostgreSQL 连接异常直接抛错，心跳线程会退出，但 `lease_lost` 不会被设置，Provider 仍可能继续运行。
3. **旧代次校验只能保护最终状态**：`worker_id + attempt` 可以拒绝旧 Worker 的完成写回，但无法撤销已经发生的 Git 操作、文件修改或外部 API 调用，因此仍可能产生重复副作用和状态抖动。

### 典型场景

Daemon A 领取 Task，得到 `attempt=1` 和 120 秒租约，随后开始创建较大的 Worktree。准备超过 120 秒且尚未启动心跳时，Daemon B 在轮询中将该 Task 恢复为 `queued`，再以 `attempt=2` 领取。此时 A、B 可能同时进行准备或执行；A 最终无法以旧 attempt 写回结果，但两边已经发生的文件系统或外部调用副作用不会自动消失。

另一种情况是 Task 已进入运行阶段，但心跳续租遇到数据库异常。心跳线程意外退出后，Provider 不知道租约已经无法确认，可能继续执行到完成；租约到期后，其他 Daemon 又可能重新领取该 Task。

### 期望方向

- 在 Task 成功领取后立即启动心跳，使 `dispatched` 准备阶段和 `running` 执行阶段都处于续租保护下；或显式区分 prepare lease 与 running lease。
- 心跳捕获数据库异常并进行有限重试，结合本地记录的租约截止时间判断所有权是否仍可信。
- 连续续租失败或接近租约截止时间时设置 `lease_lost`，触发 Provider 取消及子进程终止，避免失去所有权后继续产生副作用。
- 保留 `worker_id + attempt` 的条件写回，作为旧 Worker 晚到写回的最后一道保护。
- 明确 Worktree 准备被取消或失去租约后的清理策略，避免残留目录和分支。

### 待决策项

1. 准备阶段和运行阶段共用一个租约，还是分别设置 prepare lease 与 running lease。
2. 数据库短暂不可用时允许多少次失败、采用什么退避策略，以及何时判定租约丢失。
3. 无法确认租约时采用立即停止的 fail-closed 策略，还是允许在有限窗口内继续运行。
4. Worktree 创建过程中如何响应取消，以及失败后的目录、分支清理边界。
5. 多 Daemon 并发测试是否作为启用生产模式前的强制验收项。

### 验收条件

- 健康 Daemon 的准备时间超过初始租约时，Task 不会被其他 Daemon 重复领取。
- 短暂数据库异常在容忍范围内恢复后，Task 可以继续执行且不会重复领取。
- 续租长期失败时，Daemon 会在租约失效前后及时停止 Provider 和子进程。
- Task 被新 Daemon 以更高 attempt 领取后，旧 Worker 无法写回终态。
- 两个 Daemon 并发运行且人为延长准备阶段时，不会同时执行同一 Task。
- 心跳线程在成功、失败、取消和租约丢失路径中都能正常退出，不产生线程泄漏。

---

## 已完成

暂无。
