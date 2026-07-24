# mica-core Specification

> mica 的主 capability。覆盖 issue 看板(含 review/blocked 状态)、Task 执行队列、daemon 认领 task、AgentRunner(真实 CLI)、JSONL 持久化(issues+tasks+sessions)、WebSocket 实时、FastAPI 看板、markdown 技能、cron autopilot、多 CLI PATH 探测、多 workspace 隔离。本 spec 首次建立,作为各章在生产形态下如何接线的契约。
>
> s12 squads 为可选附录,不属本 spec 契约范围;其实现见 tasks §12,主线 s01–s11 不依赖它。
>
> 模型要点:issue 是板子卡(用户产物,状态反映整体生命周期);task 是一次执行尝试(agent_task_queue 一行,有自己的 status/session_id)。1 issue : N tasks。评论不改 issue 状态,而是入队一条新 task。

## ADDED Requirements

### Requirement: Issue 模型与状态机

系统 SHALL 用 `Issue{id, workspace_id, title, body, status, assignee_type, assignee_id, created_at, updated_at, error?}` 建模板子卡,`status ∈ {backlog, todo, in_progress, in_review, done, blocked, cancelled}`。状态转移 SHALL 仅允许:`backlog→todo`、`todo→in_progress`、`in_progress→in_review|done|blocked`、`in_review→done|in_progress|blocked`、`blocked→todo|cancelled`;其它转移 SHALL 被拒绝并报错。`done` 与 `cancelled` 为终态。Issue 状态 SHALL NOT 因评论而改变(评论走 Task 入队,见「会话延续与评论重跑」)。

#### Scenario: 合法转移
- **WHEN** 一个 `todo` issue 被认领执行,置为 `in_progress`
- **THEN** 转移成功,`updated_at` 刷新

#### Scenario: review 流转
- **WHEN** 一条 `in_progress` issue 的执行完成,置为 `in_review`
- **THEN** 转移成功;评审通过可 `in_review→done`,要求返工可 `in_review→in_progress`

#### Scenario: 非法转移被拒
- **WHEN** 试图把 `done` issue 置为 `todo`
- **THEN** 转移被拒绝,issue 状态不变,抛/返回错误

### Requirement: 多态 Assignee

系统 SHALL 用 `assignee_type ∈ {member, agent}` + `assignee_id` 表示多态指派;一个 issue 可指派给 member 或 agent。所有认领、展示、过滤逻辑 SHALL 基于此多态字段,不假设 assignee 必为 agent。

#### Scenario: 指派给 agent
- **WHEN** 创建/指派 issue 给 `agent-1`
- **THEN** `assignee_type=agent`、`assignee_id=agent-1`,可为其入队 task

#### Scenario: 指派给 member
- **WHEN** 指派 issue 给 member `alice`
- **THEN** `assignee_type=member`、`assignee_id=alice`,该 issue 不入队 agent task(留待人工)

### Requirement: Task 实体与执行队列

系统 SHALL 用 `Task{id, issue_id, assignee_type, assignee_id, status, session_id?, trigger_comment_id?, rerun_of_task_id?, prior_session_id?, created_at, claimed_at, completed_at, error?}` 建模一次 agent 执行尝试,`status ∈ {queued, dispatched, running, completed, failed, cancelled}`。一个 issue 可有多个 task(1 issue : N tasks);旧 task 行 SHALL 保留作历史。Task 状态转移 SHALL 仅允许:`queued→dispatched`(claim)→`running`(start)→`completed|failed`;`→cancelled`。`completed/failed/cancelled` 为终态。

#### Scenario: 为 issue 入队 task
- **WHEN** 一条派给 agent 的 issue 要执行,系统为其入队一条 `queued` task(`assignee` 取自 issue)
- **THEN** task 行存在,`status=queued`,等待 daemon 认领

#### Scenario: 一 issue 多 task(历史)
- **WHEN** 一条 issue 已有一个 `completed` task,又因评论入队新 task
- **THEN** 旧 task 行保留(completed),新 task 行 `queued`;两者 `issue_id` 相同

### Requirement: Daemon 认领执行循环(claim task)

系统 SHALL 运行 daemon 轮询 Task 队列中 `assignee_type=agent && assignee_id=<本 daemon 的 agent> && status==queued` 的 task,逐个认领执行。认领 SHALL 用 CAS:仅当当前 `status==queued` 时置 `dispatched`,失败即跳过该 task。认领后 SHALL `dispatched→running`(start)→执行 runner→按结果置 `completed`(exit 0)或 `failed`(非 0)。Task 生命周期 SHALL 驱动 issue 状态:task `running` → issue 置 `in_progress`;task `completed` → issue 置 `in_review`;task `failed` → issue 置 `blocked` 并记 error。s10 起泛化为「`agent.runtime_id == 本 daemon.runtime_id`」的任意 agent(1 daemon : N agent)。CAS 为 store 层 API 不变量;mica 单 daemon 下用单写者+锁实现,"双 daemon 抢同一 task"场景可用并发模拟或双进程演示,不要求生产级多 daemon。

#### Scenario: 认领并完成
- **WHEN** 一个派给本 agent 的 `queued` task 存在,daemon 轮询命中
- **THEN** CAS `queued→dispatched` 成功 → `running` → exit 0 → task `completed`;issue → `in_review`

#### Scenario: 双 daemon 抢同一 task
- **WHEN** 两个 daemon 实例同时命中同一 `queued` task
- **THEN** 只有一个 CAS 成功置 `dispatched`,另一个失败并跳过

#### Scenario: 执行失败
- **WHEN** runner exit code 非 0
- **THEN** task 置 `failed`,`error` 记录;issue 置 `blocked`

### Requirement: AgentRunner 抽象与真实 CLI 执行

系统 SHALL 定义 `AgentRunner` Protocol:`run(task, issue, context, on_event, resume_session_id=None) -> RunResult{status, output, exit_code, session_id}`。SHALL 提供 `StubRunner`(发合成事件、无子进程)与 `ClaudeCliRunner`(spawn `claude -p --verbose --output-format stream-json "<prompt>"`,逐行解析 stream-json 事件经 `on_event` 转发,exit code 决定 done/failed)。系统 SHALL NOT 用 Anthropic API 假装 agent;模型连接由被 spawn 的真实 CLI 自身完成。事件映射:`assistant` 的 content_block_delta → progress/token;`assistant` 中的 `tool_use` 与 `user` 中的 `tool_result` SHALL 经 `on_event` 透传为 progress(含工具名/状态摘要),不静默丢弃;`result` subtype=success → done;`result` subtype=error 或 exit≠0 → fail;子进程退出但未发出 `result` 事件 SHALL 判 failed(fail-closed)。`ClaudeCliRunner.run` SHALL 从 stream 的 `system`/`result` 事件捕获 `session_id` 写入 `RunResult.session_id`;SHALL NOT 传 `--no-session-persistence`。

#### Scenario: stub runner 验证认领机制
- **WHEN** s02 用 `StubRunner` 跑一个 queued task
- **THEN** 发合成 progress + done 事件,task 置 `completed`,全程无子进程、无模型调用

#### Scenario: claude runner 真实执行
- **WHEN** `ClaudeCliRunner` 跑一个 task,本机 `claude` 已装并登录
- **THEN** spawn `claude -p --verbose --output-format stream-json`,stdout JSON 事件经 `on_event` 转发,exit 0 时 task `completed`、`RunResult.session_id` 非空

#### Scenario: CLI 缺失报错不派活
- **WHEN** `claude` 不在 PATH
- **THEN** task 置 `failed`,`error` 提示 CLI 未找到,不静默跳过

#### Scenario: 工具调用事件透传
- **WHEN** claude 发出 `tool_use`/`tool_result` 事件
- **THEN** runner 经 `on_event` 转发为 progress(含工具名/状态),不静默丢弃

#### Scenario: 流结束未见 result 判失败
- **WHEN** 子进程退出但未发出 `result` 事件
- **THEN** task 置 `failed`,`error` 提示 stream 未正常结束

### Requirement: JSONL 持久化与重放

系统 SHALL 用 JSONL 追加写持久化 issue(`workspace/issues.jsonl`)、task(`workspace/tasks.jsonl`)与 agent 会话事件流(`workspace/sessions/<task_id>.jsonl`,事件为 claim/progress/token/done/fail)。重启 SHALL 重放重建 issue/task 状态;对重启时处于 `running`(或 `dispatched`)但无运行中 runner 的 task,SHALL 置 `failed` 并在 `error` 记录中断原因(不自动重派)。会话事件数超阈值时 SHALL 用截断式 summarize:保留最近 N 条原文,旧事件合并为一条 `{type:"summary",dropped_count,range}` 占位事件,SHALL NOT 调用模型。

#### Scenario: 写后重启状态恢复
- **WHEN** 一个 task 正在 `running` 时进程退出,重启后加载 JSONL
- **THEN** task 重放为 `running`;orphan 恢复将其置 `failed` + 记 error

#### Scenario: 溢出 summarize
- **WHEN** 某会话事件数超过阈值(默认 100,可配置)
- **THEN** 保留最近 N 条原文,旧事件合并为一条 `{type:"summary",dropped_count,range}` 占位事件,不调模型

### Requirement: 会话延续与评论重跑(Session Resume)

系统 SHALL 支持对 issue 的「评论」(追加 user 消息)。评论 SHALL NOT 改变 issue 状态,而是为该 issue 的 assignee 入队一条新 `queued` task(`trigger_comment_id` 指向该评论);daemon 下次 poll 认领该 task。daemon 认领一条 task 时,SHALL 查同 `(issue, agent)` 的上一条 task 的 `session_id` 作为 `prior_session_id`,经 `claude -p --verbose --output-format stream-json --resume <prior_session_id>` 续会话,使新评论/任务作为新 user turn 带历史继续;无上一 task 则全新会话。若 `--resume` 后 claude 吐回与传入不同的全新 `session_id`(resume 未接上),SHALL 用新 id 覆盖本 task 的 `session_id`,不沿用失效 id。手动重跑 SHALL 新建一条 task(`rerun_of_task_id` 指向源 task)而非在原 task 上续。

#### Scenario: 评论入队新 task,issue 状态不变
- **WHEN** 一条 `in_review` issue 收到评论
- **THEN** issue 仍 `in_review`;为新 task 入队一行(`queued`,`trigger_comment_id` 设,`assignee` 取自 issue)

#### Scenario: 认领时 resume 上一 task
- **WHEN** daemon 认领一条由评论触发的 task,且同 (issue,agent) 有上一 `completed` task
- **THEN** 命令含 `--resume <上一 task.session_id>`,claude 续上次会话

#### Scenario: resume 未接上降级
- **WHEN** `--resume` 后 claude 吐回全新 `session_id`
- **THEN** 用新 id 覆盖本 task.session_id,不沿用失效 id

#### Scenario: 不禁用会话持久化
- **WHEN** `ClaudeCliRunner` 构造命令
- **THEN** 命令 SHALL NOT 含 `--no-session-persistence`

### Requirement: WebSocket 实时进度

系统 SHALL 在 daemon 侧维护 `RealtimeHub`(task/issue→订阅者集合),`on_event` 经 `publish(id, event)` 推 JSON 事件给所有 WS 订阅者。SHALL 在 `MULTICA_WS_PORT` 提供 WS server。终端客户端 `task watch <id>` SHALL 连 WS 订阅并实时打印事件。晚到订阅者 SHALL NOT 获历史回放——只收订阅之后的新事件;历史走 `workspace/sessions/<task_id>.jsonl`,daemon 端不缓冲历史。

#### Scenario: 订阅后实时收到进度
- **WHEN** 客户端 `task watch <id>` 连上 WS,daemon 正在执行该 task
- **THEN** 客户端实时收到 progress/token/done 事件,无需轮询

#### Scenario: 多订阅者广播
- **WHEN** 同一 task 有 2 个 WS 订阅者
- **THEN** 每个事件被推给两者

#### Scenario: 晚到订阅者不获历史回放
- **WHEN** 客户端在 task 已产生若干事件后才连上 WS
- **THEN** 只收订阅之后的新事件;历史走 `sessions/<task_id>.jsonl`

### Requirement: FastAPI 单页看板

系统 SHALL 用 FastAPI 提供:`GET /` 返回单页 HTML(内联 JS、无构建)、`GET /api/issues` 列 issue、`POST /api/issues` 创建 issue、`PATCH /api/issues/{id}/status` 触发状态机校验并转移(复用 s01)、`POST /api/issues/{id}/comments` 加评论(入队新 task)、`WS /ws/issues/{id}` 复用 RealtimeHub 推实时进度。SHALL 在 `MULTICA_WEB_PORT` 起服务。页面 SHALL 列 issue 行、就地实时更新选中行进度,并提供创建表单、行内状态转移、评论框。

#### Scenario: 从看板创建并派活
- **WHEN** 在页面提交 title + assignee
- **THEN** `POST /api/issues` 创建 issue 并入队 task,出现在列表

#### Scenario: 从看板推进状态
- **WHEN** 在页面把 `in_review` issue 置 `done`
- **THEN** `PATCH` 触发状态机校验,成功后行更新;非法转移被拒

#### Scenario: 从看板评论触发 follow-up
- **WHEN** 在页面给 `in_review` issue 提交评论
- **THEN** `POST /comments` 入队新 `queued` task,issue 状态不变

### Requirement: Markdown 技能注入

系统 SHALL 把 `workspace/skills/**/SKILL.md` 下带 frontmatter 的 markdown 视为技能;frontmatter 字段 SHALL 含 `name`、`description`、`match`(`match` 为正则,作用于 `issue.title` + `issue.body`)。`SkillLoader` SHALL 扫描技能目录,对每条技能用 `match` 正则测试当前 issue 的 title+body,取第一个命中的技能;无 `match` 字段的技能永不被自动选中;选中后经 `claude --append-system-prompt "<skill 正文>"` 注入 CLI 调用。换技能文件 SHALL 不改代码即生效。

#### Scenario: 按 match 字段选中技能
- **WHEN** skill `match: "fix.*bug"` 且 issue.title 命中
- **THEN** `SkillLoader` 选中它,`ClaudeCliRunner` 调用含 `--append-system-prompt`

#### Scenario: 无匹配技能照常执行
- **WHEN** issue 不命中任何技能的 `match`
- **THEN** runner 不带 `--append-system-prompt`,照常执行

### Requirement: Cron Autopilot

系统 SHALL 用 croniter 解析 `workspace/autopilots.jsonl` 中每条作业的 cron 表达式(默认 UTC,可配 `timezone`)。每条作业字段 SHALL 含 `id`、`cron`、`agent_id`、`title`、`template`、`next_run`、`fail_count`、`enabled`、`timezone`。每 tick 检查 `enabled` 且 `next_run` 已到的作业,到期触发后 SHALL 无论成败都把 `next_run` 推进到下一槽位并落盘;触发即创建一条 issue(指派给作业 agent)并入队 task,进入正常认领流程。执行成功 SHALL 清零 `fail_count`;连续多次失败(默认 5,可配置)SHALL 置 `enabled=false`;禁用后 SHALL NOT 自动恢复,需手动改 `enabled=true`。daemon 重启时 SHALL NOT 重复触发已过槽位(以落盘 `next_run` 为准)。

#### Scenario: cron 到期建 issue+task
- **WHEN** 作业 cron=`0 9 * * *`,时刻跨过 9:00
- **THEN** 创建一条派给指定 agent 的 issue + 入队 task,daemon 认领执行,`next_run` 推进并落盘

#### Scenario: 失败后仍推进 next_run
- **WHEN** 作业到期触发并执行失败
- **THEN** `next_run` 推进并落盘,`fail_count+1`,不重复触发同槽位

#### Scenario: 连续失败禁用
- **WHEN** 作业连续 5 次(默认,可配置)失败
- **THEN** `enabled=false`,不再到期触发

### Requirement: 多 CLI 与 PATH 探测(1 daemon : N agent)

系统 SHALL 用 `PathDetector` 探测 PATH 上 `claude`/`codex`/…,`Runtime` 记录 daemon 所在机器与其可用 CLI 列表,`Agent.runtime_id` 指向某 runtime。`MultiCliRunner` SHALL 按 `agent.cli` 字段分派到对应 runner;agent 所需 CLI 缺失时 SHALL 报错不派活。s10 起 daemon SHALL 认领本 daemon runtime 上任意托管 agent 的 queued task(1 daemon : N agent),认领后按该 agent 的 `cli` 经 `MultiCliRunner` 分派。

#### Scenario: PATH 探测可用 CLI
- **WHEN** `PathDetector` 在 PATH 上找到 `claude` 与 `codex`
- **THEN** `Runtime.clis` 含两者

#### Scenario: 所需 CLI 缺失
- **WHEN** agent 的 `cli=codex` 但 PATH 无 `codex`
- **THEN** 认领后执行报 `failed`,`error` 提示 codex 未找到

#### Scenario: 一 daemon 托管多 agent、按 cli 分派
- **WHEN** 一个 daemon(runtime=rt-1)托管 agent-A(`cli=claude`)与 agent-B(`cli=codex`),两者各有 queued task
- **THEN** daemon 认领两条,分别经对应 cli runner 执行

### Requirement: 多 Workspace 隔离

系统 SHALL 用 `WorkspaceScope` 让每条 issue/task 查询带 `workspace_id` 过滤;CLI/web SHALL 用 `X-Workspace-ID`(或 `--workspace`)选择当前 workspace,并携带当前 member 身份(`X-Member-ID`/`MULTICA_MEMBER_ID`)。系统 SHALL 维护 `workspace/members.jsonl`:每个 workspace 的 member 列表。issue/task 的 create/list SHALL 校验当前 member 属于该 workspace,非成员 SHALL 被拒(返回空/403)。跨 workspace SHALL NOT 互相串扰。

#### Scenario: 跨空间不串
- **WHEN** workspace A 与 B 各有一条 issue,当前选 B
- **THEN** `issue list` 只返回 B 的

#### Scenario: 非成员被拒
- **WHEN** member `alice` 不属于 workspace A,执行 `issue list --workspace A`
- **THEN** 返回空/403

### Requirement: 配置与工作区

系统 SHALL 从 `.env` 读取 `MULTICA_DEFAULT_CLI`、`MULTICA_POLL_INTERVAL`、`MULTICA_DATA_DIR`、`MULTICA_WS_PORT`、`MULTICA_WEB_PORT`、`MULTICA_AGENT_ID`。工作区 SHALL 位于 `MULTICA_DATA_DIR`(默认 `./workspace`),含 `issues.jsonl`、`tasks.jsonl`、`sessions/`、`skills/`、`workspaces.jsonl`、`members.jsonl`、`autopilots.jsonl`。

#### Scenario: 无默认 CLI 仍可跑前置章节
- **WHEN** 未设 `MULTICA_DEFAULT_CLI`,跑 s01/s02
- **THEN** 看板与认领(stub)照常工作,不要求 `claude` 在场

#### Scenario: s03+ 缺 CLI 拒执行
- **WHEN** s03+ 运行而 `claude` 未装/未登录
- **THEN** daemon 报错提示前置,不进入执行
