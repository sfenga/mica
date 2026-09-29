# 本地 Agent 调度与隔离执行

## 1. 业务需求

本地 Coding Agent 和普通远程 HTTP 服务不同。它们要读取开发者电脑上的项目，可能持续运行几十分钟，执行 shell、写文件、启动子进程，并占用较多 CPU 和内存。用户又希望从 Web 或飞书远程提交任务，而不必守在终端前。

因此本地执行层需要解决：

- Task 必须被拥有对应项目和 CLI 的机器领取。
- Runtime 有并发上限，不能先把大量 Task 标成运行中再排队等资源。
- 修改代码的任务不能互相覆盖，也不能污染用户当前工作区。
- 只读分析、普通问答不应被强制创建 Git Worktree。
- Daemon 崩溃、机器重启或网络中断后，Task 不能永久卡在 running。
- 用户取消任务时，要终止 CLI 及其子进程，而不只是停止 Python 等待。
- 同一 Issue 的后续要求应在已有代码和会话上下文上继续，而不是从零开始。

## 2. 演进过程

### V0：在 API 进程中直接启动 CLI

合理的原型方案是在收到请求后直接 `subprocess.Popen()`。它能验证 Codex/Claude Code 可调用，但 API 生命周期与长任务绑定；服务发布会中断任务；每个 API 实例都可能访问不同的本地路径；并发数量也没有统一边界。

### V1：拆出本地 Daemon，并让 Agent 绑定 Runtime

将执行器拆成独立 Daemon 后，控制平面只保存 Task，Daemon 按 `runtime_id` 领取。Agent 同时保存 `runtime_id + provider`：前者回答“在哪台执行节点运行”，后者回答“使用哪种 Agent 协议”。

这使未来增加第二台开发机不需要修改 Issue 模型，只需把 Agent 绑定到对应 Runtime。

### V2：先领取任务，再等待 Semaphore

早期容易写成“先 claim Task，再等待本地并发锁”。当两个 Slot 已满时，第三条任务已经从 queued 变成 dispatched，却没有进程真正执行；它会持有租约、制造假运行状态，并让用户无法判断为什么没有输出。

当前顺序改为 Slot-before-claim：Daemon 先从进程内 `RuntimeSlotPool` 预留容量，成功后才访问数据库领取 Task。没有 Slot 时 Task 继续保持 queued。

### V3：从状态更新演进为租约执行

只有 `claimed_by` 不足以恢复崩溃任务，因为 Worker 消失后没有人清理所有权。当前 Task 同时保存：

- `claimed_by`：当前 Worker。
- `lease_expires_at`：所有权失效时间。
- `attempt`：每次重新领取递增的执行代次。

Daemon 在执行期间发送心跳续租；轮询时把过期的 dispatched/running Task 恢复到 queued。完成或失败写回时校验 `worker_id + attempt`，旧进程即使恢复也不能覆盖新一轮结果。

这是 Mica 的恢复模型，不是 Multica 的字段级复刻。Multica 在任务启动前使用较短的 prepare lease，任务真正运行后主要根据 Runtime 心跳判断执行节点是否失联；Daemon 重启时会把旧执行记录标记为失败，再创建一条递增 attempt 的重试子任务。Mica 则让同一 Task 通过 `claimed_by + lease_expires_at + attempt` 重新排队并防止旧 Worker 晚到写回。面试中可以比较两种取舍，但不要说 Multica 也直接恢复同一行。

### V4：所有任务都建 Worktree

为了解决代码覆盖，最保守的方案是所有本地任务都创建 Worktree。但“解释一段代码”或“不关联项目的普通问答”并不需要 Git 分支；非 Git 目录也无法创建 Worktree，额外目录会让用户难以理解。

当前改为三种工作目录策略：

| 场景 | 工作目录 | 并发策略 |
|---|---|---|
| 项目选择 `worktree` | 独立 Git Worktree | 不同 Issue/Agent 可并发 |
| 项目选择 `in_place` | 用户当前项目目录 | 同一路径进程内串行 |
| 未关联项目的本地 Agent | Daemon 管理的空目录 | 彼此隔离，无 Git 分支 |
| 远程 A2A Agent | 不分配本机目录 | 由远端服务管理 |

### V5：每次后续要求创建新分支和新会话

如果每条补充都创建新 Worktree，Agent 看不到上次修改；如果只复用分支却不复用 CLI Session，模型还要重新阅读上下文，调用成本高。

当前按 `(Issue, Agent)` 查找最近执行上下文，后续 Task 复用：

- `branch_name` 和 `worktree_path`：延续已修改的代码。
- `session_id` → `prior_session_id`：让 Codex、Claude Code、ACP 或 A2A 延续协议会话。

Task 仍然是独立执行记录，因此每轮输入、状态、结果和失败可以分别追踪。

## 3. 当前调度流程

```text
Daemon.run_once
    │
    ├─ 1. acquire Runtime Slot
    ├─ 2. recover_expired(runtime_id)
    ├─ 3. claim_next(runtime_id, worker_id, lease)
    ├─ 4. 校验 Agent 仍绑定本 Runtime
    ├─ 5. 查找上一轮 session / branch / worktree
    ├─ 6. 准备 worktree、in_place 或空 workspace
    ├─ 7. bind_execution_context + task.start
    ├─ 8. 启动租约心跳
    ├─ 9. Provider.run(..., cancellation_check)
    └─ 10. complete / waiting_input / fail / cancel
```

主要实现：

- `mica/runtime/daemon.py`：Slot、领取、租约、工作目录和 Provider 调用。
- `mica/infrastructure/database/repositories.py`：原子领取、续租和过期恢复。
- `mica/runtime/worktree.py`：Git Worktree 与分支复用。
- `mica/runtime/workspace.py`：无项目任务的持久空目录。
- `mica/runtime/providers/cli.py`：Codex、Claude Code 流式进程和进程组取消。

## 4. 为什么指定 Agent 后仍然会有 Worker 竞争

“Issue 指定 Agent”确定的是业务执行者，例如 `general_cc`；“哪个 Worker 领取 Task”确定的是运行该 Agent 的 Daemon 实例。

它们是两个层次：

```text
Issue → Agent general_cc → Runtime local-mac
                              ├─ daemon-local-1
                              └─ daemon-local-2（未来可扩展）
```

即使当前只有一个 Daemon，Task 仍通过数据库领取，是为了保证进程重启恢复并保持未来扩展的一致语义。`FOR UPDATE SKIP LOCKED` 不是用来在多个 Agent 之间抢归属，而是避免同一 Runtime 的多个 Worker 重复执行同一 Task。

## 5. Git Worktree 的隔离语义

`WorktreeManager` 为 `(repository, issue_id, agent_id)` 生成稳定目录和分支：

```text
branch: mica/<issue-slug>/<agent-slug>
path:   <worktree-root>/<repo-hash>/<issue-agent>
```

首次执行使用 `git worktree add -b ... HEAD`；后续执行如果目录存在，会校验当前分支是否与 Task 记录一致后复用。为避免多个线程同时操作同一 Git 元数据，代码同时使用进程内 key lock 和 `.git` common directory 下的文件锁。

创建 Worktree 前要求源仓库干净。原因是 Git Worktree 基于提交或分支创建，未提交改动不会自动进入新目录；如果静默忽略，Agent 看到的代码会和用户预期不一致。

这一实现比 Multica 更保守。Multica 有更完整的仓库缓存与任务分支管理，并允许会话继续使用既有 Worktree；它不会用“源仓库必须全局干净”作为所有场景的统一前提。Mica 当前采用稳定的 `(Issue, Agent)` 分支并拒绝脏源仓库，是为了先把“看见哪份代码”讲清楚，代价是不能直接带入用户尚未提交的本地改动。

## 6. in_place 为什么需要串行

`in_place` 允许只读任务或用户明确希望直接修改当前目录的任务避免创建 Worktree。但两个 Agent 同时写同一个目录会互相覆盖，甚至一个任务执行测试时读到另一个任务尚未完成的修改。

当前 `LocalDirectoryLockPool` 按规范化绝对路径加锁。等待锁期间仍会检查取消信号。这个锁是单 Daemon 进程内的保护；如果未来允许同一目录被多个 Daemon 使用，需要升级成数据库 advisory lock 或文件锁。

Multica 会对部分只读 Chat 执行放宽仓库锁，而 Mica 当前对同一路径的所有 `in_place` 任务都串行。这是安全优先的简化：实现容易解释，但只读请求较多时并发利用率会更低。后续可在明确的只读能力声明和工具权限约束建立后，再增加共享读锁。

## 7. CLI 流式执行与取消

Codex 和 Claude Code 都由 `CliProvider` 启动为独立进程组：

- stdout JSONL 被解析成 `agent.text`、`agent.tool`、`agent.system` 等事件。
- stderr 保留有限尾部，用于返回可诊断错误。
- 取消、Daemon shutdown 或租约丢失时向整个进程组发送终止信号。
- Claude 使用双向 stream-json，Mica 写入任务并处理 `control_request`；自动批准工具调用，同时强制后台任务改为前台，以免子进程脱离 Task 生命周期。
- Claude 的 `thinking_tokens`、hook 等内部遥测不会进入用户事件时间线。

取消检查把平台状态和 Provider 连接起来：用户把 `cancel_requested` 写入数据库后，正在运行的 Provider 在轮询中发现并终止本地进程组。

## 8. 异常恢复示例

某 Task 被 `daemon-A` 领取，`attempt=1`，租约两分钟。运行一分钟后机器异常重启：

1. 心跳停止，租约到期。
2. 新 Daemon 调用 `recover_expired()`，Task 回到 queued。
3. 再次领取后 `attempt=2`。
4. 如果旧进程晚到并尝试以 `(daemon-A, attempt=1)` 完成，状态服务拒绝该写入。

需要注意：租约保证的是“旧 Worker 不能确认结果”，不是从物理上保证外部副作用恰好一次。如果 Agent 在崩溃前已经调用外部 API，重试仍可能再次调用，因此外部工具本身还需要幂等键。

## 9. 当前边界

- `RuntimeSlotPool` 是 Daemon 进程内容量，不是跨多 Daemon 的全局配额。
- 当前 Runtime 心跳和在线状态模型较轻量，未实现中心化节点选主。
- Worktree 创建基于当前 HEAD，且要求源仓库无未提交修改；尚未实现把用户 working tree 变更安全复制过去。
- 分支会复用，但尚未自动提交、合并、创建 PR 或清理 Worktree。
- `in_place` 的目录锁不能跨进程。
- 租约恢复是至少一次执行语义，不能替代业务工具的幂等设计。

## 10. 面试时的 90 秒讲法

“本地 Coding Agent 和普通 HTTP Agent 最大的区别是它要占用本机资源并操作工作目录。我把执行从 API 拆到本地 Daemon，Agent 绑定 Runtime，Daemon 只领取本机任务。调度上先预留 Runtime Slot 再 claim，避免任务已经显示运行却在本地等资源。数据库领取使用租约和 attempt：执行中持续续租，Daemon 崩溃后过期任务回到队列，旧 attempt 不能晚到覆盖新结果。工作目录不是一律建 Worktree，而是项目可选 worktree 或 in_place，未关联项目则使用隔离空目录；同一 Issue 和 Agent 的后续任务复用 Worktree 分支和 CLI Session。整体思路参考了 Multica 的 Runtime、Slot 与工作目录隔离，但同一 Task 的租约恢复和稳定 Issue/Agent 分支是 Mica 当前采用的简化实现。”
