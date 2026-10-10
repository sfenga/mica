# mica-core Specification（MODIFIED 增量）

> 本变更修改 mica-core 的两条 Requirement：Task 实体（`issue_id` 可空 + chat 任务字段）与
> Daemon 认领执行循环（按任务形状分叉互斥）。以下为修改后的完整 Requirement 文本，
> 原文见 `openspec/changes/add-mica-core/specs/mica-core/spec.md`。

## MODIFIED Requirements

### Requirement: Task 实体与执行队列

系统 SHALL 用 `Task{id, issue_id?, chat_session_id?, chat_input_task_id?, reply_to_message_id?,
reply_in_thread?, agent_id, runtime_id, status, session_id?, trigger_comment_id?, ...}` 建模一次
agent 执行尝试，`status ∈ {queued, dispatched, running, completed, failed, cancelled}`。`issue_id`
与 `chat_session_id` SHALL 恰有一者非空：issue 任务（挂 issue）与 chat 任务（挂会话）共用本表与
生命周期。一个 issue 可有多个 task（1 issue : N tasks），旧 task 行 SHALL 保留作历史。Task 状态
转移 SHALL 仅允许：`queued→dispatched`(claim)→`running`(start)→`completed|failed`；`→cancelled`。
`completed/failed/cancelled` 为终态。

#### Scenario: 为 issue 入队 task
- **WHEN** 一条派给 agent 的 issue 要执行,系统为其入队一条 `queued` task(`assignee` 取自 issue)
- **THEN** task 行存在,`status=queued`,等待 daemon 认领

#### Scenario: 一 issue 多 task(历史)
- **WHEN** 一条 issue 已有一个 `completed` task,又因评论入队新 task
- **THEN** 旧 task 行保留(completed),新 task 行 `queued`;两者 `issue_id` 相同

#### Scenario: chat 任务无 issue
- **WHEN** 飞书会话批次 flush 创建 chat 任务
- **THEN** 该任务 `issue_id` 为空、`chat_session_id` 非空，不产生任何 issue 行

### Requirement: Daemon 认领执行循环(claim task)

系统 SHALL 运行 daemon 轮询队列认领 task 并执行。认领 SHALL 按**任务形状分叉互斥**：issue 任务
仅当同 `(issue_id, agent_id)` 无 dispatched/running 任务时可认领，且其 owner issue 不处于
`done`/`cancelled`；chat 任务仅当同 `chat_session_id` 无 dispatched/running 任务、且会话
`status='active'` 时可认领。两种形状经 `LEFT JOIN` 与 NULL 安全分支共存于同一认领语句，按
`created_at` 公平竞争。认领 SHALL 用 CAS：仅当当前 `status==queued` 时置 `dispatched`，失败即
跳过。认领后 SHALL `dispatched→running`(start)→执行→按结果置 `completed` 或 `failed`。
Task 生命周期 SHALL 驱动 issue 状态**仅限 issue 任务**：task `running` → issue `in_progress`；
task `completed` → issue `in_review`；task `failed` → issue `blocked` 并记 error；chat 任务
SHALL NOT 驱动任何 issue 状态。s10 起泛化为「`agent.runtime_id == 本 daemon.runtime_id`」的
任意 agent(1 daemon : N agent)。

#### Scenario: 认领并完成
- **WHEN** 一个派给本 agent 的 `queued` task 存在,daemon 轮询命中
- **THEN** CAS `queued→dispatched` 成功 → `running` → exit 0 → task `completed`;issue → `in_review`

#### Scenario: 双 daemon 抢同一 task
- **WHEN** 两个 daemon 实例同时命中同一 `queued` task
- **THEN** 只有一个 CAS 成功置 `dispatched`,另一个失败并跳过

#### Scenario: 执行失败
- **WHEN** runner exit code 非 0
- **THEN** task 置 `failed`,`error` 记录;issue 置 `blocked`

#### Scenario: issue 与 chat 任务互斥互不干扰
- **WHEN** 会话 S 的 chat 任务在跑，同时 issue I 的新任务入队
- **THEN** issue I 的任务正常认领执行；会话 S 的后继任务保持 queued 直至前序终态

#### Scenario: chat 任务失败不动看板
- **WHEN** 一个 chat 任务执行失败
- **THEN** 任务置 `failed`，无任何 issue 状态变化
