# Design: add-mica-core

## 目标与约束

- 每章 `sessions/zh/sNN_xxx.py` 一个文件、可直接 `python sessions/zh/sNN.py` 运行,不跨章 import,复现各章核心算法。
- 每章只引入一个新概念,前置代码原样保留;章数弹性。
- s03 起 agent 执行为真实 `claude` CLI 调真实模型;不用 Anthropic API 假装 agent。
- JSONL + 内存持久化,不引 PostgreSQL;s02 stub 使认领循环单测不花钱、不依赖 CLI。

## 核心模型:issue / task 分层(贴生产 multiCa)

issue 是看板项(用户产物,状态反映整体生命周期);task 是一次 agent 执行尝试(`agent_task_queue` 一行,有自己的 status/session_id)。**1 issue : N tasks**,旧 task 行保留作历史。评论不改 issue 状态,而是入队一条新 task——这是与「issue 即执行单元」模型的关键区别,避免评论把刚标完成的卡拨回进行中。

```
Issue   : id, workspace_id, title, body, status, assignee_type, assignee_id,
          created_at, updated_at, error?
          status ∈ {backlog, todo, in_progress, in_review, done, blocked, cancelled}
          assignee_type ∈ {member, agent}      # 多态 assignee
Task    : id, issue_id, assignee_type, assignee_id, status, session_id?,
          trigger_comment_id?, rerun_of_task_id?, prior_session_id?,
          created_at, claimed_at, completed_at, error?
          status ∈ {queued, dispatched, running, completed, failed, cancelled}
Agent   : id, name, cli("claude"), runtime_id
Member  : id, name
Runtime : id, name, clis[]
Workspace : id, slug, name
Session : task_id -> JSONL of events(claim/progress/token/done/fail)   # s04 起
Skill     : 磁盘 markdown,带 frontmatter(name/description/keywords 关键词),技能正文拼入 user prompt
Autopilot : id, cron, agent_id, title, template, next_run, fail_count, enabled, timezone?
Membership: workspace_id -> [member_id]
```

### Issue 状态机
`backlog→todo`、`todo→in_progress`、`in_progress→in_review|done|blocked`、`in_review→done|in_progress|blocked`、`blocked→todo|cancelled`;`done`/`cancelled` 终态。`in_review` 是评论高发期(成果待人评审)。

### Task 状态机
`queued→dispatched`(claim CAS)→`running`(start)→`completed|failed`;`→cancelled`。终态 `completed/failed/cancelled`。

### Task 生命周期驱动 issue 状态
task `running` → issue `in_progress`;task `completed` → issue `in_review`(待人评审,不直接 done);task `failed` → issue `blocked`。**评论不驱动 issue 状态**——评论只入队新 task。

## 架构:以"task 认领执行循环"为脊柱

```
CLI/Web 建 issue + 入队 task,或评论入队 follow-up task
   │
Daemon 轮询 TaskStore: assignee==me && status==queued  (s02 单 agent;s10 起 runtime 维度 1:N)
   │ 原子认领: queued -> dispatched  (CAS,防多 daemon 抢同一 task)
   │ start: dispatched -> running;查同(issue,agent)上一 task.session_id -> prior_session_id
   ▼
AgentRunner.run(task, issue, ctx, on_event, resume_session_id=prior_session_id)
   ├─ StubRunner(s02): 合成 progress + done,无子进程
   └─ ClaudeCliRunner(s03+): spawn `claude -p --verbose --output-format stream-json [--resume <id>] "<prompt>"`
        stdout 行 = JSON 事件 → on_event → 追加进 SessionStore(s04);捕获 session_id
   │
   ├─ on_event → RealtimeHub(s06) → WS 客户端 + Web 看板(s07)
   ▼
exit 0  → task completed,  issue -> in_review;  result 落会话
exit≠0  → task failed,     issue -> blocked;    error 落会话
```

## 关键设计决策

### D1: issue/task 分层(贴生产,非 issue 即执行单元)
issue 是看板项(用户产物),task 是执行尝试。评论入队新 task、不改 issue 状态;resume 从上一 task.session_id 续。对应生产 multiCa 的 issue + agent_task_queue 两层。比"issue 即执行单元 + reopen"更合理:看板项不会因评论跳状态,执行历史可审计(一 issue 多 task 行),续会话靠 task 间 session_id 串。

### D2: AgentRunner 抽象 + 真实 CLI
`AgentRunner` Protocol:`run(task, issue, context, on_event, resume_session_id=None) -> RunResult{status,output,exit_code,session_id}`。`StubRunner`(s02)验证认领机制;`ClaudeCliRunner`(s03)`subprocess` spawn `claude -p --verbose --output-format stream-json`,逐行解析 stream-json 事件经 `on_event` 转发,exit code 决定 done/failed;fail-closed(无 result 事件 → failed);从 system/result 捕获 session_id。`_cmd` 默认传 `--permission-mode bypassPermissions`(让 claude 能自动批工具、执行真实编码任务;对应生产 multiCa)。模型连接由 claude 自身完成,不引 `anthropic` SDK。事件映射:`assistant` content_block_delta→progress/token;`tool_use`/`tool_result`→progress(含工具名/状态);`result` subtype=success→done、error→fail。

### D3: s02 stub 隔离认领机制
认领(轮询 task 队列→CAS queued→dispatched→标终态)是命脉,单独一章验证。s02 用 `StubRunner` 发合成事件,使该章及单测不依赖 CLI、不花钱。s03 起换真实 CLI。

### D4: 多态 assignee 从 s01 立住
`assignee_type` + `assignee_id` 可指 member 或 agent——"agents as teammates" 核心。s01 建立,后续认领/入队/展示都基于此。

### D5: 持久化用 JSONL(issues + tasks + sessions)
JSONL 追加写、读取重放,零依赖。issue 状态、task 行、会话事件流各一文件(`issues.jsonl`/`tasks.jsonl`/`sessions/<task_id>.jsonl`)。重启重放重建;orphan(running/dispatched 无 runner 的 task)→ failed + error,不自动重派。会话溢出截断式 summarize(留近 N 条 + 1 条 summary 占位),不调模型。

### D6: 评论 = 入队新 task + resume 上一 task
评论触发新 `queued` task(`trigger_comment_id` 指向评论,`assignee` 取自 issue),issue 状态不动。daemon 认领时查同 (issue,agent) 上一 task 的 `session_id` → `prior_session_id` → `--resume` 续会话;无则全新。resume 失效(吐回新 id)→ 覆盖本 task.session_id。手动重跑建新 task(`rerun_of_task_id`)。对应生产 multiCa「评论入队新 task + claim 时 --resume 上一 task.session_id」(简化:不做的生产三路分流合并/暂存)。

### D7: 实时走 WebSocket(不轮询拉取)
daemon 侧 `RealtimeHub`(s06)维护 task/issue→订阅者,`on_event` 推 JSON 事件给 WS 客户端(终端 + s07 web)。晚到订阅者不回放历史(历史走 `sessions/<task_id>.jsonl`)。

### D8: web 面板零构建
s07 FastAPI 单页 HTML(内联 JS、无构建)+ WS 端点;issue 行表 + 创建表单 + 行内状态转移 + 评论框 + 选中行实时进度。

### D9: 技能 = markdown(frontmatter.keywords 关键词)注入 user prompt
`SkillLoader`(s08)扫 `workspace/skills/**/SKILL.md`,frontmatter `keywords` 关键词子串匹配 issue.title+body,取首个命中,技能正文拼入 user prompt。换文件不改代码即生效。

### D10: autopilot = cron 建 issue + 入队 task
`Scheduler`(s09)croniter 解析 cron,到期建 issue(指派 agent)+ 入队 task,进正常认领。next_run 无论成败推进并落盘;连续失败禁用;重启不重触发。

### D11: 多 CLI + PATH 探测(1 daemon : N agent)
`PathDetector`(s10)探测 PATH 上 CLI,`Runtime` 记机器+CLI 列表,`Agent.runtime_id` 指向 runtime。`MultiCliRunner` 按 `agent.cli` 分派;daemon 从 s02 的 1:1(单 agent)泛化为 1:N(`claim` 按 `agent.runtime_id==本 daemon.runtime_id`)。s02 已交付的 1:1 是 1:N 的单 agent 特例。

### D12: workspace 隔离 = 过滤 + 成员资格闸门
`WorkspaceScope`(s11)每条 issue/task 查询带 `workspace_id` 过滤 + 成员校验(`members.jsonl`);非成员拒。

## REPL / CLI 命令(各章渐进)
`issue create/list/assign/show`、`issue comment <id> <text>`(s05 起)、`task watch <id>`(s06 起)、`ws list/switch`(s11 起)。

## 非目标(明确不做)
- squads 作主线(列为可选附录 s12)。
- 计费 / analytics / feature flags。
- 多 runtime(云端实例)—— 仅本地 daemon。
- iOS / desktop / docs 站。
- 生产三路评论分流(合并进 queued / active 暂存兜底)—— mica 简化为始终新建 queued task。
- 认证轮换、3 层重试洋葱、control_request 自动批、stdin 长开会话等生产化主题。

## 验证策略
1. 每章 `py_compile` 通过。
2. `tests/` 纯逻辑:issue 状态机合法性、task CAS 认领原子性(双 daemon 抢同一 task 只一个赢)、PATH 探测、workspace 成员闸门。
3. s03+ 在线(需 `claude`):派 issue+task → daemon 认领 → spawn claude → 流式 → 落盘 → 完成 → issue in_review。
4. `openspec validate add-mica-core` 通过。

## 与生产 multiCa 取舍
| 生产 multiCa | mica | 理由 |
|---|---|---|
| Go (Chi + sqlc + gorilla/ws) | Python | 门槛低,贴 claw0 |
| PostgreSQL 17 + pgvector | JSONL | 零依赖直接跑 |
| Next.js 全功能前端 | 一节 FastAPI 单页 | 聚焦后端机制 |
| DB 外键/级联禁用,应用层处理 | 同样应用层处理 | 贴合生产约定 |
| issue + agent_task_queue 两层 | 保留(issue + task) | 核心 |
| 评论三路分流(合并/暂存/新建) | 简化为始终新建 queued task | 减并发复杂度 |
| 长开 stdin 会话 + --resume | `claude -p` + `--resume` 每次 | 不做 stdin 长开/control_request |
| 多态 assignee / workspace 成员资格 / skills | 保留 | 核心 |

> env 变量用 `MULTICA_` 前缀贴生产 multiCa 命名;mica 本身不依赖 multica CLI。
