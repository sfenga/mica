# Squad 编排与飞书协作

## 1. 业务需求

复杂任务通常不是一个 Agent 能独立完成。例如空间布局任务可能需要：

- Consultation Agent 补充企业规范或知识库信息。
- Geometry Agent 校验房间尺寸和几何约束。
- Placement Agent 生成候选摆放方案。
- Coding Agent 修改展示或集成代码。

用户希望在 Web 中按角色组装这些 Agent，并把整个组合当成一个 Squad 指派。任务可以从 Web 创建，也可以从飞书群发起；用户关心的是整体结果，而不是被内部每个成员的完成消息刷屏。

系统需要回答：谁负责理解目标、谁决定调用哪些成员、成员失败后谁收口、什么时候算整体完成，以及结果如何可靠回到原会话。

### 与 Multica 的关系

两套实现都采用 Leader-first：Squad 只是成员与协作规则的配置，真正运行的仍是具体 Agent，Leader 负责选择成员并汇总结果。但 Mica 为了让第一版状态机更确定，采用了比 Multica 更窄的编排协议：

- Multica 把 Squad 操作协议和成员 roster 注入 Leader；Leader 通过平台工具、`@` 成员评论或创建子 Issue 完成真实委派，并记录 `action / no_action / failed` 类型的 Squad activity。
- Multica 的等待条件主要基于子 Issue 及其阶段，子 Issue 达到对应完成阶段后再触发父 Issue 的 Leader。
- Mica 当前要求 Leader 输出 `delegate / complete` JSON，由平台创建带 `delegated_from_task_id` 的成员 Task，并以同批 sibling Task 是否结束作为 barrier。

因此，后文的结构化 JSON、成员 Task 批次和 Outbox 是 Mica 当前实现，不应描述成 Multica 的原样机制；真正继承的是“Leader 先理解目标、按 roster 委派、成员结果回到 Leader 收口”这一业务思想。

## 2. 演进过程

### V0：任务广播给所有成员

合理的最初方案是 Squad 收到 Issue 后给每个成员创建 Task，全部完成后拼接输出。

问题包括：

- 简单任务也会调用全部 Agent，成本高。
- 平台只知道成员标签，不一定能理解自然语言目标该如何拆分。
- 成员结果可能冲突，没有明确角色做取舍。
- 直接拼接不是业务汇总，用户仍需自己判断。

### V1：让 Leader 在 Prompt 中自由决定

引入 Leader 后，可以把成员列表写入 Prompt，让它自己调用成员。但如果只依赖自然语言，Leader 可能输出“我准备让 Geometry 处理”却没有真正派发；也可能编造一个不存在的 Agent ID，或者一次重复指派同一成员。

### V2：Mica 的 Leader-first + 结构化决策

当前要求 Leader 每轮只返回一个 JSON 对象：

```json
{
  "decision": "delegate",
  "summary": "需要分别检查几何和摆放约束",
  "assignments": [
    {"agent_id": "geometry", "instruction": "校验尺寸和消防通道"},
    {"agent_id": "placement", "instruction": "基于有效区域生成候选方案"}
  ]
}
```

或者在整体结果可交给用户确认时返回：

```json
{"decision":"complete","summary":"约束通过，已生成最终方案","assignments":[]}
```

平台负责确定性校验和真正创建成员 Task。这样自然语言理解交给模型，成员范围、重复指派、空指令和状态推进交给代码。

### V3：Mica 的成员 Task barrier

如果每个成员完成后都创建一条 Leader Task，两个并行成员会让 Leader提前汇总两次，甚至在另一个成员仍运行时得出不完整结论。

当前以 `delegated_from_task_id` 标记同一批委派。任一成员进入终态时，平台检查所有 sibling；只有全部不再处于 queued/dispatched/running，且不存在待执行 Leader Task 时，才创建一条 Leader 汇总 Task。

### V4：内部事件直接全部推送飞书

把每个 worker 的完成、失败和 Leader 中间决策都发到群聊，会暴露内部编排细节并造成通知噪声。当前成员的完成、失败和取消事件只保留在 Web 时间线中；Squad 整体终态才回传飞书。成员等待输入是例外，它会通知用户补充信息，避免任务无声卡住。

## 3. Squad 配置模型

`Squad` 保存：

- `workspace_id`：所属工作空间。
- `leader_agent_id`：协调 Agent。
- `description`：业务场景。
- `instructions`：用户配置的协作规则。

`SquadMember` 保存：

- `agent_id`：可引用 CLI、ACP 或 A2A Agent。
- `role`：如 geometry reviewer、catalog consultant。
- `capabilities`：供 Leader 选择成员的能力标签。

Web 端提供 Squad 组装表单。修改 Leader 或移除成员前，`SquadService._ensure_idle()` 会检查是否仍有 queued、dispatched、running、waiting_input Task，避免执行过程中改变 roster，使历史决策失去解释依据。

## 4. Mica 当前的 Leader-first 流程

```text
Issue assignee = Squad
          │
          ▼
Leader Task
  注入 roster、角色、能力、协作规则
          │
          ▼
delegate JSON ──平台校验──► Worker Task A / Worker Task B
                                  │
                                  ▼
                         全部成员进入终态
                                  │
                                  ▼
                         单个 Leader 汇总 Task
                                  │
                     ┌────────────┴────────────┐
                     ▼                         ▼
                再次 delegate              complete
                     │                         │
                     └───────────────► Issue in_review
```

Issue 始终保持 `assignee_type=squad`，但具体 Task 指向真实 Agent，因此同一个 Squad 可以同时包含本地 Claude、ACP Coding Agent 和远程 A2A Geometry Agent，无需设计“Squad Provider”。

Multica 中“具体工作仍交给成员 Agent”这一点相同，但委派载体不只是一组 sibling Task：Leader 可以通过评论提及成员，或创建有层级和阶段语义的子 Issue。Mica 当前舍弃了这层通用 Issue DAG，换取更简单、可直接用数据库字段校验的单层批次循环。

## 5. Mica 的确定性校验

`SquadService._parse_and_validate_leader_decision()` 检查：

- 输出必须是一个 JSON object。
- decision 只能是 `delegate` 或 `complete`。
- delegate 至少包含一项 assignment。
- assignment 的 Agent 必须属于 Squad 且不能是 Leader 自己。
- 同一批不能重复指派相同 Agent。
- 每项 instruction 不能为空。
- complete 不能携带 assignments。

任何校验失败都会使 Leader Task 失败并把 Issue 置为 blocked，而不是猜测 Leader 的真实意图。这体现了 Agent 编排中的一个重要原则：模型负责提议，代码负责约束和执行。

这里的 JSON 校验是 Mica 为降低演示和调试复杂度加入的控制层。Multica 不要求 Leader 只返回这两个 JSON 分支，而是通过真实的平台工具调用判断是否发生了委派；所以“Leader 必须输出 JSON”是 Mica 的设计取舍，不是 Leader-first 的通用前提。

## 6. Mica 中成员结果如何回到 Leader

成员 Task 保存：

- `squad_id`：属于哪个 Squad。
- `delegated_from_task_id`：由哪一轮 Leader 决策产生。
- `handoff_note`：Leader 给该成员的具体指令。

成员完成、失败、取消或等待输入后，最近结果会被追加进下一次 Leader briefing：

```text
## Recent delegated results
- geometry: completed: ...
- placement: failed: ...
```

Leader 根据结果决定再次委派、调整策略或 complete。成员失败并不直接把整个 Issue 判失败，因为降级和重新分配应该由 Leader 决定；Leader 本身失败才会阻塞整体 Issue。

对于 `waiting_input`，成员已经释放 Runtime Slot。如果仍把它视为活跃 sibling，Leader 永远无法被唤醒来询问用户或调整委派，形成死锁。因此 barrier 检查只把 queued、dispatched、running 视为活跃，并允许 Leader 看到 waiting_input 后接管决策。

Multica 的对应 barrier 更偏业务层：Leader 委派出的子 Issue 可以处于不同 stage，平台在子 Issue 完成并达到父流程要求的阶段后，再触发父 Issue 的后续处理。Mica 当前没有复制这套 child-Issue/stage 模型，只对一次 Leader Task 创建的成员 Task 做批次等待。

## 7. 用户补充要求与 Squad 顺序

Issue 指派给 Squad 时，普通评论默认路由给 Leader。若首个 Leader Task 还在 queued，新评论会合并到该 Task；若成员已经在执行，后续 Leader Task 需要等待本批成员终态后才能被领取。

数据库 `claim_next()` 对 Leader Task 增加条件：同一 Issue/Squad 只要还有非 Leader worker 处于 queued、dispatched、running，就不领取 Leader。这样用户补充不会让 Leader 绕过正在执行的成员批次提前汇总。

## 8. 飞书入口

飞书集成由官方 SDK 处理回调 token、签名和加密事件。Mica 当前采用 HTTP Webhook，只接受文本消息，并执行：

1. 校验事件时间戳，拒绝超出允许时钟偏差的请求。
2. 清理机器人 @ 标记和 `/mica` 前缀。
3. 解析 `agent=<id>` 或 `squad=<id>`；没有显式目标时使用默认 Agent。
4. 使用 tenant key 隔离 Workspace 和用户身份映射。
5. 根据 `parent_id/root_id` 查找 `IssueOrigin`；命中则追加到原 Issue，否则创建新 Issue。
6. 记录原 chat/message，使后续结果回到相同会话。

示例：

```text
/mica squad=layout-squad 为 20 人团队设计会议室布局
```

在线程中回复“消防通道至少 1.2 米”时，不会再创建第二个 Issue，而是进入原 Issue 的评论合并和 Leader 路由流程。

Multica 的飞书 Adapter 使用 WebSocket 长连接接收事件，并在统一 Channel 抽象下支持线程、卡片、附件、输入状态和消息编辑等更完整能力。Mica 的 Webhook + 文本入口是较小的可运行子集；二者共享“外部会话映射到内部上下文”的目标，但接入形态和功能范围不同。

## 9. 为什么使用 Outbox 回传飞书

如果 Task 完成事务里直接调用飞书 API，可能出现两种不一致：

- 飞书发送成功，但数据库事务回滚，用户看到不存在的结果。
- Task 已提交，但发送超时，结果永久丢失。

当前先把终态 `TaskEvent` 投影成持久化 `outbox_message`，再由独立 Worker发送：

```text
TaskEvent → TaskNotificationProjector → Outbox → FeishuMessageSender
```

Outbox 使用 `(channel, source_event_id, message_type)` 唯一约束去重；投递 Worker通过 `FOR UPDATE SKIP LOCKED` 与租约认领消息；失败后指数退避，超过最大次数进入 failed。飞书请求携带 Outbox message ID 作为 UUID，进一步利用飞书侧幂等。

这是 Mica 的可靠投递设计。Multica 目前通过内部事件总线、Channel delivery 状态和 outbound ledger 组织回传，并不是同一个 `TaskEvent → Projector → Outbox Worker` 链路。面试中可以把两者作为同一可靠性问题的不同实现进行比较，不应把 Mica 的 Outbox 说成从 Multica 直接迁移而来。

## 10. Web 与飞书展示边界

- Web：通过 PostgreSQL `TaskEvent` 和事件 Relay 展示执行时间线，包含文本、工具调用和状态。
- 飞书：当前发送 `issue.accepted`、`comment.accepted`、普通 Task 终态或 `squad.completed`。
- Squad 内部 worker 的成功、失败、取消不会单独发飞书；成员的 `task.input_required` 会提示用户补充，正常流程由 Leader 汇总后发送整体结果。

因此当前不能说“所有流式进度实时同步到飞书”。更准确的表述是“Web 展示执行进度，受理状态和最终结果回传原飞书会话”。

## 11. 当前边界

- Leader 决策是单层批次，不支持显式 DAG、依赖边和任务优先级。
- 一个 Leader 每批对同一成员只能指派一次，不支持同成员并行执行多个子任务。
- Squad roster 没有配置版本快照；通过禁止活跃时修改来维持一致性。
- 飞书目前只处理文本，不支持图片、文件和交互卡片。
- 飞书只支持简单 `agent=`/`squad=` 语法，没有可视化选择器。
- Projector 游标保存在进程内；重启后会重新扫描历史事件，但依赖 Outbox 唯一约束避免重复消息。大规模场景应持久化游标。
- Web 的实时总线是进程内 EventBus + 数据库轮询 Relay，不是 Kafka 等跨节点消息总线。

## 12. 面试时的 90 秒讲法

“Squad 场景里我没有采用收到任务就广播所有成员，因为简单任务会造成无效调用，而且没人负责处理冲突结果。整体参考 Multica 的 Leader-first：Web 配置 Leader、成员角色和能力，Leader 根据 roster 选择成员，成员结果再回到 Leader 收口。Mica 为了先把编排状态做确定，要求 Leader 输出 delegate 或 complete JSON，由平台校验 roster、重复成员和空指令，再创建带批次来源的成员 Task；同批成员结束后只唤醒一次 Leader 汇总。这一点比 Multica 通过平台工具、成员评论或子 Issue 委派更窄，但更容易保证状态一致。飞书和 Web 共用 Mica 的 IngressService，线程回复回到原 Issue，最终结果通过事务 Outbox 投递回原会话。”
