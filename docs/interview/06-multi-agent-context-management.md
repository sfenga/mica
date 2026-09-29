# 多 Agent 上下文管理

## 1. 业务需求

一个 Issue 可能不是由单个 Agent 一次完成。例如，用户要求为会议室生成布局方案：

- Consultation Agent 查询企业规范和产品目录。
- Geometry Agent 检查空间、通道和尺寸约束。
- Placement Agent 根据有效区域生成候选方案。
- Coding Agent 修改展示代码或集成接口。
- Squad Leader 决定调用顺序并汇总结果。

这些 Agent 可能运行在不同机器上，使用 CLI、ACP 或 A2A 等不同协议，也不会共享同一个模型会话。系统需要回答的不是简单的“Prompt 里放什么”，而是：

- 哪些上下文是整个 Issue 的公共事实，所有参与者都可以读取。
- 哪些上下文只属于某个 Agent，不能直接传给其他 Agent。
- Leader 如何把前一个 Agent 的结果可靠地交给下一个 Agent。
- 用户在执行期间补充要求时，哪些消息属于本轮，哪些留到下一轮。
- Session 丢失或 Agent 更换后，是否能够从持久化数据恢复任务背景。
- Issue 持续数天、积累大量评论和产物后，如何避免把全部历史反复塞进模型上下文。

核心原则是：**Issue 是可恢复的上下文事实源，Agent Session 只是某个 Agent 的私有连续记忆和性能优化。**

## 2. 不应采用的简单方案

### V0：把完整 Issue 历史拼进每个 Prompt

最直接的方案是把标题、正文、全部评论、所有 Agent 输出和附件文本拼成一个 Prompt，再发送给每个 Agent。

它在短 Issue 上能工作，但很快出现问题：

- 上下文随评论数量持续增长，每轮都重复支付相同 token。
- 大量无关讨论掩盖当前任务，模型不容易判断本轮重点。
- 附件和长 JSON 可能超出模型上下文窗口。
- 每个 Agent 都看到所有信息，无法按角色和权限控制可见范围。
- 很难证明某次执行实际消费了哪些评论和结果。

### V1：只依赖 Provider Session

第二种方案是首次发送完整背景，后续只恢复 Codex thread、Claude session、ACP session 或 A2A contextId。

Session 可以减少重复输入，但不能成为唯一事实源：

- Geometry 的 Session 无法直接交给 Placement。
- Provider 可能不支持恢复，或者 Session 因上下文溢出而失效。
- 模型历史中混合了推理、工具调用和旧结论，平台无法精确查询。
- 更换 Agent 或 Runtime 后，新执行者没有此前背景。
- 人类无法审查只存在于模型 Session 内的隐式信息。

### V2：Leader 在 handoff 中复述全部上下文

可以让 Leader 总结已有背景，再把一段长 `handoff_note` 发给 Worker。这样实现简单，但 Leader 可能漏掉约束、错误改写事实或截断结构化结果。相同内容还会在多个委派中重复复制。

因此，handoff 应表达“这个成员本轮负责什么”，不应代替可查询的共享上下文。

## 3. 合理的四层上下文模型

```text
Workspace / Project Context
    企业规则、项目说明、知识入口、可用工具和权限
                 │
                 ▼
Issue Shared Context
    标题、正文、验收标准、评论线程、附件、结构化产物
                 │
                 ▼
Task Input Snapshot
    本轮触发评论、委派指令、依赖结果引用、上下文版本
                 │
                 ▼
Agent Private Context
    Agent instructions、skills、Session、Worktree、私有执行历史
```

四层的生命周期和可见范围不同。

### 3.1 Workspace / Project Context

这一层描述相对稳定的组织和项目背景，例如：

- 企业术语、合规要求和默认业务规则。
- 项目说明、仓库位置、技术栈和运行方式。
- Agent 可以使用的内部知识库、MCP Server 和远程服务。
- 当前任务被授予的工具权限和数据范围。

它不应由用户在每个 Issue 中重复填写，而应在执行时根据 Workspace、Project、Agent 和 Task 权限组合。

### 3.2 Issue Shared Context

这一层是多个 Agent 与用户共同看到的业务事实：

- Issue 标题、正文、状态和验收标准。
- 用户评论及其线程关系。
- Agent 已发布的结论和等待补充信息。
- 附件、结构化 Artifact、子 Issue 和依赖结果。
- 与 Issue 关联的代码项目和业务对象。

共享不等于把全部内容主动注入每次 Prompt。系统可以先给出摘要和索引，Agent 再按评论线程、Artifact ID 或时间范围读取详细内容。

### 3.3 Task Input Snapshot

Task 表示一次具体执行，它需要一个不可变的本轮输入边界，例如：

- `delivered_comment_ids`：本轮必须处理的评论。
- `handoff_note`：Leader 为该成员定义的本轮职责。
- `context_inputs`：本轮上下文输入清单。每一项只记录对象类型和 ID，指向本轮需要读取的评论线程、上游 TaskResult 或 Artifact，不复制正文和结果内容。
- `context_revision`：领取任务时观察到的 Issue 上下文版本。

例如 Placement Agent 本轮需要读取用户对通道宽度的讨论、Geometry Agent 的结构化结果和第二版户型图：

```json
{
  "context_inputs": [
    {"type": "comment_thread", "id": "comment_01"},
    {"type": "task_result", "id": "result_geometry_01"},
    {"type": "artifact", "id": "floor_plan", "version": 2}
  ]
}
```

平台在执行前根据这些 ID 查询并校验数据，再将可读取的内容装配给 Agent。因此它表示“这次执行依赖什么”，不是另一份 Prompt，也不是 Provider Session。

这样才能回答：“这次 Agent 为什么得到这个结论，它当时看到了哪些输入？”

用户在 Task 领取后追加的评论不应悄悄进入本轮，而应进入后续 Task；如果 Provider 支持运行中消息，也应把注入动作记录为新的上下文事件，不能静默修改 Prompt。

### 3.4 Agent Private Context

这一层只属于特定 Agent：

- Agent instructions 和绑定 skills。
- Codex thread、Claude/ACP session、A2A contextId。
- 该 Agent 的 Worktree、分支和本地工作目录。
- 模型内部历史、工具调用过程和临时文件。

不同 Agent 不应直接共享 Provider Session。跨 Agent 传递的应该是已经持久化的业务结论、结构化结果和 Artifact，而不是另一个模型的完整对话历史。

## 4. Mica 当前实际实现

Mica 已经具备四层中的一部分，但还没有形成完整的公共上下文读取机制。

### 4.1 当前 Prompt 输入

`build_task_prompt()` 当前按 Task 类型构造输入：

- 普通首次执行：Issue 标题和正文。
- 评论触发：Issue 标题、正文和 `delivered_comment_ids` 对应的本轮评论。
- Squad 委派：Issue 标题、正文和 `handoff_note`。

评论按创建时间排序，因此合并进本轮的多条补充要求具有稳定顺序。Prompt 不会读取 Issue 的全部评论，也不会自动加入其他 Agent 的所有历史输出。

相关代码：`mica/runtime/providers/base.py`。

### 4.2 同一 Agent 的会话与代码上下文

Daemon 在执行 Task 前，按 `(issue_id, agent_id)` 查找最近一次执行，复用：

- `session_id → prior_session_id`；
- `branch_name`；
- `worktree_path`。

因此同一个 Agent 可以在同一 Issue 上继续此前的模型会话和代码修改；不同 Agent 拥有各自独立的 Session，本地代码 Agent 还拥有各自的 Worktree。

```text
Issue A
├─ Leader Agent     → Session L / Worktree L
├─ Geometry Agent   → Session G / Remote Context G
└─ Placement Agent  → Session P / Remote Context P
```

相关代码：`mica/runtime/daemon.py`、`TaskRepository.find_previous()`。

### 4.3 Leader 当前得到的协作上下文

Leader Task 不是常驻的 Leader 进程，而是一条由 Squad Leader Agent 执行的普通 Task，通过 `squad_id` 和 `is_leader_task=true` 标识。它主要在两个时机产生：Issue 首次指派给 Squad 时，由 Leader 判断是否需要拆分任务；同一批成员 Task 都完成、失败、取消或进入等待输入后，再创建一条 Leader Task 检查结果并决定下一步。

```text
Leader Task：拆分任务
        ↓
成员 Task：执行具体工作
        ↓
Leader Task：检查成员结果，继续委派或汇总完成
```

Daemon 执行 Leader Task 时，会在 Leader 自己的 Agent instructions 后追加 Squad briefing，包括：

- Leader 协作协议。
- Squad roster。
- 成员角色和 capabilities。
- Squad instructions。
- 当前 Issue、当前 Squad 最近 20 条已完成、失败、取消或等待输入的成员 Task 摘要。

这里的“成员 Task”是 `is_leader_task=false` 的具体执行任务。例如 Geometry 解析空间、Placement 生成布局都属于成员 Task。每条摘要只包含 `agent_id + status + output/error`，其中 `output/error` 最多保留 500 个字符。Leader 根据这些摘要返回结构化决策：继续向成员委派任务，或者汇总结果并将 Issue 推进到人工确认。

这 20 条记录按 Issue 和 Squad 筛选，但当前没有按某一次委派批次隔离；截断后的文本也可能丢失长 JSON。因此 briefing 适合帮助 Leader 快速判断，不应作为跨 Agent 结果的唯一事实来源。

相关代码：`mica/application/squads.py`。

### 4.4 Worker 当前得到的委派上下文

Worker 不会得到 Leader 的完整 Session，也不会自动得到其他 Worker 的结果。它当前主要得到：

```text
Issue title
Issue body
Delegated instruction: <handoff_note>
```

这意味着当前 Mica 的跨 Agent 协作依赖 Leader 中转：前一个成员的 output 先进入 Leader briefing，再由 Leader 在下一次 assignment 中决定是否传给后一个成员。

### 4.5 不同 Provider 的传递方式

四种 Provider 使用相同的 Task 业务输入，但私有会话载体不同：

| Provider | 私有会话载体 | 本轮公共输入 |
|---|---|---|
| Codex CLI | thread ID | `build_task_prompt()` 生成的文本 |
| Claude Code | session ID | 文本 Prompt + Agent instructions |
| ACP | session ID | `session/prompt` 文本 |
| A2A | contextId/taskId | A2A Message text，必要时附 output schema |

A2A 的 contextId 只代表该远端 Agent 的连续上下文，并不会使不同远端 Agent 自动共享记忆。

## 5. 当前 Mica 存在的上下文缺口

### 5.1 Squad Worker 可能遗漏用户补充要求

假设 Issue 正文为：

```text
为 20 人会议室生成布局。
```

用户随后评论：

```text
消防通道至少 1.2 米，并且不能使用 A 类桌子。
```

Leader 再委派 Placement Agent：

```text
生成两个候选摆放方案。
```

当前 `squad.delegated` Task 的 Prompt 只包含正文和 handoff。如果 Leader 没有复述新增约束，Placement Agent 就可能不知道消防通道和产品限制。

这不是模型能力问题，而是平台没有为 Worker 提供完整或可查询的 Issue 公共上下文。

### 5.2 Worker 之间不能可靠引用结果

Geometry 的长 JSON 如果只保存在 `Task.output` 中，Placement 不能通过稳定 ID 引用它。让 Leader把 JSON 再复制进 handoff 会产生截断、转义和语义改写风险。

更合理的做法是把结果保存为结构化 `TaskResult/Artifact`，委派时只传引用：

```json
{
  "instruction": "基于已校验区域生成候选方案",
  "context_inputs": [
    {"type": "artifact", "id": "artifact_geometry_01"}
  ]
}
```

### 5.3 Leader briefing 不是可靠事实存储

当前最近 20 条、每条最多 500 字符的摘要适合展示，但不能代替事实源：

- JSON 可能被截断为无效内容。
- 历史批次与当前批次没有明确分组。
- 结果没有携带原委派指令和 Artifact 引用。
- Issue 较长时，旧但仍有效的关键结论可能被挤出窗口。

### 5.4 Session 作用域还不够精细

本质问题是：仅凭 `(issue_id, agent_id)` 不足以唯一确定一个可以安全恢复的 Session。同一个 Agent 在同一个 Issue 中可能先后承担普通执行者、Squad Leader 或 Squad Worker 等不同角色，而 Provider Session 会保留此前的角色指令、对话历史、工具调用和输出要求。

当前 Session 主要按 `(issue_id, agent_id)` 复用，没有继续区分：

- 普通直接执行；
- Squad Leader；
- Squad Worker；
- 同一 Agent 在不同 Squad 中的角色。

如果一个 Agent 在同一 Issue 中先直接执行具体任务，后来又以 Leader 身份运行，恢复同一个 Session 会同时包含“直接完成任务”和“拆分任务、委派成员”的角色要求，可能导致它跳过委派或返回错误的输出格式。可以把会话作用域扩展为：

```text
(issue_id, agent_id, role, squad_id)
```

Worktree 是否沿用相同作用域需要单独决策：代码分支可以有意共享，但模型 Session 应避免角色污染。

### 5.5 Session 丢失后的重建不足

当前 Mica 会重新发送 Issue 标题、正文和本轮输入，但不会自动重建完整评论历史、已确认结论和 Artifact 索引。Session 一旦不可恢复，Agent 可能丢失前几轮只存在于 Session 中的信息。

因此重要结论必须持久化到 Issue 公共层，而不能只留在 Provider Session。

## 6. Multica 的对应设计

2026-09-13 对照本机 Multica 源码核对，HEAD 为 `ca47495fcc3c4d2d0d6c7a421e45ddd0de98f0fd`。下文描述该本地版本；不将 Mica 的拟议设计视为 Multica 已实现能力。面试口述见 [第二个难点故事](./07-resume-storytelling.md#六第二个难点故事跨-agent-交接时如何让接手者拿到正确上下文)。

Multica 已经形成了更完整的“共享事实 + 私有会话 + 按需读取”模型。

### 6.1 不直接把完整 Issue 塞进 Prompt

Multica 的普通 Issue Task Prompt 主要提供 Issue ID，并要求 Agent 先执行：

```text
multica issue get <issue-id> --output json
multica issue comment list <issue-id> --roots-only --summary --compact --output json
```

Agent 先读取 Issue 概况和评论线程摘要，再只展开相关线程。这样不同 Agent 都能基于同一份持久化 Issue 数据工作，又不需要每轮重传所有历史。

对应实现：`server/internal/daemon/prompt.go` 的 `BuildPrompt()` 与 `buildPromptBody()`。

### 6.2 本轮必须处理的输入直接注入

评论触发时，Multica 仍会把最新触发评论和已合并评论直接放进 Prompt，确保本轮输入不会因为 Agent 忘记查询而丢失。

合并评论携带：

- comment ID；
- thread ID；
- author type/name；
- created time；
- content。

如果评论来自多个线程，Prompt 会要求 Agent 分别读取相关线程并在对应位置回复。完整历史仍通过 CLI 按需读取。

对应实现：`server/internal/daemon/prompt.go` 的 `buildCommentPrompt()`。

### 6.3 每个 Agent 维护独立 Session

Multica 使用 `(agent_id, issue_id)` 查询最近可恢复的 Session。失败、取消、上下文溢出和手工重试有更细的恢复规则，但不会把一个 Agent 的 Session 直接交给另一个 Agent。

因此 Squad Leader 后续被成员评论或子 Issue 状态变化重新唤醒时，通常会恢复自己在该 Issue 上的 Session；前提是 Runtime 未变化、Session 仍可访问且没有因上下文溢出等原因被判定为不可恢复。这个查询没有区分 Leader、Worker 和 Squad，仍存在 5.4 所述的角色污染风险。

对应实现：`server/pkg/db/queries/agent.sql` 的 `GetLastTaskSession`。

### 6.4 Squad 通过 Issue 共享信息

Multica 的 Squad Leader 被明确告知：每个成员都可以读取 Issue 的标题、描述、全部历史评论、附件和 Workspace 上下文，因此 delegation comment 不应重复整段背景，只写成员选择和额外约束。

成员执行后把结果作为 Issue 评论、子 Issue、附件、metadata 或状态变化持久化。Leader 被重新触发时再次读取 Issue，而不是只依赖一段平台拼接的 Worker output。

对应实现：`server/internal/handler/squad_briefing.go`。

### 6.5 区分正常续接、增量未知与上下文重建

当前 `buildCommentPrompt()` 已根据会话是否真正续接、增量是否已计算，选择不同的历史读取提示。正常续接且有新增评论时提供增量信息；已确认没有额外评论与根本没有计算增量是两个不同状态，后者不能被解释为“没有变化”。无历史会话，或者最新会话不可用而退回旧会话时，走上下文重建提示。

因此不能再概括成“每轮都必须全量扫描评论”，也不能仅凭 Session ID 非空就认为历史完整。相关分支调用 `BuildNewCommentsHint`、`BuildResumedCommentsHint`、`BuildResumedUnknownDeltaCommentsHint` 和 `BuildColdCommentsHint`；这是 Prompt 侧的读取引导，不等于平台已经强制每个 Agent 执行了所有读取。

可借鉴的是：正常续接优先处理变化，连续性不可信时重建。重复追加历史仍可能增加输入成本；具体缓存收益取决于 Provider，不能从这些代码直接推导固定节省比例。Mica 拟议的统一 `context_revision`、输入清单和按 Agent 已读游标仍属于后续设计，不应直接归因于本次核对的 Multica 实现。

### 6.6 上下文读取受到范围与预算约束

Multica 没有让 Agent 无限制读取所有内容：

- 评论查询支持 roots summary、thread tail、since 和分页。
- Claim 时评论输入有稳定的 payload budget。
- 跨 Workspace 的 comment ID 按找不到处理，防止文本越权泄漏。
- Workspace Context、Project Context、Agent instructions 和每轮 initiator 等按不同生命周期注入。
- Session 无法恢复时会明确通知 Agent重新读取持久化上下文。

这里真正值得参考的不是某个 Prompt 模板，而是：**共享上下文可查询、当前输入有快照、读取有预算、Session 可丢失。**

## 7. Mica 的建议演进方案

### 阶段一：补齐可查询的 Issue 公共上下文

先提供统一的只读工具或内部 API，使本地 CLI、ACP 和 A2A Agent 都能按权限读取：

```text
get_issue(issue_id)
list_comment_threads(issue_id, summary=true)
get_comment_thread(thread_id, tail=30)
get_task_result(task_id)
get_artifact(artifact_id)
```

首次运行或 Session 不可恢复时，Prompt 要求 Agent先读取 Issue 概况；评论触发时仍直接注入本轮 `delivered_comment_ids`。

这一步解决“Worker 看不到用户先前评论”的核心缺口。

### 阶段二：引入结构化 TaskResult 与 Artifact

将 `Task.output` 从唯一结果载体升级为：

```text
TaskResult
├─ task_id
├─ summary
├─ status
├─ structured_data
├─ artifact_ids
└─ created_at

Artifact
├─ id
├─ issue_id
├─ source_task_id
├─ media_type
├─ schema_name/schema_version
├─ content or storage_ref
└─ visibility
```

文本总结用于人类和 Leader 快速判断，完整 JSON、文件或长报告通过 Artifact 引用读取。A2A data part 可以直接落入 `structured_data` 或 Artifact，而不是先转成可能被截断的字符串。

### 阶段三：委派时传递上下文引用

将当前单一 `handoff_note` 扩展为：

```text
instruction
context_inputs[]
expected_output_schema
```

平台在 Worker 启动前校验引用：

- 对象是否属于同一 Workspace/Issue。
- Worker 是否有权限读取。
- 依赖 Task 是否已到达允许状态。
- Artifact schema 是否满足下游 Agent 要求。

Leader 负责选择依赖，平台负责验证和装载，避免把数据复制粘贴进自然语言。

### 阶段四：为 Context 建立版本和输入清单

Task 领取时保存 `context_revision` 和 `context_manifest`：

```json
{
  "issue_revision": 12,
  "delivered_comment_ids": ["comment_1", "comment_2"],
  "thread_refs": ["thread_8"],
  "task_result_refs": ["result_geometry_1"],
  "artifact_refs": ["artifact_layout_constraints_1"],
  "project_revision": "git-sha"
}
```

这让任务回放不再依赖“当时数据库大概是什么状态”，而是能解释本次执行消费了哪些版本的输入。

### 阶段五：控制上下文预算

上下文装配器根据 Provider 能力和任务类型分配预算：

1. Agent/System instructions：必须保留。
2. 当前 Task 输入：必须完整保留。
3. 显式依赖的结构化结果：优先保留或以引用提供。
4. 当前评论线程：保留最近相关窗口。
5. Issue 其他历史：先摘要，按需读取。
6. 旧执行日志和工具细节：默认不注入。

超出预算时不能静默截断 JSON。应保存完整 Artifact，只在 Prompt 中提供摘要、schema 和引用。

## 8. 推荐的执行流程

```text
用户创建/补充 Issue
        │
        ▼
持久化 Issue、Comment、Artifact
        │
        ▼
Leader 读取共享上下文与 roster
        │
        ▼
生成 assignment + context_inputs
        │
        ▼
Task 领取时固化 context_manifest
        │
        ├─ 注入 Agent 私有 instructions / skills
        ├─ 恢复该 Agent 自己的 Session / Worktree
        ├─ 直接注入本轮必须处理的评论
        └─ 提供共享上下文与 Artifact 的按需读取工具
        │
        ▼
Worker 产出 TaskResult + Artifact + Issue Comment
        │
        ▼
Leader 根据持久化结果继续委派或汇总
```

这里不要求所有 Agent 使用相同模型，也不要求共享 Session。它们只需要遵守统一的上下文读取和结果发布契约。

## 9. 安全与一致性边界

### 9.1 上下文可见性

共享上下文必须同时校验：

- Workspace 归属。
- Issue 访问权限。
- Agent/Task 的工具授权。
- Artifact visibility。

不能仅因为调用方知道 comment ID 或 artifact ID 就允许读取。

### 9.2 Prompt Injection

历史评论、附件和其他 Agent 输出都是不可信业务数据，不能与平台 System Prompt 混为同一优先级。装配时应明确标记来源和用途，引用内容只能作为资料，不能覆盖系统权限策略。

### 9.3 一致性

`context_manifest` 记录的是领取时输入快照。Agent 运行期间 Issue 仍然可以变化，但新增内容进入后续 Task，或者通过显式的 context update event 注入。

### 9.4 至少一次执行

租约恢复可能让同一 Task 再次运行。TaskResult 和 Artifact 写入应使用 `task_id + attempt` 或业务幂等键，避免重试生成多份无法区分的结果。

## 10. 当前实现边界

- Mica 目前没有面向所有 Provider 的 Issue/Comment/Artifact 读取工具。
- Squad Worker 不会自动读取完整 Issue 评论历史。
- 跨 Agent 结果主要依赖 Leader briefing 和 handoff 文本。
- Leader briefing 对最近结果有数量和字符截断，不适合作为完整事实源。
- 尚未实现 `TaskResult`、通用 Artifact、`context_inputs` 和 `context_manifest`。
- Session 作用域尚未区分 direct、leader、worker 和 squad role。
- 当前没有自动摘要、上下文预算器和 schema version。

因此现在可以说项目已经实现“同一 Agent 的会话续接”和“Task 级输入快照”，但不能说已经实现完整的多 Agent 共享记忆或 Context Engineering 平台。

## 11. 面试时的 90 秒讲法

“同一个 Issue 可能由本地 Coding Agent 和多个远程业务 Agent 分阶段处理，但不同 Agent 的 Provider Session 无法共享，所以我没有把 Session 当成公共记忆，而是把上下文拆成四层：Workspace/Project 背景、Issue 公共事实、Task 输入快照和 Agent 私有会话。当前评论在 Task 领取时通过 delivered comment IDs 固化，同一 Agent 复用自己的 Session 和 Worktree；Squad Leader 通过 handoff 委派成员，并接收成员结果。对照 Multica 后我发现，仅靠 handoff 和截断后的 Worker output 不足以支撑复杂协作，更合理的是让所有 Agent 按权限查询同一份 Issue 评论和 Artifact，并用结构化结果引用完成跨 Agent 交接。这样 Session 丢失后仍能从持久化事实恢复，同时可以按线程、版本和预算装配上下文，而不是每轮把全部历史塞进 Prompt。”

## 12. 简历表述

如果完成阶段一至四，可以写成：

> **多 Agent 上下文管理**：针对同一 Issue 由多个异构 Agent 分阶段处理、Provider Session 无法跨 Agent 共享的问题，将上下文拆分为持久化的 Issue 公共事实、Agent 私有会话与 Task 输入快照；通过评论线程、结构化 TaskResult、Artifact 引用和上下文版本完成跨 Agent 交接，使 Session 丢失后仍可恢复执行，并通过按需读取与预算控制降低长任务的上下文膨胀。

在当前代码尚未补齐共享读取和 Artifact 前，更准确的表述是：

> **Agent 上下文续接**：按 Issue 与 Agent 隔离并复用 Provider Session、代码分支和工作目录，在 Task 领取时固化本轮评论输入；Squad 通过成员委派指令和终态结果向 Leader 回传上下文，为后续引入可查询的 Issue 公共上下文与结构化 Artifact 交接建立执行基础。
