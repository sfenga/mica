# mica-core Specification

> mica 的主 capability。覆盖 issue 看板与状态机、多态 assignee、daemon 认领执行、AgentRunner(真实 CLI)、JSONL 会话持久化、WebSocket 实时、FastAPI 看板、markdown 技能、cron autopilot、多 CLI PATH 探测、多 workspace 隔离。本 spec 首次建立,作为各章在生产形态下如何接线的契约。
>
> s11 squads 为可选附录,不属本 spec 契约范围;其实现见 tasks §11,主线 s01–s10 不依赖它。

## ADDED Requirements

### Requirement: Issue 模型与状态机

系统 SHALL 用 `Issue{id, workspace_id, title, body, status, assignee_type, assignee_id, created_at, updated_at, error?}` 建模任务,`status ∈ {todo, in_progress, done, failed}`。状态转移 SHALL 仅允许:`todo→in_progress`、`in_progress→done`、`in_progress→failed`;其它转移 SHALL 被拒绝并报错。`failed` 为终态;重试通过新建 issue 或人工置回 `todo`,不在本 spec 自动转移范围。

#### Scenario: 合法转移
- **WHEN** 一个 `todo` issue 被认领,置为 `in_progress`
- **THEN** 转移成功,`updated_at` 刷新

#### Scenario: 非法转移被拒
- **WHEN** 试图把 `done` issue 置为 `todo`
- **THEN** 转移被拒绝,issue 状态不变,抛/返回错误

### Requirement: 多态 Assignee

系统 SHALL 用 `assignee_type ∈ {member, agent}` + `assignee_id` 表示多态指派;一个 issue 可指派给 member 或 agent。所有认领、展示、过滤逻辑 SHALL 基于此多态字段,不假设 assignee 必为 agent。

#### Scenario: 指派给 agent
- **WHEN** `issue create --assignee agent-1` 创建 issue
- **THEN** `assignee_type=agent`、`assignee_id=agent-1`,daemon 可认领

#### Scenario: 指派给 member
- **WHEN** `issue create --assignee member:alice` 创建 issue
- **THEN** `assignee_type=member`、`assignee_id=alice`,该 issue 不会被 agent daemon 认领(留待人工)

### Requirement: Daemon 认领执行循环

系统 SHALL 运行 daemon 轮询 `IssueStore` 中 `assignee_type=agent && assignee_id=<本 daemon 的 agent> && status=todo` 的 issue,逐个认领执行。认领 SHALL 用 CAS:仅当当前 `status==todo` 时置 `in_progress`,失败即跳过该 issue。CAS 为 store 层 API 不变量;教学版单 daemon 下用单写者+文件锁实现,"双 daemon 抢同一 issue"场景可用并发模拟或双进程演示,不要求生产级多 daemon。执行后 SHALL 按 exit code 置 `done`(0)或 `failed`(非 0)。

#### Scenario: 认领并完成
- **WHEN** 一个派给本 agent 的 `todo` issue 存在,daemon 轮询命中
- **THEN** CAS 置 `in_progress` 成功 → 执行 runner → exit 0 → 置 `done`

#### Scenario: 双 daemon 抢同一 issue
- **WHEN** 两个 daemon 实例同时命中同一 `todo` issue
- **THEN** 只有一个 CAS 成功置 `in_progress`,另一个 CAS 失败并跳过,不会重复执行

#### Scenario: 执行失败标 failed
- **WHEN** runner exit code 非 0
- **THEN** issue 置 `failed`,`error` 字段记录 stderr/错误摘要

### Requirement: AgentRunner 抽象与真实 CLI 执行

系统 SHALL 定义 `AgentRunner` Protocol:`run(issue, context, on_event) -> RunResult{status, output, exit_code}`。SHALL 提供 `StubRunner`(发合成事件、无子进程)与 `ClaudeCliRunner`(spawn `claude -p --output-format stream-json "<prompt>"`,逐行解析 stream-json 事件经 `on_event` 转发,exit code 决定 done/failed)。系统 SHALL NOT 用 Anthropic API 调用假装 agent 执行;模型连接 SHALL 由被 spawn 的真实 CLI 自身完成。`claude -p --output-format stream-json` 事件映射:`assistant` 的 content_block_delta → progress/token;`result` subtype=success → done;`result` subtype=error 或 exit≠0 → fail。

#### Scenario: stub runner 验证认领机制
- **WHEN** s02 用 `StubRunner` 跑一个派给 agent 的 issue
- **THEN** 发出合成 progress + done 事件,issue 置 `done`,全程无子进程、无模型调用

#### Scenario: claude runner 真实执行
- **WHEN** s03 起 `ClaudeCliRunner` 跑一个 issue,本机 `claude` 已装并登录
- **THEN** spawn `claude -p --output-format stream-json`,stdout 的 JSON 事件经 `on_event` 转发,exit 0 时 issue 置 `done`、output 含最终结果

#### Scenario: CLI 缺失报错不派活
- **WHEN** `claude` 不在 PATH,daemon 试图用 `ClaudeCliRunner` 执行
- **THEN** issue 置 `failed`,`error` 提示 CLI 未找到,不静默跳过

### Requirement: JSONL 持久化与会话重放

系统 SHALL 用 JSONL 追加写持久化 issue(`workspace/issues.jsonl`)与 agent 会话事件流(`workspace/sessions/<issue_id>.jsonl`,事件为 claim/progress/token/done/fail)。重启 SHALL 重放重建 issue 状态与 in_progress 会话。会话事件过多时 SHALL 对旧事件做 summarize 以控制上下文。

#### Scenario: 写后重启状态恢复
- **WHEN** 一个 issue 正在 `in_progress` 时进程退出,重启后加载 JSONL
- **THEN** issue 恢复为 `in_progress`,会话事件流完整重放

#### Scenario: 溢出 summarize
- **WHEN** 某会话事件数超过阈值(默认 100 事件,可配置)
- **THEN** 旧事件被 summarize 为摘要,近事件保留原文,上下文长度受控

### Requirement: WebSocket 实时进度

系统 SHALL 在 daemon 侧维护 `RealtimeHub`(issue→订阅者集合),`on_event` 经 `publish(issue_id, event)` 推 JSON 事件给所有 WS 订阅者。SHALL 在 `MULTICA_WS_PORT` 提供 WS server。终端客户端 `issue watch <id>` SHALL 连 WS 订阅并实时打印事件。

#### Scenario: 订阅后实时收到进度
- **WHEN** 客户端 `issue watch <id>` 连上 WS,daemon 正在执行该 issue
- **THEN** 客户端实时收到 progress/token/done 事件并打印,无需轮询

#### Scenario: 多订阅者广播
- **WHEN** 同一 issue 有 2 个 WS 订阅者
- **THEN** 每个事件被推给两者,互不影响

### Requirement: FastAPI 单页看板

系统 SHALL 用 FastAPI 提供:`GET /` 返回单页 HTML(内联 JS、无构建步骤)、`GET /api/issues` 列 issue、`WS /ws/issues/{id}` 复用 RealtimeHub 推实时进度。SHALL 在 `MULTICA_WEB_PORT` 起服务。页面 SHALL 列出 issue 行并就地实时更新选中行进度。

#### Scenario: 看板实时更新
- **WHEN** 浏览器打开看板并选中一个正在执行的 issue
- **THEN** 该行进度随 WS 事件实时更新,无需手动刷新

### Requirement: Markdown 技能注入

系统 SHALL 把 `workspace/skills/` 下带 frontmatter(name/description)的 markdown 视为技能。`SkillLoader` SHALL 扫描技能目录,按 issue 标签/匹配规则选技能,经 `claude --append-system-prompt "<skill>"` 注入 CLI 调用。换技能文件 SHALL 不改代码即生效。

#### Scenario: 选中技能注入
- **WHEN** issue 命中某技能匹配规则,`SkillLoader` 选中它
- **THEN** `ClaudeCliRunner` 调用含 `--append-system-prompt` 注入该技能正文

#### Scenario: 无匹配技能照常执行
- **WHEN** issue 不命中任何技能
- **THEN** runner 不带 `--append-system-prompt`,照常执行

### Requirement: Cron Autopilot

系统 SHALL 用 croniter 解析 `workspace/autopilots.jsonl` 中每条作业的 cron 表达式,算 next_run,每 tick 检查到期则 `IssueStore.create(...)` 并指派给作业指定的 agent,进入正常认领流程。连续多次执行失败(默认 5 次,可配置)SHALL 自动禁用该作业。

#### Scenario: cron 到期建 issue
- **WHEN** 作业 cron=`0 9 * * *`,时刻跨过 9:00
- **THEN** 系统创建一条派给指定 agent 的 issue,daemon 认领执行,next_run 推进到次日 9:00

#### Scenario: 连续失败禁用
- **WHEN** 某作业连续 5 次(默认,可配置)到期后执行失败
- **THEN** 该作业被自动禁用,不再到期建 issue

### Requirement: 多 CLI 与 PATH 探测

系统 SHALL 用 `PathDetector` 探测 PATH 上 `claude`/`codex`/… 等可用 CLI,`Runtime` 记录 daemon 所在机器与其可用 CLI 列表。`MultiCliRunner` SHALL 按 `agent.cli` 字段分派到对应 runner;agent 所需 CLI 缺失时 SHALL 报错不派活,不静默跳过。

#### Scenario: PATH 探测可用 CLI
- **WHEN** `PathDetector` 在 PATH 上找到 `claude` 与 `codex`
- **THEN** `Runtime.clis` 含两者,可创建对应 cli 的 agent

#### Scenario: 所需 CLI 缺失
- **WHEN** agent 的 `cli=codex` 但 PATH 无 `codex`,该 agent 被派 issue
- **THEN** 认领后执行报 `failed`,`error` 提示 codex 未找到

### Requirement: 多 Workspace 隔离

系统 SHALL 用 `WorkspaceScope` 让每条 `IssueStore` 查询带 `workspace_id` 过滤;CLI/web SHALL 用 `X-Workspace-ID`(或 `--workspace`)选择当前 workspace。跨 workspace 的 issue SHALL NOT 互相串扰。

#### Scenario: 跨空间不串
- **WHEN** workspace A 与 B 各有一条 issue,当前选 B
- **THEN** `issue list` 只返回 B 的 issue,不显示 A 的

#### Scenario: 切换 workspace
- **WHEN** 从 B 切到 A
- **THEN** 后续查询只返回 A 的 issue,B 的数据不变

### Requirement: 配置与工作区

系统 SHALL 从 `.env` 读取 `MULTICA_DEFAULT_CLI`、`MULTICA_POLL_INTERVAL`、`MULTICA_DATA_DIR`、`MULTICA_WS_PORT`、`MULTICA_WEB_PORT`、`MULTICA_AGENT_ID`。工作区 SHALL 位于 `MULTICA_DATA_DIR`(默认 `./workspace`),含 `issues.jsonl`、`sessions/`、`skills/`、`workspaces.jsonl`、`autopilots.jsonl`。

#### Scenario: 无默认 CLI 仍可跑前置章节
- **WHEN** 未设 `MULTICA_DEFAULT_CLI`,跑 s01/s02
- **THEN** 看板与认领(stub)照常工作,不要求 `claude` 在场

#### Scenario: s03+ 缺 CLI 拒执行
- **WHEN** s03+ 运行而 `claude` 未装/未登录
- **THEN** daemon 报错提示前置,不进入执行
