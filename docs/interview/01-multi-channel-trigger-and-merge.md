# 多渠道任务触发与合并

## 1. 业务需求

用户可能在 Web 创建一个“排查订单同步失败”的 Issue，随后连续补充日志、时间范围和期望输出；也可能在飞书群里发送任务，再在线程中追加要求。一个 Issue 还可能有默认负责人、被评论显式 @ 的 Agent，以及用户正在回复的某次 Agent 执行。

系统需要同时满足：

- Web 和飞书进入同一套业务流程，不能各自维护一套任务状态。
- 飞书重试回调或 Web 重复提交不能创建两份 Issue。
- 评论要找到正确的 Agent，而不是一律发给 Issue 默认负责人。
- Agent 尚未启动时，连续补充应合并，避免每条消息启动一次模型。
- Agent 已经启动后，不能篡改本次输入；新增要求应留给下一次执行。
- 多个 Daemon 或并发请求下仍要保证上述规则成立。

这里的“合并”不是把多条评论覆盖成一段字符串，而是把评论 ID 聚合到一个待执行 Task，并保留原始评论及顺序。

### 与 Multica 的关系

Mica 借鉴的是 Multica 中“渠道消息进入持久化会话、评论按执行状态合并”的思路，但没有照搬它的渠道入口模型：

- Multica 把飞书等渠道里的普通消息放入 `ChatSession`；用户显式使用 `/issue` 时才创建 Issue。
- Mica 当前把 `/mica` 消息视为任务入口，直接创建 `Issue + Task`；线程回复再成为该 Issue 的 Comment。
- 因此，本文描述的是 Mica 的 **Issue 型任务入口**，不能表述为 Multica 的普通聊天流程。若后续需要“先聊天、再转任务”，应单独引入 ChatSession，而不是让 Issue 同时承担临时问答会话。

## 2. 演进过程

以下早期版本属于合理演进假设，用来解释当前设计为什么存在。

### V0：渠道收到消息后直接调用 Agent

最直接的做法是在 Web Controller 或飞书回调里调用 `provider.run()`。

优点是开发快，但问题明显：HTTP 请求生命周期和 Agent 执行绑定；飞书超时重试会重复启动 Agent；服务重启后没有可靠状态；Web 和飞书会逐渐产生两套不同逻辑。

因此第一次拆分是把“接收用户意图”和“执行 Agent”分开：渠道只产生持久化任务，由 Daemon 异步执行。

### V1：每次用户动作创建一个 Task

引入 Issue 和 Task 后，Issue 表示用户持续关注的事项，Task 表示一次实际执行。创建 Issue、评论和重试都可以生成 Task，执行历史也能被保留。

新问题是：用户连续发送三条补充信息，就会得到三个 Task。如果 Daemon 还没来得及领取，第一个 Task 仍会在不知道后两条要求的情况下启动，另外两个 Task 又各自调用一次 Agent。

### V2：应用层先查再合并

可以先查询是否存在 queued Task，有就更新，没有就插入。但两个 Web 请求并发时都可能先查到“不存在”，然后各自插入，仍然生成重复 Task。单机锁只能保护单进程，挡不住多个 API 实例。

因此唯一性必须下沉到 PostgreSQL，而不是只依赖 Python 判断。

### V3：部分唯一索引与原子 UPSERT

当前数据库建立了条件唯一索引：同一 `(issue_id, agent_id)` 在 `status='queued'` 时最多只能有一条 Task。`enqueue_or_merge()` 使用带条件的 `ON CONFLICT ... DO UPDATE`，并把旧的 `trigger_comment_id` 追加到 `merged_comment_ids`，新评论成为最新的 `trigger_comment_id`。

这解决了并发插入，但还有一个时间边界：Daemon 领取 Task 的同时可能又到达一条评论。如果生成 Prompt 时直接读取 Issue 当前的全部评论，新评论可能在执行开始后悄悄混入本轮，导致重试时无法解释“本轮到底消费了哪些输入”。

### V4：领取时固化输入快照

当前 `claim_next()` 在同一个 SQL 语句里完成三件事：

1. 通过 `FOR UPDATE SKIP LOCKED` 选中一条 queued Task。
2. 将 Task 更新为 `dispatched`，并记录本次执行的所有权：
   - `claimed_by` 保存领取任务的 Daemon Worker ID，例如 `daemon-local-1`。这里的 Worker 是运行任务的 Daemon 实例，不是 Issue 指定的业务 Agent；后续完成、失败和取消操作必须由当前 Worker 写回。
   - `lease_expires_at` 保存所有权的过期时间。Daemon 执行期间通过心跳不断续租；如果 Daemon 崩溃导致心跳停止，租约过期后 Task 可以恢复为 queued 并被重新领取，避免永久停留在 dispatched 或 running。
   - `attempt` 表示任务的执行代次，每次重新领取都会递增。状态写回同时校验 `claimed_by + attempt`，即使旧进程稍后恢复，也不能用上一代执行结果覆盖新一轮任务。
3. 将 `merged_comment_ids + trigger_comment_id` 去重并按原顺序写入 `delivered_comment_ids`。

Prompt 只读取 `delivered_comment_ids` 对应的评论。因此，领取之前到达的评论属于本轮；领取之后到达的评论无法再命中 queued 唯一索引对应的旧 Task，而会创建或合并进一条后续 Task。

这里是 Mica 为了让输入边界与执行所有权一次提交而采用的实现。Multica 的对应逻辑更细：领取 Task 与写入本轮已投递评论是两次带状态校验的数据库操作；领取后到达的评论会先登记为运行中 Task 的“计划但尚未投递输入”，在当前执行结束时统一对账，再决定是否创建后续 Task。两者解决的是同一个竞态，但不能把 Mica 的“一条 SQL 固化快照”说成 Multica 的原始实现。

## 3. 当前实现

### 3.1 Web 与飞书统一入口

Web 的 `POST /api/issues` 和飞书 `FeishuWebhook` 不直接创建数据库任务，而是先把各自的请求转换成统一的 `IngressCommand`，再交给 `IngressService.submit()`。这样幂等、用户映射、Issue 创建和首个 Task 入队只实现一次，不会随着入口增加而形成多套业务逻辑。

可以用一条飞书消息理解整个过程。用户在飞书群 `oc_group_01` 中发送：

```text
/mica agent=general_cc 排查订单同步失败
```

飞书回调中包含以下外部信息：

```text
tenant_key = tenant_A
event_id = event_1001
sender_id = ou_123
chat_id = oc_group_01
message_id = om_456
```

`FeishuWebhook` 完成验签和文本解析后，将这些渠道字段转换成 `IngressCommand`。Web 请求也会构造相同对象，只是 `channel=web`，并将 `Idempotency-Key`、当前用户和 Workspace 转换成对应字段。

```text
Web / 飞书
    │
    ▼
IngressCommand
    │
    ├─ 预占 external_event，完成幂等判断
    ├─ 解析 external_identity → platform_user
    ├─ 新消息：创建 Issue + 首个 Task + IssueOrigin
    └─ 线程回复：追加 Comment + 路由/合并后续 Task
```

#### 第一步：预占 `external_event`，阻止重复请求

飞书 Webhook 和网络请求都可能被重试。如果回调超时后飞书再次发送 `event_1001`，直接创建 Issue 会导致同一条消息重复启动 Agent。

因此 `IngressService` 首先尝试写入：

```text
external_event
├─ channel = feishu
├─ tenant_key = tenant_A
├─ external_event_id = event_1001
├─ payload_hash = 当前业务内容的哈希
└─ issue_id = 暂时为空
```

数据库使用 `(channel, tenant_key, external_event_id)` 作为唯一标识：

- 第一次收到事件：插入成功，当前请求取得处理资格。
- 相同事件和相同 payload 再次到达：返回第一次创建的 Issue，不再创建 Task。
- 相同事件 ID 却携带不同 payload：返回幂等冲突，防止错误地复用第一次结果。

这里称为“预占”，是因为系统先登记“这条外部事件正在处理”，再创建具体 Issue。事件登记、Issue/Task 创建以及 `external_event.issue_id` 回填处于同一事务；如果中途失败，它们会一起回滚，不会留下“事件显示已处理，但实际没有任务”的记录。

Multica 的渠道幂等同样采用两阶段处理，但唯一键是 `(installation_id, message_id)`：先 claim 消息，成功后标记完成，失败则释放以便重试。Mica 额外保存 `payload_hash`，用于识别“同一幂等键却携带不同业务内容”的冲突；这是 Mica 的增强，不应归因于 Multica。

#### 第二步：将渠道身份映射为平台用户

飞书提供的是 `ou_123` 这样的外部用户 ID，Web 或未来其他渠道可能使用完全不同的账号标识。核心业务如果直接保存渠道 ID，就需要在权限、创建人和审计逻辑中不断判断渠道类型。

Mica 因此维护两层身份：

```text
external_identity                         platform_user
├─ channel = feishu                  ┌──► ├─ id = user_001
├─ tenant_key = tenant_A             │    ├─ workspace_id = feishu:tenant_A
├─ external_user_id = ou_123         │    └─ display_name = 张三
└─ user_id = user_001 ───────────────┘
```

`external_identity` 回答“这个渠道账号是谁”，`platform_user` 是 Mica 内部统一使用的用户。创建 Issue 时保存 `created_by_user_id=user_001`，而不是把 `ou_123` 传播到领域模型中。

当前实现保证同一渠道、同一租户、同一外部用户稳定映射到同一个平台用户。跨渠道账号合并、企业通讯录同步和 Workspace RBAC 属于后续能力。

#### 第三步：创建 `Issue + 首个 Task + IssueOrigin`

确认事件未重复并取得平台用户后，新消息会在同一事务中创建三个对象：

```text
Issue
├─ id = issue_001
├─ title/body = 排查订单同步失败
├─ assignee_id = general_cc
├─ created_by_user_id = user_001
└─ status = todo

Task
├─ issue_id = issue_001
├─ agent_id = general_cc
├─ trigger_type = issue.created
└─ status = queued

IssueOrigin
├─ issue_id = issue_001
├─ channel = feishu
├─ tenant_key = tenant_A
├─ conversation_id = oc_group_01
└─ external_message_id = om_456
```

三者职责不同：

- `Issue` 保存用户持续关注的业务事项和后续讨论。
- `Task` 表示第一次具体 Agent 执行，是 Daemon 真正领取的对象。
- `IssueOrigin` 保存事项来自哪个渠道、群聊和原始消息，用于线程关联与结果回传。

任务完成后，通知投影根据 `issue_id` 查找 `IssueOrigin`，把结果发送回 `oc_group_01`。如果用户回复原消息 `om_456`，飞书适配器也能通过 Origin 找到 `issue_001`，将新内容追加为 Comment，而不是再创建一个 Issue。

#### 第四步：线程回复进入原 Issue

例如用户在原飞书线程中补充：

```text
traceId 是 123，只检查华东租户
```

飞书消息中的 `parent_id` 或 `root_id` 会被用来查找 `IssueOrigin`。命中后，`IngressService` 设置 `related_issue_id=issue_001`，后续流程变成：

```text
追加 Comment
    ↓
根据 @提及、回复关系或默认负责人确定 Agent
    ↓
合并到该 Agent 的 queued Task，或创建一条 queued 后续 Task
```

因此四类数据分别解决四个问题：

| 数据 | 解决的问题 |
|---|---|
| `external_event` | 这条外部请求是否已经处理过 |
| `external_identity → platform_user` | 发起人在 Mica 内部是谁 |
| `Issue + Task` | 用户要处理什么，以及第一次具体执行是什么 |
| `IssueOrigin` | 后续回复属于哪个 Issue，结果应该发送到哪里 |

相关代码：

- `mica/api/app.py`：Web 请求转换为 `IngressCommand`。
- `mica/integrations/feishu.py`：飞书事件验签、文本解析和线程关联。
- `mica/application/channels.py`：统一入口、幂等和身份映射。
- `mica/infrastructure/database/sql/003_channels_and_outbox.up.sql`：外部事件、身份和来源会话表。

### 3.2 评论路由优先级

Mica 参考 Multica，把“继续对话”建模为**回复评论**，而不是让用户理解或填写 Task ID。Web 端的评论卡片提供“回复”入口，提交时只携带 `parent_comment_id`；Task 仍然是后端执行记录，只在执行日志中提供查看、取消等操作。

Agent 完成任务或等待用户补充信息时，平台会把其结果写成一条评论，并记录两组关系：

- `parent_comment_id`：这条结果在回答哪条用户评论。
- `source_task_id`：这条 Agent 评论由哪一次 Task 产生。

例如：

```text
用户评论 C1：@agent:codex 检查订单同步逻辑
    ↓ 触发
Task T1：Codex 执行检查
    ↓ 产出
Agent 评论 C2：发现同步游标更新时机不正确
    parent_comment_id = C1
    source_task_id = T1
    ↓ 用户点击“回复”
用户评论 C3：如果事务回滚，游标会不会提前提交？
    parent_comment_id = C2
    ↓ 路由回 C2 对应的 Codex，创建后续 Task
```

`TriggerService._comment_targets()`（`mica/application/services.py`）决定本条评论触发谁。先说明三个前提：

- **事务边界**：评论落库与目标任务创建在同一事务内。路由过程中抛错（例如 @ 了不存在的 Agent）会整体回滚，评论不会留下（见第 7 节与 known-issues KI-006）。
- **状态门禁**：Issue 处于 backlog / done / cancelled 时只保存评论，完全不进入路由；todo / in_progress / in_review / blocked 正常路由。
- **Agent 评论不参与路由**：Agent 的结果评论由 `_append_agent_comment()` 直接写入，不经过 `add_comment()`，因此 Agent 的发言永远不会再触发其他 Agent，结构上杜绝互相触发死循环；Squad 成员到 Leader 的回流由批次屏障负责，不走评论路由。

决策树按优先级分为四级，命中即停（第一级可产生多个目标）：

1. **显式提及**：识别 `mention://agent|squad/ID` 与 `@agent:ID` / `@squad:ID` 两种写法。`@agent` 以该 Agent 为目标；`@squad` 以该 Squad 的 Leader 为目标，并带 `squad_id`、`is_leader_task=True`。一条评论可提及多个对象，每个对象产生一个目标；回复中只要带提及，就以提及为准，不再追溯回复关系。
2. **回复追溯**（无提及且 `parent_comment_id` 非空），按精确度分三个子规则：
   - **2a. 父评论带 `source_task_id`**（它是某次 Task 的产出）：路由给执行该 Task 的 Agent，并继承其 `squad_id` / `is_leader_task`——回复成员的结果回到成员，回复 Leader 的汇总回到 Leader（以 Leader 身份）。
   - **2b. 父评论 `author_type='agent'` 但无任务关联**：按作者身份路由。当前写入路径不会产生这种评论（唯一写 Agent 评论的 `_append_agent_comment()` 必带 `source_task_id`）；它是为 `source_task_id` 外键的 `ON DELETE SET NULL` 预留的降级——任务行先于评论消失时（例如未来引入任务保留期清理），路由从“精确的任务角色”降级为“作者身份”（squad 角色随之丢失），而不是直接掉入最模糊的爬根。
   - **2c. 父评论是人写的**：沿 `parent_comment_id` 一路爬到线程根（visited 集合防环，中途不停），再在该 Issue 的全部 Task 中找根评论的匹配者（`trigger_comment_id == 根.id` 或根 id 属于 `merged_comment_ids`）。命中则路由给该 Task 的 Agent（继承 squad 上下文）；**未命中则不触发任何 Agent**——纯人工讨论线不产生任务。
3. **默认负责人兜底**：以上都未命中时，Issue 负责人是 Agent 则交给该 Agent；是 Squad 则交给其 Leader。没有提及的根评论必然落到这里。
4. **去重**：同一 Agent 被多次命中只保留一个目标；同一 Agent 同时以普通身份和 Leader 身份出现时，Leader 身份优先。

显式提及优先级最高，因此用户既可以回复原 Agent 延续上下文，也可以在回复中 `@` 另一个 Agent 做独立审核。多个提及会生成多个目标，同一 Agent 被重复提及时按 Agent ID 去重。

人与人在评论区的讨论是否触发 Agent，可以归纳为：

| 场景 | 路由结果 |
|---|---|
| 活跃 Issue 上新建根评论 | 触发默认负责人（第 3 级） |
| 回复人评论，该线根评论触发过 Task | 触发该 Task 的 Agent（2c 命中） |
| 回复人评论，根未触发过 Task（如 backlog 期的讨论） | 不触发任何 Agent（2c 落空） |
| 回复 Agent 的结果评论 | 触发产出它的 Agent（2a） |
| backlog / done / cancelled 上的任何评论 | 不触发（状态门禁，评论照存） |

这里需要区分三类 ID：

| 字段 | 含义 | 谁使用 |
|---|---|---|
| `parent_comment_id` | 当前评论回复哪条评论 | 用户界面和评论线程 |
| `source_task_id` | Agent 评论由哪次执行产生 | 路由、追踪和角色恢复 |
| `trigger_comment_id` | Task 是由哪条评论触发 | 调度和 Prompt 输入 |

### 3.3 queued Task 合并

核心表字段：

- `trigger_comment_id`：当前最新一条评论。
- `merged_comment_ids`：此前被合并的评论。
- `delivered_comment_ids`：领取瞬间固定的本轮输入。

数据库约束：

```sql
CREATE UNIQUE INDEX agent_task_one_queued_per_issue_agent_idx
ON agent_task(issue_id, agent_id)
WHERE status = 'queued';
```

这条约束的含义不是“一个 Issue 永远只有一个 Task”，而是“同一 Issue 与 Agent 同时最多有一个尚未领取的 Task”。正在运行的 Task 和一个 queued 后续 Task 可以共存。

### 3.4 为什么不把新消息并入 running Task

Agent 的 Prompt、会话状态和工具调用在启动后已经确定。修改 running Task 的输入有三个问题：

- 本地 CLI 未必支持运行中注入消息。
- 即使协议支持，也难以定义工具调用期间插入消息的顺序。
- 失败重放时无法还原当时真正被消费的输入。

因此 Mica 采用明确的批次边界：`queued → dispatched` 是输入封口点。运行中的新增消息进入唯一的 queued successor，等当前任务结束后顺序执行。

## 4. 具体业务示例

假设 10:00 用户创建 Issue，首个 Task 仍在 queued：

```text
10:00 创建 Issue：排查订单同步失败
10:01 评论 A：仅检查华东租户
10:02 评论 B：附上 traceId
10:03 Daemon 领取 Task
10:04 评论 C：输出修复建议
```

结果是：

- A、B 被合并进首个 queued Task。
- 10:03 领取时 `delivered_comment_ids=[A,B]`，Prompt 按 A、B 顺序构造。
- C 到达时首个 Task 已 dispatched，因此创建后续 queued Task。
- 当前 Agent 完成后，后续 Task 复用历史会话，基于 C 继续处理。

如果评论 C 显式 `@agent:reviewer`，它会进入 Reviewer 自己的 queued Task，不会和默认负责人的 Task 混合。

## 5. 为什么 Mica 的 Issue 评论合并没有采用固定时间窗口 debounce

固定等待 3～5 秒再聚合消息看似简单，但会增加每个任务的启动延迟，而且无法处理用户间隔更久的补充。Mica 使用 Task 状态作为自然窗口：只要任务还未领取就能合并，一旦执行资源已经承诺就关闭本轮输入。

这个边界比时间窗口更容易解释，也能由数据库事务严格保证。

这里也要区分 Multica 的两条路径：它的普通渠道 Chat 为了改善连续短消息体验，会先做约 3 秒的 debounce；进入 Issue 之后，评论合并同样主要依据 Task 状态，而不是仅依赖时间窗口。Mica 当前没有 ChatSession，所以只实现了后一种状态型合并。

## 6. 一致性与并发保证

- 创建 Issue 和首个 Task位于同一事务，不会出现只有 Issue 没有 Task。
- 外部事件预占、身份映射、Issue/Comment 创建和 Outbox 写入共享事务。
- 部分唯一索引负责最终并发约束，UPSERT 避免应用层先查再写竞态。
- `FOR UPDATE SKIP LOCKED` 让多个 Daemon 不会领取同一 Task。
- `delivered_comment_ids` 让执行输入可重放、可解释。
- `worker_id + attempt` 阻止租约过期后的旧 Worker 晚到覆盖新执行结果。

## 7. 当前边界

- 评论合并粒度是 `(Issue, Agent)`，没有按意图语义自动拆分消息。
- Web 评论接口本身没有独立幂等键；外部入口的新 Issue 和飞书事件具备幂等保护。
- 路由语法目前是显式 ID，不包含自然语言实体识别。
- 多个目标是分别创建 Task，不存在跨 Agent 的全局消息合并。
- `delivered_comment_ids` 固化输入，但暂未保存最终 Prompt 的完整哈希或版本。
- @mention 目标不存在时整条评论创建失败（路由与落库同事务），见 `docs/known-issues.md` KI-006。
- 回复追溯的线程归属只认根评论的触发记录；中途被 @ 拉进线程的 Agent 不接管整线，见 `docs/known-issues.md` KI-007。

## 8. 面试时的 90 秒讲法

“这个问题来自用户会在 Web 或飞书里连续补充要求。如果每条评论都直接启动 Agent，不但浪费调用，还可能让后来的要求和正在执行的上下文交叉。第一版是 Issue 下每个动作生成 Task，但连续评论仍会产生多条待执行任务；应用层先查后写又有并发竞态。后来我把约束下沉到 PostgreSQL，用 `(issue_id, agent_id) WHERE status='queued'` 的部分唯一索引配合 UPSERT，使同一 Agent 同时只有一个待执行批次。Daemon 领取时再用一条 SQL 把评论 ID 按顺序固化到 `delivered_comment_ids`，`queued → dispatched` 就成为输入封口点，领取后的新评论进入下一条 queued Task。渠道侧统一经过 IngressService，用外部事件 ID 和 payload hash 做幂等。这里参考了 Multica 的评论路由和状态型合并，但 Mica 当前把飞书任务直接建成 Issue；Multica 的普通渠道消息则先进入带短时间 debounce 的 ChatSession。”
