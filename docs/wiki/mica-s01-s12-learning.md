# mica s01-s12 完整学习文档

本文面向第一次系统学习 `mica` 的读者。`mica` 不是把 `multiCa` 完整复制一遍,而是把一个多 agent 协作平台拆成十二个可运行、可测试、可解释的学习切片。读完 s01-s12,你应该能理解一条用户需求如何从 issue 进入系统,如何变成 task,如何被 daemon 认领,如何交给 CLI agent 执行,如何产生实时事件、持久化记录、评论续跑、定时触发、workspace 隔离,以及最后如何扩展到 squad 多 agent 协调。

## 1. 系统总览

`mica` 的主线可以概括为:

```text
用户需求
  -> Issue
  -> Task
  -> Daemon claim
  -> Runner / CLI agent
  -> Session events
  -> Issue 状态更新
  -> Web / Realtime / Resume / Autopilot / Workspace / Squad
```

这里有两个视角必须分清。

**Issue 是用户视角。** 它表达“要做什么”,有标题、正文、状态、负责人、评论和父子关系。用户打开看板时看到的是 issue,而不是底层执行细节。

**Task 是执行视角。** 它表达“一次具体执行”。同一个 issue 可以有多条 task:第一次执行、评论后续跑、手动 rerun、leader 被重新唤醒、worker 子任务等,都应该是新的 task,而不是反复修改旧 task。

这条分层是后续所有设计的基础。没有 task,系统就无法表达重试、续跑、抢占、runtime 边界、session 追踪和多 agent 协作。

## 2. s01: Issue 看板项、状态机、assignee

s01 建立系统最小用户模型:一个 issue 是看板上的卡片。它包含 `id`、`workspace_id`、`title`、`body`、`status`、`assignee_type`、`assignee_id` 等字段。

状态机是 s01 的核心。issue 不是任意改状态,而是沿着合法路径推进:

```text
backlog -> todo -> in_progress -> in_review -> done
                         \-> blocked -> todo / cancelled
```

这样设计的目的不是形式主义,而是给后续自动化执行留出稳定语义。daemon 开始执行时可以把 issue 推到 `in_progress`,执行成功后推到 `in_review`,失败后推到 `blocked`。如果状态可以随便跳,自动化就无法可靠判断当前处于哪一步。

assignee 也是多态的。早期可以是 `member` 或 `agent`,后续扩展到 `squad`。这说明 `assignee_id` 本身不是“用户 ID”,而是要和 `assignee_type` 一起理解:同一个字符串在不同类型下指向不同对象。

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

s02 还引入了 `StubRunner`。它不调用真实模型,只合成执行事件,用于验证 claim、状态推进、失败处理等主链路。教学上先用 stub 是必要的:在接真实 CLI 之前,应该先证明调度链路本身是正确的。

## 4. s03: 真实 CLI Runner

s03 接入真实 Claude CLI runner,学习重点从“如何调一个命令”转为“如何把 agent CLI 当作可审计的执行后端”。

真实 CLI runner 不能只等待最终 stdout。agent CLI 往往会流式输出 system、assistant、tool_use、tool_result、result 等事件。系统需要逐行解析这些事件,一边记录过程,一边向前端或订阅者推送进度。

s03 的关键点有三个。

**解析和执行分离。** `parse_stream_line()` 是纯函数,负责把一行 JSON 转成内部事件或终态结果。`ClaudeCliRunner` 负责启动子进程、读取 stdout/stderr、判断 exit code。这样 parser 可以独立测试,runner 可以专注进程生命周期。

**session_id 必须捕获。** CLI 返回的 session_id 是后续 `--resume` 的锚点。没有 session_id,评论续跑就只能重新开始,无法接上上一次上下文。

**成功必须 fail-closed。** 如果 stream 结束但没有 result 事件,或者 result 标记错误,或者进程 exit code 非 0,系统都应该按失败处理。不能因为 stdout 里有一些文本就误判成功。

## 5. s04: JSONL 持久化与重启恢复

s04 让系统从内存 demo 变成可跨进程恢复的教学系统。它使用 JSONL snapshot append 模式保存 issue、task 和 session events。

issue/task 采用“追加快照 + 重放 last-wins”。每次对象变化都追加一行完整快照。启动时顺序读取,同一个 id 后出现的记录覆盖前面的记录。这个模型实现简单,非常适合教学:你能直接打开文件看到状态演进,也能理解事件溯源和快照之间的关系。

session events 是另一类日志。它不是对象快照,而是 task 执行过程中的事件流。事件流用于回看执行过程、实时订阅、截断总结和排查问题。

s04 还处理 orphan recovery。重启时,如果某些 task 处于 `dispatched` 或 `running`,说明上一个 daemon 可能崩溃或进程被杀。系统不能让这些 task 永久卡住,因此会把它们恢复为 `failed`,错误信息为 interrupted by restart。这是任务系统必须具备的基本自愈能力。

## 6. s05: 评论触发新 task 与 resume

s05 解决一个真实产品问题:用户在 issue 上补充评论后,agent 应该如何继续工作?

正确模型不是修改旧 task,而是创建新 task。旧 task 表达上一次执行,已经完成或失败;新评论代表新的触发,应该形成新的执行记录。这样系统能清楚回答:

- 第一次执行做了什么?
- 用户后来补充了什么?
- 第二次执行是否接上了之前 session?
- 如果第二次失败,失败发生在哪一次 task?

daemon 认领新 task 时,会查同一个 issue、同一个 agent 最近一次 completed task 的 session_id,并把它作为 `resume_session_id` 传给 runner。runner 再用 CLI 的 `--resume` 参数接回上下文。

这也是为什么 s03 必须捕获 session_id。s05 的 resume 不是独立功能,而是建立在 s03 的执行事件和 s04 的持久化记录之上。

## 7. s06: WebSocket 实时进度

s06 把 daemon 执行过程从“终端日志”变成“可订阅事件”。核心对象是 `RealtimeHub`,它是一个纯 Python pub/sub:按 task_id 或 issue_id 维护订阅者队列,daemon 每产生一个事件就 publish。

这里有两个重要取舍。

**Hub 是同步世界里的薄 pub/sub。** daemon 的 runner 回调通常在同步执行路径里,WebSocket server 则是 async 世界。`RealtimeHub` 用 `queue.Queue` 和锁把两边接起来,避免把整个执行链路改成 async。

**默认不做历史回放。** 订阅者只收到订阅之后的新事件。历史执行过程由 `SessionStore` 负责保存和读取。实时通道与历史存储分开,职责更清楚:WebSocket 管“现在发生什么”,SessionStore 管“过去发生了什么”。

s06 是 s07 Web 看板和后续可视化调试的基础。

## 8. s07: FastAPI 单页看板

s07 把前面的能力接到一个最小 Web UI。它提供:

- `GET /`: 返回内联 HTML 页面。
- `GET /api/issues`: 列出 issue。
- `POST /api/issues`: 创建 issue,可顺手派给 agent 并入队 task。
- `PATCH /api/issues/{id}/status`: 复用 s01 状态机修改状态。
- `POST /api/issues/{id}/comments`: 追加评论并创建新 task。
- `WS /ws/issues/{id}`: 订阅某个 issue 下的实时事件。

这个页面的价值不是 UI 完整度,而是验证系统边界:前端只操作 issue/comment/status/watch,不直接操纵 runner;执行仍由 task/daemon 负责。这保持了产品层和执行层的分离。

s07 也说明一个很重要的集成原则:Web API 不应该绕过底层模型。改状态仍调用 `IssueStore.transition()`,评论仍调用 `comment_and_enqueue()` 或等价逻辑,创建 issue 仍按 assignee 入队 task。前端只是入口,不是第二套业务规则。

## 9. s08: Skills 技能注入

s08 引入 skill,让 agent 执行时不只拿到 issue 标题和正文,还可以获得任务相关的额外能力说明。

教学版的 `SkillLoader` 从 workspace 的 skills 目录扫描 `SKILL.md`,解析 frontmatter 和 markdown 正文。执行前根据 issue 内容选择命中的 skill,并把 skill body 注入 runner 的 prompt 或 system prompt。

学习 s08 时要抓住三层含义。

**Skill 是可复用知识单元。** 它不是某一次 issue 的正文,而是可被多个任务复用的操作规范、工具说明、领域知识或工作流。

**Skill 选择发生在执行前。** daemon 认领 task 后,根据当前 issue 查找匹配 skill,再把 skill 交给 runner。这样 task 执行记录仍然存在,skill 只是影响本次执行上下文。

**教学版只实现最小召回。** 完整 multiCa 中,skill 还会涉及 workspace 资源、agent_skill 绑定、启停状态、bundle hash、daemon materialization、provider-specific 环境等。s08 的目标是先理解“为什么执行前需要注入可复用技能”,而不是一次性实现完整 skill 平台。

## 10. s09: Cron Autopilot

s09 学习系统如何主动创建工作。前面章节的 issue 多由用户创建,s09 则加入 `Autopilot`:一个 cron 配置到期后,自动创建 issue 和 task。

`Autopilot` 包含 cron、agent_id、title、template、next_run、fail_count、enabled、timezone 等字段。`Scheduler.tick()` 检查所有 enabled autopilot,发现到期后执行三件事:

1. 创建 issue。
2. 为目标 agent 入队 task。
3. 推进 next_run。

next_run 在触发时就推进,而不是等 daemon 执行完成后再推进。这样做可以避免调度器反复看到同一个过期时间,导致短时间重复触发。执行成功或失败是 daemon 的责任;调度触发是否发生是 scheduler 的责任。

fail_count 用于表达连续失败治理。教学版用简单计数和禁用机制说明这个方向,完整系统还会有 run history、admission control、runtime availability、去重锁等更复杂设计。

## 11. s10: 多 CLI 与 runtime 级调度

s10 把“一个 daemon 对一个 agent”升级为“一个 runtime daemon 托管多个 agent,并按 agent.cli 分派到不同 runner”。

这里需要区分三个概念。

**CLI/provider** 是具体执行工具,例如 `claude`、`codex`、`cursor-agent`。它是本机或环境里的命令。

**Agent** 是平台里的执行身份。agent 可以声明自己使用哪个 CLI,例如 `agent-claude` 使用 `claude`,`agent-codex` 使用 `codex`。

**Runtime** 是执行端边界。daemon 以某个 runtime_id 运行,只认领属于这个 runtime 的 task。runtime 不等于 CLI 二进制本身;它更像“这个 workspace 里某个可执行端的注册记录/执行端点”。

`PathDetector` 负责探测本机可用 CLI。`MultiCliRunner` 是 CLI 名到 runner 的路由表。daemon claim task 后,根据 task assignee 找到 agent,再根据 agent.cli 找到对应 runner。如果 runner 未注册,必须 fail-closed,不能悄悄落到默认 runner。

runtime_id 是 claim 的硬边界。一个 daemon 不应该抢另一个 runtime 的 task。这是多 daemon、多机器、多 CLI 并存时的基本隔离。

## 12. s11: Workspace 隔离与成员闸门

s11 引入 workspace 和 membership。它回答的问题是:当系统服务多个团队或项目时,谁可以看见和创建哪些 issue?

教学版有 `Workspace`、`Member`、`MemberStore` 和 `WorkspaceScope`。`MemberStore` 记录某个 workspace 下有哪些 member。`IssueStore.list()` 和 `IssueStore.create()` 可以接收 member_id 和 member_store,并在访问前做成员校验。

workspace 是协作和租户边界。一个用户可以属于多个 workspace;一个 workspace 里也可以有多个 member、agent、runtime、skill、squad。runtime “属于 workspace”不是说 CLI 二进制属于 workspace,而是说平台记录中的 runtime 与某个 workspace_id 绑定,它只能 claim 该 workspace 下授权给它的 task。

s11 的教学实现只做 Python 层校验。完整系统还会在 SQL 查询、handler、claim、agent/squad 间接关系、API token 和审计上重复贯彻 workspace 边界。

## 13. s12: Squads 多 agent 协调

s12 把 assignee 从单个 agent 扩展到 squad。一个 issue 可以分配给 `squad:sq-1`,但真正被唤醒的不是“squad 这个抽象对象”,而是 squad 的 leader agent。

Squad 的核心对象包括:

- `SquadMember`: 成员类型、成员 id、角色、技能标签。
- `Squad`: squad id、名称、leader_agent_id、instructions、members。
- `Task.is_leader_task`: 标记这条 task 是 leader 协调任务。
- `Task.squad_id`: 标记 task 属于哪个 squad。
- `Task.handoff_note`: 记录唤醒或委派原因。

主流程是:

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

`build_leader_briefing()` 让 leader 知道自己不是普通 worker。briefing 包含操作协议、成员 roster、每个成员的 role/skills 以及 squad instructions。leader 据此决定任务拆分和委派对象。

child issue 和 stage barrier 是 s12 的关键学习点。leader 可以把父 issue 拆成多个 child issue,并给 child 标 stage。某个 child 完成后,`notify_child_done()` 会检查同一阶段以及更早阶段是否都已经 terminal。只有 barrier 关闭时,系统才重新唤醒 leader。这样可以避免每个 worker 一完成就反复打断 leader。

`worker_report()` 是另一条唤醒路径。worker 在父 issue 上回报时,系统会给 leader 入队新 task;如果已经存在 queued/dispatched/running 的 leader task,则不重复入队。这个 pending 去重避免短时间多个 worker 回报导致 leader 被重复唤醒。

## 14. 一条完整运行链路

把 s01-s12 串起来,可以得到一个完整的教学场景。

用户在 Web 看板上创建一个 issue,把它分配给某个 squad。IssueStore 保存 issue,TaskStore 入队 leader task。daemon 以某个 runtime_id 运行,只 claim 属于自己 runtime 的 queued task。leader task 被 claim 后进入 running,runner 执行时产生 stream events,SessionStore 记录事件,RealtimeHub 把事件推给 WebSocket 订阅者。

leader 根据 briefing 创建两个 Stage 1 child issues:一个给 frontend agent,一个给 qa agent。child issue 如果是 todo 且 assignee 是 agent,系统会为 worker 入队普通 task。leader 完成当前 task 后不再占用执行权。

worker task 被 daemon 认领、执行并完成。第一个 child 完成时,stage barrier 仍然打开,leader 不会被唤醒。第二个 child 完成时,Stage 1 barrier 关闭,系统在父 issue 上记录系统评论,并 enqueue 一条新的 leader task。leader 再次执行,汇总 Stage 1 结果,把 Stage 2 从 backlog 推到 todo,继续派发。

如果用户在任意时刻评论补充要求,系统会创建新的 task,并用上一条 completed task 的 session_id 尝试 resume。如果某个 autopilot 到期,它也可以自动创建 issue 和 task,进入同一条执行链路。所有 issue/task/session 变化都能持久化,重启后可以恢复。

## 15. 现在这个系统是否完整

做完 s01-s12 后,`mica` 已经具备一个多 agent 平台的主干能力:

- issue 看板和状态机。
- task 队列、claim、daemon 执行。
- 真实 CLI runner 和 stream 解析。
- JSONL 持久化、session event、orphan recovery。
- 评论续跑和 session resume。
- WebSocket 实时进度和 FastAPI 看板。
- skill 注入。
- cron autopilot。
- 多 CLI、多 runtime 调度。
- workspace 成员隔离。
- squad leader-first 多 agent 协调。

但它仍然是教学系统,不是生产系统。生产系统还需要数据库事务、SQL 级权限、认证授权、完整 daemon 注册和心跳、runtime admission、完整 skill bundle、通知系统、审计、并发 worker 池、分布式锁、失败重试策略、指标观测和更完整的前端。

`mica` 的价值在于它把生产系统的关键概念压缩成可以读、可以跑、可以测试的最小实现。学习时不应该只看代码能不能跑,更应该看每一章解决了上一章的哪个缺口,以及这个设计如何为下一章提供基础。

## 16. 推荐学习顺序

如果你是第一次读,建议按下面节奏学习:

1. 先跑 s01-s02,确认 issue 和 task 的分层。
2. 再读 s03-s05,理解真实 CLI、session_id、resume 的关系。
3. 接着读 s06-s07,观察事件如何从 daemon 到前端。
4. 然后读 s08-s10,理解 skill、autopilot、runtime 如何改变执行上下文和调度边界。
5. 最后读 s11-s12,把系统从单用户单 agent 扩展到 workspace 和 squad 协作。

每章都建议同时看三类文件:

- `sessions/zh/sXX_*.md`: 概念和学习说明。
- `sessions/zh/sXX_*.py`: 可运行教学代码。
- `tests/test_*.py`: 这个章节认为必须成立的行为契约。

如果某个概念读起来抽象,优先去测试里找最小场景。测试通常比正文更直接地表达了“这个功能到底保证什么”。
