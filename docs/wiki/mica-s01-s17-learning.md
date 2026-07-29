# mica s01-s17 完整学习文档

本文面向第一次系统学习 `mica` 的读者。`mica` 不是把 `multiCa` 完整复制一遍,而是把一个多 agent 协作平台拆成十七个可运行、可测试、可解释的学习切片。读完 s01-s17,你应该能理解一条用户需求如何从 issue 进入系统,如何变成 task,如何被 daemon 认领,如何交给 CLI agent 执行,如何产生实时事件、持久化记录、评论续跑、定时触发、workspace 隔离、squad 协调、project workdir、agent profile、后台 daemon loop、execution context,以及 runtime capacity / slot 调度。

## 1. 系统总览

`mica` 的主线可以概括为:

<whiteboard type="mermaid">
flowchart LR
  User["用户需求"] --> Issue["Issue 用户视角"]
  Issue --> Task["Task 一次执行"]
  Task --> Claim["Daemon claim"]
  Claim --> Runtime["Runtime 本地执行端"]
  Runtime --> Runner["Runner / CLI agent"]
  Runner --> Events["Session events"]
  Events --> Status["Issue 状态更新"]
  Status --> Surfaces["Web / Realtime / Resume / Autopilot / Workspace / Squad"]
  Surfaces --> Context["Project workdir / Agent skills / Execution Context / Runtime slots"]
</whiteboard>

```text
用户需求
  -> Issue
  -> Task
  -> Daemon claim
  -> Runtime / Runner / CLI agent
  -> Session events
  -> Issue 状态更新
  -> Web / Realtime / Resume / Autopilot / Workspace / Squad
  -> Project workdir / Agent skills / Execution Context / Runtime slots
```

这里有三个视角必须分清。

**Issue 是用户视角。** 它表达“要做什么”,有标题、正文、状态、负责人、评论和父子关系。用户打开看板时看到的是 issue,而不是底层执行细节。

**Task 是执行视角。** 它表达“一次具体执行”。同一个 issue 可以有多条 task:第一次执行、评论后续跑、手动 rerun、leader 被重新唤醒、worker 子任务等,都应该是新的 task,而不是反复修改旧 task。

**Runtime 是执行端视角。** 它不是某个 task,也不是某个目录。runtime 表示本地 daemon 注册出来的可执行能力:它能 claim 属于自己的 task,在指定 workdir 中启动某个 CLI,并受 capacity / slot 限制。

这三层分开后,后续设计才清楚:

```text
Issue 负责表达需求
Task 负责表达一次执行
Runtime 负责表达本机执行能力
Daemon 负责把 queued task 变成 runner 执行
```

## 2. s01: Issue 看板项、状态机、assignee

s01 建立系统最小用户模型:一个 issue 是看板上的卡片。它包含 `id`、`workspace_id`、`title`、`body`、`status`、`assignee_type`、`assignee_id` 等字段。

状态机是 s01 的核心。issue 不是任意改状态,而是沿着合法路径推进:

```text
backlog -> todo -> in_progress -> in_review -> done
                         \-> blocked -> todo / cancelled
```

这样设计的目的不是形式主义,而是给后续自动化执行留出稳定语义。daemon 开始执行时可以把 issue 推到 `in_progress`,执行成功后推到 `in_review`,失败后推到 `blocked`。如果状态可以随便跳,自动化就无法可靠判断当前处于哪一步。

assignee 是多态的。早期可以是 `member` 或 `agent`,后续扩展到 `squad`。这说明 `assignee_id` 本身不是“用户 ID”,而是要和 `assignee_type` 一起理解。

## 3. s02: Task 队列与 daemon 认领

s02 把“用户看到的 issue”和“系统执行的一次工作”拆开。创建 issue 后,系统会为对应 agent 入队一条 task。daemon 不直接认领 issue,而是认领 task。

task 有自己的状态机:

```text
queued -> dispatched -> running -> completed
                              \-> failed
                              \-> cancelled
```

`queued` 表示等待认领,`dispatched` 表示 daemon 已经抢到执行权,`running` 表示 runner 正在执行,`completed` / `failed` 表示执行终态。

daemon claim 必须是原子操作。s02 用 `TaskStore.claim()` 表达 CAS 语义:只有 `queued` task 可以被置为 `dispatched`。如果两个 daemon 同时看到同一条 task,最终也只能有一个 claim 成功。这个设计对应生产系统里的数据库行锁、状态条件更新或租约机制。

s02 还引入 `StubRunner`。它不调用真实模型,只合成执行事件,用于验证 claim、状态推进、失败处理等主链路。

## 4. s03: 真实 CLI Runner

s03 接入真实 Claude CLI runner,学习重点从“如何调一个命令”转为“如何把 agent CLI 当作可审计的执行后端”。

真实 CLI runner 不能只等待最终 stdout。agent CLI 往往会流式输出 system、assistant、tool_use、tool_result、result 等事件。系统需要逐行解析这些事件,一边记录过程,一边向前端或订阅者推送进度。

s03 的关键点有三个。

**解析和执行分离。** `parse_stream_line()` 是纯函数,负责把一行 JSON 转成内部事件或终态结果。`ClaudeCliRunner` 负责启动子进程、读取 stdout/stderr、判断 exit code。

**session_id 必须捕获。** CLI 返回的 session_id 是后续 `--resume` 的锚点。没有 session_id,评论续跑就只能重新开始。

**成功必须 fail-closed。** 如果 stream 结束但没有 result 事件,或者 result 标记错误,或者进程 exit code 非 0,系统都应该按失败处理。

## 5. s04: JSONL 持久化与重启恢复

s04 让系统从内存 demo 变成可跨进程恢复的教学系统。它使用 JSONL snapshot append 模式保存 issue、task 和 session events。

issue/task 采用“追加快照 + 重放 last-wins”。每次对象变化都追加一行完整快照。启动时顺序读取,同一个 id 后出现的记录覆盖前面的记录。这个模型实现简单,也方便学习者直接打开文件看状态演进。

session events 是另一类日志。它不是对象快照,而是 task 执行过程中的事件流。事件流用于回看执行过程、实时订阅、截断总结和排查问题。

s04 还处理 orphan recovery。重启时,如果某些 task 处于 `dispatched` 或 `running`,说明上一个 daemon 可能崩溃或进程被杀。系统不能让这些 task 永久卡住,因此会把它们恢复为 `failed`,错误信息为 `interrupted by restart`。

## 6. s05: 评论触发新 task 与 resume

s05 解决一个真实产品问题:用户在 issue 上补充评论后,agent 应该如何继续工作?

正确模型不是修改旧 task,而是创建新 task。旧 task 表达上一次执行,已经完成或失败;新评论代表新的触发,应该形成新的执行记录。这样系统能清楚回答:

- 第一次执行做了什么?
- 用户后来补充了什么?
- 第二次执行是否接上了之前 session?
- 如果第二次失败,失败发生在哪一次 task?

daemon 认领新 task 时,会查同一个 issue、同一个 agent 最近一次 completed task 的 session_id,并把它作为 `resume_session_id` 传给 runner。runner 再用 CLI 的 `--resume` 参数接回上下文。

## 7. s06: WebSocket 实时进度

s06 把 daemon 执行过程从“终端日志”变成“可订阅事件”。核心对象是 `RealtimeHub`,它是一个纯 Python pub/sub:按 task_id 或 issue_id 维护订阅者队列,daemon 每产生一个事件就 publish。

这里有两个重要取舍。

**Hub 是同步世界里的薄 pub/sub。** daemon 的 runner 回调通常在同步执行路径里,WebSocket server 则是 async 世界。`RealtimeHub` 用 `queue.Queue` 和锁把两边接起来,避免把整个执行链路改成 async。

**默认不做历史回放。** 订阅者只收到订阅之后的新事件。历史执行过程由 `SessionStore` 负责保存和读取。实时通道与历史存储分开,职责更清楚:WebSocket 管“现在发生什么”,SessionStore 管“过去发生了什么”。

## 8. s07: FastAPI 单页看板

s07 把前面的能力接到一个最小 Web UI。它提供:

- `GET /`: 返回内联 HTML 页面。
- `GET /api/issues`: 列出 issue。
- `POST /api/issues`: 创建 issue,可顺手派给 agent 并入队 task。
- `PATCH /api/issues/{id}/status`: 复用 s01 状态机修改状态。
- `POST /api/issues/{id}/comments`: 追加评论并创建新 task。
- `WS /ws/issues/{id}`: 订阅某个 issue 下的实时事件。

这个页面的价值不是 UI 完整度,而是验证系统边界:前端只操作 issue/comment/status/watch,不直接操纵 runner;执行仍由 task/daemon 负责。

## 9. s08: Skills 技能文件加载

s08 引入 skill,让 agent 执行时不只拿到 issue 标题和正文,还可以获得任务相关的额外能力说明。

教学版的 `SkillLoader` 从 workspace 的 `skills/<name>/SKILL.md` 扫描技能文件,解析 frontmatter 和 markdown 正文。s08 的核心是让学习者理解 skill 是可复用知识单元,不是某一次 issue 的正文。

后续 s14 会把 skill 选择从“按 issue 内容匹配”改成更接近 multiCa 的 agent-bound 模型:

```text
Agent.skill_names
  -> SkillLoader.get(name)
  -> daemon 注入 runner
```

因此 s08 的价值是先建立“skill 文件是什么、如何被加载”的基础。

## 10. s09: Cron Autopilot

s09 学习系统如何主动创建工作。前面章节的 issue 多由用户创建,s09 则加入 `Autopilot`:一个 cron 配置到期后,自动创建 issue 和 task。

`Autopilot` 包含 cron、agent_id、title、template、next_run、fail_count、enabled、timezone 等字段。`Scheduler.tick()` 检查所有 enabled autopilot,发现到期后执行三件事:

1. 创建 issue。
2. 为目标 agent 入队 task。
3. 推进 next_run。

next_run 在触发时就推进,而不是等 daemon 执行完成后再推进。这样可以避免调度器反复看到同一个过期时间,导致短时间重复触发。执行成功或失败是 daemon 的责任;调度触发是否发生是 scheduler 的责任。

## 11. s10: 多 CLI 与 runtime 边界

s10 把“一个 daemon 对一个 agent”升级为“一个 runtime daemon 托管多个 agent,并按 agent.cli 分派到不同 runner”。

这里需要区分三个概念。

**CLI/provider** 是具体执行工具,例如 `claude`、`codex`、`cursor-agent`。它是本机或环境里的命令。

**Agent** 是平台里的执行身份。agent 可以声明自己使用哪个 CLI,例如 `agent-claude` 使用 `claude`,`agent-codex` 使用 `codex`。

**Runtime** 是执行端边界。daemon 以某个 runtime_id 运行,只认领属于这个 runtime 的 task。runtime 不等于 CLI 二进制本身;它更像“这台机器上某组可执行 agent 能力的注册记录/执行端点”。

`PathDetector` 负责探测本机可用 CLI。`MultiCliRunner` 是 CLI 名到 runner 的路由表。daemon claim task 后,根据 task assignee 找到 agent,再根据 agent.cli 找到对应 runner。如果 runner 未注册,必须 fail-closed,不能悄悄落到默认 runner。

## 12. s11: Workspace 隔离与成员闸门

s11 引入 workspace 和 membership。它回答的问题是:当系统服务多个团队或项目时,谁可以看见和创建哪些 issue?

教学版有 `Workspace`、`Member`、`MemberStore` 和 `WorkspaceScope`。`MemberStore` 记录某个 workspace 下有哪些 member。`IssueStore.list()` 和 `IssueStore.create()` 可以接收 member_id 和 member_store,并在访问前做成员校验。

workspace 是协作和租户边界。一个用户可以属于多个 workspace;一个 workspace 里也可以有多个 member、agent、runtime、skill、squad。runtime “属于 workspace”不是说 CLI 二进制属于 workspace,而是说平台记录中的 runtime 与某个 workspace_id 绑定,它只能 claim 该 workspace 下授权给它的 task。

## 13. s12: Squads 多 agent 协调

s12 把 assignee 从单个 agent 扩展到 squad。一个 issue 可以分配给 `squad:sq-1`,但真正被唤醒的不是“squad 这个抽象对象”,而是 squad 的 leader agent。

Squad 的核心对象包括:

- `SquadMember`: 成员类型、成员 id、角色、技能标签。
- `Squad`: squad id、名称、leader_agent_id、instructions、members。
- `Task.is_leader_task`: 标记这条 task 是 leader 协调任务。
- `Task.squad_id`: 标记 task 属于哪个 squad。
- `Task.handoff_note`: 记录唤醒或委派原因。

主流程是:

<whiteboard type="mermaid">
flowchart TD
  Parent["Parent issue 分配给 squad"] --> LeaderTask["enqueue leader task"]
  LeaderTask --> Briefing["leader claim 后读取 squad briefing"]
  Briefing --> Plan["leader 拆分阶段和 child issues"]
  Plan --> WorkerTasks["enqueue worker tasks"]
  WorkerTasks --> LeaderIdle["leader 本轮 task 完成并进入 idle"]
  WorkerTasks --> WorkerDone["worker task completed 或 worker report"]
  WorkerDone --> Barrier{"stage barrier 是否关闭"}
  Barrier -- "否" --> Wait["等待同阶段其他 child 完成"]
  Barrier -- "是" --> Reawaken["enqueue 新 leader task"]
  Reawaken --> Review["leader 汇总结果并推进下一阶段"]
  Review --> Plan
</whiteboard>

```text
parent issue 分配给 squad
  -> 系统 enqueue leader task
  -> leader claim 时看到 squad briefing
  -> leader 创建 child issues 或委派 worker
  -> leader 完成本轮 task,进入 idle
  -> worker 完成 / 回报 / stage barrier 关闭
  -> 系统再次 enqueue leader task
  -> leader 汇总结果并推进下一阶段
```

leader 的“休眠”不是线程 sleep,而是它完成了一次 leader task,队列里暂时没有新的 leader task。leader 的“唤醒”也不是 worker 直接调用 leader,而是系统因为事件再次入队一条 `is_leader_task=True` 的 task。

child issue 和 stage barrier 是 s12 的关键学习点。leader 可以把父 issue 拆成多个 child issue,并给 child 标 stage。某个 child 完成后,`notify_child_done()` 会检查同一阶段以及更早阶段是否都已经 terminal。只有 barrier 关闭时,系统才重新唤醒 leader。这样可以避免每个 worker 一完成就反复打断 leader。

## 14. s13: Project + workdir

s13 解决真实代码执行必须面对的问题:agent CLI 不应该总是在启动 `mica` 的目录里运行,而应该进入当前任务对应的项目目录。

核心对象是 `Project`:

```text
Project.id
Project.workspace_id
Project.name
Project.local_path
Project.repo_url
```

`local_path` 是最重要字段。它表示 agent 应该进入哪个本地目录执行。创建 issue 时可以选择 project,daemon 执行 task 时通过 `issue.project_id -> Project.local_path` 解析出 workdir,再把它作为 runner 的 `cwd`。

这条链路是:

```text
Issue.project_id
  -> ProjectStore.get(project_id)
  -> project.local_path
  -> task.workdir
  -> runner.run(..., cwd=workdir)
```

因此 s13 让 `mica` 从“只能在教学仓库里跑 agent”升级为“可以指定目标项目目录执行 task”。

## 15. s14: Agent Profile 与 agent-bound skills

s14 把 agent 从“runtime 派生出来的名字”升级为可配置 profile。

`Agent` 包含:

```text
Agent.id
Agent.name
Agent.cli
Agent.runtime_id
Agent.system_prompt
Agent.skill_names
```

其中 `skill_names` 是 s14 的关键设计。Issue 不直接绑定 skill。skill 由 agent profile 声明:

```text
Agent.skill_names
  -> SkillLoader.get(name)
  -> merge_skills(...)
  -> daemon.execute 注入 runner
```

这样设计有两个好处。

第一,能力边界清楚。一个 issue 描述需求,不决定 agent 有哪些工具或技能。不同 agent 接同一个 issue,可见 skill 可以不同。

第二,更接近 multiCa 的 agent-skill 关系。生产系统里通常不会让任务文本临时决定权限和能力,而是通过 agent profile、workspace skill、权限关系决定执行上下文。

s14 还把 `system_prompt` 注入 runner,让 agent 的长期身份说明和 task 的一次性需求分离。

## 16. s15: Daemon Loop

s15 解决的是“任务入队后为什么还要手动 run_once”的问题。

s14 之前的执行方式是:

```text
TaskStore.enqueue(...)
用户或测试调用 daemon.run_once()
daemon claim queued task
runner 执行
```

s15 引入 `DaemonWorker`:一个常驻后台 worker。它平时等待,收到 wakeup 或 poll_interval 到期后,反复调用 `daemon.tick()`。

<whiteboard type="mermaid">
flowchart TD
  Enqueue["TaskStore.enqueue queued task"] --> Wakeup["WakeupTaskStore 通知 worker"]
  Wakeup --> Loop["DaemonWorker loop"]
  Poll["poll_interval fallback"] --> Loop
  Loop --> Tick["daemon.tick"]
  Tick --> Claim{"claim_next 是否拿到 task"}
  Claim -- "否" --> Sleep["等待 wakeup 或 poll_interval"]
  Sleep --> Loop
  Claim -- "是" --> Execute["daemon.execute task"]
  Execute --> Done["task completed 或 failed"]
  Done --> Drain["立即继续检查队列"]
  Drain --> Loop
</whiteboard>

核心循环是:

```text
while not stopped:
  while daemon.tick():
    drained = True
  if drained:
    continue
  wait(wakeup or poll_interval)
```

这里有两个重点。

**wakeup 让新 task 尽快执行。** `WakeupTaskStore.enqueue()` 成功后通知订阅者,`DaemonWorker.wakeup()` 让 worker 不必等下一次 poll。

**poll fallback 保证通知丢失时仍能恢复。** 即使 wakeup 没有送达,worker 下一轮 poll 仍会发现 queued task。

s15 的边界是:worker 只负责“什么时候调用 daemon.tick()”,不负责“如何执行 task”。执行逻辑仍在 `Daemon.execute()`。

## 17. s16: Execution Context

s16 把 task 执行前 runtime 应该看到的上下文写成文件。

教学版输出:

```text
project workdir/
  .mica/
    tasks/
      <task_id>/
        agent-identity.md
        task-context.md
        skills/
          <skill-name>/
            SKILL.md
        manifest.json
```

`ExecutionContextBuilder` 做五步:

```text
1. task.issue_id -> Issue
2. issue.project_id -> Project -> workdir
3. task.assignee_id -> Agent
4. Agent.skill_names -> SkillLoader.get(name)
5. 写 agent/task/skills/manifest 文件
```

它不 claim task,不调用 runner,也不改变 task 状态。它只负责 prepare。

这章的重要性在于把执行输入从“Python 参数”变成“runtime 可见文件”。这样 task 当时使用了哪个 agent identity、哪个 issue context、哪些 skills,都能在 workdir 下审计和调试。

## 18. s17: Runtime Capacity / Slot

s17 补上 runtime capacity。一个 runtime 同时能执行多少 task 是有限的。没有本地执行空位时,daemon 不应该把 task 从队列里 claim 走。

<whiteboard type="mermaid">
flowchart TD
  Start["SlotDaemonWorker wakeup"] --> Acquire{"acquire slot"}
  Acquire -- "无 slot" --> NoClaim["不 claim task"]
  NoClaim --> Wait["等待 release 或 poll"]
  Wait --> Start
  Acquire -- "slot 0 或 slot 1" --> Claim{"daemon.claim_next"}
  Claim -- "无 queued task" --> ReleaseEmpty["release 空 slot"]
  ReleaseEmpty --> Wait
  Claim -- "claim 到 task" --> Bind["bind_task slot 到 task"]
  Bind --> Thread["start thread"]
  Thread --> Execute["daemon.execute task"]
  Execute --> Release["finally release slot"]
  Release --> Wakeup["wakeup worker"]
  Wakeup --> Start
</whiteboard>

核心不变量是:

```text
先 acquire 本地 slot
再 claim task
claim 到 task 后 bind_task
task 结束后 release slot
release 后 wakeup worker
```

`RuntimeSlot` 表示一个执行名额:

```text
runtime_id
slot id
task_id
```

`RuntimeSlotPool` 管理 `_available` 和 `_occupied`。`acquire()` 会预留 slot,`bind_task()` 把 slot 和 task 关联起来,`release()` 把 slot 还回可用池。

`SlotDaemonWorker` 的关键区别是 claim 前先拿 slot:

```text
slot = slot_pool.acquire()
if no slot:
  不调用 daemon.claim_next()

task = daemon.claim_next()
if no task:
  release slot

bind slot to task
start thread:
  daemon.execute(task)
  release slot
  wakeup worker
```

这样 `max_slots=2` 且有 A/B/C 三个 queued task 时,系统会先运行 A/B,让 C 保持 queued。A 或 B 完成释放 slot 后,worker 再 claim C。

s17 用 `SlowRecordingRunner` 和线程模拟受控并发,不接真实 CLI 并发。测试关心的是调度不变量:

```text
runner.max_active <= max_slots
没有 slot 时第三个 task 保持 queued
release slot 后 queued task 被继续 claim
```

这对应 multiCa 中 `MaxConcurrentTasks`、slot semaphore、`waitForTaskSlot()`、`ClaimTasksForRuntimes(..., maxTasks=len(slots))` 和 `signalPollerWakeup(wakeup)` 的核心思想。

## 19. 一条完整运行链路

把 s01-s17 串起来,可以得到一个更完整的教学场景。

用户在 Mission Control 创建 project,指定 `local_path`。然后创建 agent profile,填写 `system_prompt`,绑定 `skill_names`。用户创建 issue 时选择 project 和 assignee。IssueStore 保存 issue,TaskStore 入队 queued task。

s15 的后台 DaemonWorker 或 s17 的 SlotDaemonWorker 被唤醒。s17 模型下,worker 会先检查 runtime slot。如果没有空 slot,task 保持 queued。如果有 slot,worker 才 claim task。claim 成功后 task 进入 dispatched,slot 绑定 task。

daemon 执行 task 时解析 issue、project、agent 和 skill。s16 可以把这些上下文写到 `.mica/tasks/<task_id>/` 下。runner 在 project workdir 中执行 CLI,产生 stream events。SessionStore 记录事件,RealtimeHub 推送实时进度。执行成功后 task 进入 completed,issue 进入 in_review;失败后 task 进入 failed,issue 进入 blocked。线程 finally 释放 slot 并唤醒 worker,后续 queued task 才能继续 claim。

如果 issue 分配给 squad,系统先唤醒 leader agent。leader 根据 briefing 拆 child issue、安排 worker、等待 stage barrier 或 worker report。leader 的每一次唤醒仍然是 task,也会进入同一条 daemon/runtime/slot 执行链路。

## 20. 现在这个系统是否完整

做完 s01-s17 后,`mica` 已经具备一个多 agent 平台的主要学习骨架:

- issue 看板和状态机。
- task 队列、claim、daemon 执行。
- 真实 CLI runner 和 stream 解析。
- JSONL 持久化、session event、orphan recovery。
- 评论续跑和 session resume。
- WebSocket 实时进度和 FastAPI 看板。
- skill 文件加载。
- cron autopilot。
- 多 CLI、多 runtime 边界。
- workspace 成员隔离。
- squad leader-first 多 agent 协调。
- project workdir。
- agent profile、system_prompt、agent-bound skills。
- 后台 daemon loop、wakeup 和 poll fallback。
- execution context 文件物化。
- runtime capacity / slot-before-claim 调度。

但它仍然是教学系统,不是生产系统。生产系统还需要数据库事务、SQL 级权限、认证授权、完整 daemon 注册和心跳、runtime admission、完整 skill bundle、通知系统、审计、取消、超时、orphan recovery 策略、批量 claim、分布式锁、指标观测和更完整的前端。

`mica` 的价值在于它把生产系统的关键概念压缩成可以读、可以跑、可以测试的最小实现。学习时不应该只看代码能不能跑,更应该看每一章解决了上一章的哪个缺口,以及这个设计如何为下一章提供基础。

## 21. 推荐学习顺序

如果你是第一次读,建议按下面节奏学习:

1. 先跑 s01-s02,确认 issue 和 task 的分层。
2. 再读 s03-s05,理解真实 CLI、session_id、resume 的关系。
3. 接着读 s06-s07,观察事件如何从 daemon 到前端。
4. 然后读 s08-s10,理解 skill、autopilot、runtime 如何改变执行上下文和调度边界。
5. 读 s11-s12,把系统从单用户单 agent 扩展到 workspace 和 squad 协作。
6. 读 s13-s14,理解 project workdir、agent profile 和 agent-bound skills。
7. 读 s15-s17,理解 daemon 常驻执行、execution context 和 runtime capacity。
8. 最后运行 s99 Mission Control,把 Project、Agent、Skill、Issue、Daemon、Task trace 和 Context 文件串起来操作一遍。

每章都建议同时看三类文件:

- `sessions/zh/sXX_*.md`: 概念和学习说明。
- `sessions/zh/sXX_*.py`: 可运行教学代码。
- `tests/test_*.py`: 这个章节认为必须成立的行为契约。

如果某个概念读起来抽象,优先去测试里找最小场景。测试通常比正文更直接地表达了“这个功能到底保证什么”。
