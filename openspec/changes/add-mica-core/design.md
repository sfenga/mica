# Design: add-mica-core

## 目标与约束

- **渐进自包含**:每章 `sessions/zh/sNN_xxx.py` 一个文件、可直接 `python sessions/zh/sNN.py` 运行,不跨章 import,复现各章核心算法。
- **一概念一章**:每章只引入一个新概念,前置代码原样保留;章数弹性,过薄则并、过厚则拆。
- **真实执行**:s03 起 agent 执行为真实 `claude` CLI 调真实模型;不用 Anthropic API 假装 agent。
- **零依赖直接跑**:JSONL + 内存持久化,不引 PostgreSQL;s02 stub 使认领循环单测不花钱、不依赖 CLI。

## 架构:以"认领执行循环"为脊柱,各层挂载

```
CLI/Web 建 issue,assignee = 某 agent
   │
Daemon 轮询 IssueStore: assignee==me && status==todo
   │ 原子认领: todo -> in_progress  (CAS,防多 daemon 抢同一 issue)
   ▼
AgentRunner.run(issue, ctx, on_event)
   ├─ StubRunner(s02): 合成 progress + done,无子进程
   └─ ClaudeCliRunner(s03+): spawn `claude -p --output-format stream-json "<prompt>"`
        stdout 行 = JSON 事件 → on_event → 同时追加进 SessionStore(s04)
   │
   ├─ on_event → RealtimeHub(s05) → WS 客户端 + Web 看板(s06)
   ▼
exit 0  → status=done,   结果作为事件落 issue 会话
exit≠0  → status=failed, stderr/error 落 issue 会话
```

## 数据模型

```
Issue       : id, workspace_id, title, body, status, assignee_type, assignee_id,
              created_at, updated_at, error?
              status ∈ {todo, in_progress, done, failed}
              assignee_type ∈ {member, agent}      # 多态 assignee
Agent       : id, name, cli("claude"), runtime_id
Member      : id, name
Runtime     : id, name, clis[]
Workspace   : id, slug, name
Session     : issue_id -> JSONL of events(claim/progress/token/done/fail)   # s04 起
Skill       : 磁盘 markdown,带 frontmatter(name/description),注入 CLI 调用
Autopilot   : id, cron, agent_id, title, template, enabled, fail_count?    # s08
```

## 关键设计决策

### D1: AgentRunner 抽象 + 真实 CLI(不用 API 假装 agent)
multiCa 的本质是**管理** coding agent CLI,不是自己当 agent。`AgentRunner` 是 `run(issue, ctx, on_event) -> RunResult` 的 Protocol;`StubRunner`(s02)只验证认领机制;`ClaudeCliRunner`(s03)`subprocess` spawn `claude -p --output-format stream-json`,逐行解析 stream-json 事件经 `on_event` 转发,exit code 决定 done/failed。模型连接由 `claude` 自身完成,故 mica 不引 `anthropic` SDK 当执行器。事件映射:`claude -p --output-format stream-json` 的 `assistant` content_block_delta → progress/token;`result` subtype=success → done;`result` subtype=error 或 exit≠0 → fail。

### D2: s02 stub 隔离认领机制
认领(轮询→CAS→标终态)是 multiCa daemon 的命脉,值得单独一章验证。s02 用 `StubRunner` echo 出合成事件,使该章及单测不依赖 CLI、不消耗真实模型调用。s03 起 stub 让位给真实 CLI。

### D3: 多态 assignee 从 s01 立住
`assignee_type` + `assignee_id` 可指 member 或 agent —— 这是 multiCa "agents as teammates" 的核心约束。s01 即建立,后续所有认领/展示逻辑都基于此多态。

### D4: 持久化用 JSONL,生产对应 PostgreSQL
JSONL 追加写、读取重放(贴 claw0 s03 风格),零依赖直接跑。issue 状态与 agent 会话事件流同存为 JSONL;重启可重放重建。与生产 multiCa 的 PostgreSQL 取舍见 spec 表。CAS 认领在 JSONL 下用"单写者 + 文件锁"保证;教学版可先单 daemon,但 API 形态保留 CAS 以对应生产。"双 daemon 抢同一 issue"为 CAS 语义验证,可用并发模拟或双进程演示,不要求生产级多 daemon。

### D5: 实时走 WebSocket,不轮询拉取
daemon 侧 `RealtimeHub`(s05)维护 issue→订阅者集合,`on_event` 推 JSON 事件给 WS 客户端(终端 + s06 web 看板)。终端客户端 `wscat`/脚本订阅某 issue 即可看流式进度,与生产 `gorilla/websocket` 对应。

### D6: web 面板零构建
s06 用 FastAPI 服务单页 HTML(内联 JS,无构建步骤)+ 一个 WS 端点;页面列 issue 行,WS 收到 progress 事件就地更新对应行。教学聚焦后端机制,前端只够展示"板子+实时"。

### D7: 技能 = markdown 注入 CLI 调用
`SkillLoader`(s07)扫 `workspace/skills/` 下带 frontmatter 的 markdown,按 issue 标签/匹配规则选技能,经 `claude --append-system-prompt "<skill>"` 注入。技能是磁盘文件,换文件换能力、不改代码 —— 对应 multiCa "reusable skills"。

### D8: autopilot = cron 建 issue + 路由
`Scheduler`(s08)用 croniter 解析 cron 表达式,到期则 `IssueStore.create(...)` 并指派给某 agent,issue 进入正常认领流程。对应 multiCa autopilots。

### D9: 多 CLI + PATH 探测统一 runtime
`PathDetector`(s09)探测 PATH 上 `claude`/`codex`/…,`MultiCliRunner` 按 `agent.cli` 字段分派;缺失 CLI 的 agent 不被派活并报错。对应 multiCa unified runtimes。

### D10: workspace 隔离从 store 层贯彻
`WorkspaceScope`(s10)让每条 `IssueStore` 查询带 `workspace_id` 过滤,CLI/web 用 `X-Workspace-ID` 选空间。对应 multiCa 多 workspace 隔离。

## REPL / CLI 命令(各章渐进)

终端 CLI 按 chapter 渐进:`issue create/list/assign`、`issue watch <id>`(s05 起)、`ws list/switch`(s10 起)。每章 `main()` demo 展示本章新增命令。

## 非目标(明确不做)

- squads 作主线(列为可选附录 s11,非必须)。
- 计费 / analytics / feature flags。
- 多 runtime(云端实例)—— 仅本地 daemon。
- iOS / desktop / docs 站。
- 认证轮换、3 层重试洋葱等 claw0 后期生产化主题。
- 死信人工干预 UI、富媒体合并等。

## 验证策略

1. 每章 `py_compile` 通过。
2. `tests/` 纯逻辑:issue 状态机合法性(非法转移被拒)、CAS 认领原子性(双 daemon 抢同一 issue 只一个成功)、PATH 探测(假 CLI 名/缺失处理)、workspace 过滤(跨空间不串)。
3. s03+ 在线(需本机 `claude` 已装并登录):`issue create` 派给 agent → daemon 认领 → spawn `claude` → 流式回传 → 落盘 → 终端/看板实时更新 → 完成。
4. `openspec validate add-mica-core` 通过。

## 与生产 multiCa 取舍

| 生产 multiCa | mica | 理由 |
|---|---|---|
| Go (Chi + sqlc + gorilla/ws) | Python | 教学门槛,贴 claw0 |
| PostgreSQL 17 + pgvector | JSONL | 零依赖直接跑 |
| Next.js 全功能前端 | 一节 FastAPI 单页 | 教学聚焦后端机制 |
| DB 外键/级联禁用,应用层处理 | 同样应用层处理 | 贴合生产约定 |
| 多态 assignee(member/agent) | 保留 | 核心 |
| workspace 隔离 + 成员资格 | 保留(workspace 过滤) | 核心 |
| skills 复用 | 保留(markdown 注入) | 核心 |

> env 变量用 `MULTICA_` 前缀贴生产 multiCa 命名;mica 本身不依赖 multica CLI,无需先安装它。
