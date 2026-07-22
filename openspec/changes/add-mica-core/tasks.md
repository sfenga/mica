# Tasks: add-mica-core

> 实现清单。apply 阶段照此勾选。每章一个 `.py` + `.md`,前置代码原样保留。章数弹性,过薄则并、过厚则拆。

## 0. 仓库骨架

- [ ] 0.1 `mica/README.md`(项目说明 + 章节索引 + 快速开始)
- [ ] 0.2 `mica/.env.example`(`MULTICA_DEFAULT_CLI`/`POLL_INTERVAL`/`DATA_DIR`/`WS_PORT`/`WEB_PORT`/`AGENT_ID`;顶部注释:`MULTICA_*` 前缀贴生产命名,mica 不依赖 multica CLI)
- [ ] 0.3 `mica/requirements.txt`(python-dotenv/websockets/croniter/fastapi/uvicorn/httpx)
- [ ] 0.4 `mica/.gitignore`(`.venv/`、`workspace/`、`__pycache__/`)
- [ ] 0.5 `mica/tests/` 目录占位

## 1. s01 — Issue 模型 + 看板 + 多态 assignee

- [ ] 1.1 `Issue`/`Agent`/`Member`/`Workspace` dataclass;status 枚举 `{todo,in_progress,done,failed}`;`assignee_type ∈ {member,agent}`
- [ ] 1.2 `IssueStore`(内存):create/list/get/assign + 合法状态转移校验(非法转移拒)
- [ ] 1.3 CLI:`issue create/list/assign/show`,终端打印看板
- [ ] 1.4 `s01_issue_board.md`(概念 + 试一试 + 对应生产代码指针)
- [ ] 1.5 `tests/test_issue_state.py`(状态机合法性)

## 2. s02 — Daemon 认领执行循环(stub)

- [ ] 2.1 `AgentRunner` Protocol:`run(issue, ctx, on_event) -> RunResult`;`RunResult{status,output,exit_code}`
- [ ] 2.2 `StubRunner`:发合成 progress + done,无子进程
- [ ] 2.3 `Daemon`:轮询 `assignee==me && status==todo` → CAS 认领(todo→in_progress)→ run → 标终态
- [ ] 2.4 `main()`:建 store + agent + daemon,演示一个 issue 被认领执行到 done
- [ ] 2.5 `s02_daemon_claim.md`;`tests/test_claim_cas.py`(双 daemon 抢同一 issue 只一个成功)

## 3. s03 — `claude` CLI runner

- [ ] 3.1 `ClaudeCliRunner`:`subprocess` spawn `claude -p --output-format stream-json "<prompt>"`
- [ ] 3.2 逐行解析 stream-json 事件,经 `on_event` 转发(token/progress/done/fail)
- [ ] 3.3 exit code 0→done、非 0→failed;stderr 落 issue
- [ ] 3.4 daemon 默认 runner 切换为 `ClaudeCliRunner`;`claude` 不在 PATH 时报错提示
- [ ] 3.5 `s03_claude_runner.md`(前置:本机 `claude` 已装并登录)

## 4. s04 — JSONL 持久化 + 会话重放

- [ ] 4.1 `IssueStore` 改/增 JSONL 后端:`workspace/issues.jsonl` 追加写、读取重放
- [ ] 4.2 `SessionStore`:`workspace/sessions/<issue_id>.jsonl` 存事件流(claim/progress/token/done/fail)
- [ ] 4.3 重启重放:加载 issue 与会话,重建 in_progress 状态
- [ ] 4.4 上下文溢出处理:事件过多时 summarize 旧事件(贴 claw0 s03 思路)
- [ ] 4.5 `s04_sessions.md`;`tests/test_persistence.py`(写 → 重启 → 状态恢复)

## 5. s05 — WebSocket 实时

- [ ] 5.1 `RealtimeHub`:issue→订阅者集合;`publish(issue_id, event)` 推给所有订阅者
- [ ] 5.2 daemon `on_event` 调 hub.publish;WS server 在 `MULTICA_WS_PORT`
- [ ] 5.3 终端客户端 `issue watch <id>`:连 WS、订阅、实时打印事件
- [ ] 5.4 `s05_realtime.md`;对应生产 `server/internal/realtime`

## 6. s06 — FastAPI 单页看板

- [ ] 6.1 FastAPI app:`GET /` 返回单页 HTML(内联 JS,无构建)
- [ ] 6.2 `GET /api/issues` 列 issue;`WS /ws/issues/{id}` 推实时进度(复用 RealtimeHub)
- [ ] 6.3 页面:issue 行表 + 选中行实时进度流;`uvicorn` 起 `MULTICA_WEB_PORT`
- [ ] 6.4 `s06_web_panel.md`

## 7. s07 — markdown 技能

- [ ] 7.1 `Skill`:带 frontmatter(name/description)的磁盘 markdown
- [ ] 7.2 `SkillLoader`:扫 `workspace/skills/`,按 issue 标签/匹配规则选技能
- [ ] 7.3 `ClaudeCliRunner` 经 `--append-system-prompt "<skill>"` 注入选中技能
- [ ] 7.4 示例 skill `workspace/skills/code-review/SKILL.md`
- [ ] 7.5 `s07_skills.md`;对应生产 `server/internal/service/builtin_skills`

## 8. s08 — Autopilot(cron)

- [ ] 8.1 `Scheduler`:croniter 解析 cron 表达式,算 next_run,每 tick 检查到期
- [ ] 8.2 到期:`IssueStore.create(...)` 并指派给 agent,进入正常认领流程
- [ ] 8.3 配置来源:`workspace/autopilots.jsonl`(id/cron/agent_id/title/template)
- [ ] 8.4 连续多次失败自动禁用该作业
- [ ] 8.5 `s08_autopilot.md`

## 9. s09 — 多 CLI + PATH 探测

- [ ] 9.1 `PathDetector`:探测 PATH 上 `claude`/`codex`/…,返回可用 CLI 列表
- [ ] 9.2 `MultiCliRunner`:按 `agent.cli` 字段分派到对应 runner;缺失 CLI 报错不派活
- [ ] 9.3 `Runtime` 概念:daemon 所在机器 + 可用 CLI 列表
- [ ] 9.4 `s09_multicli.md`;`tests/test_path_detect.py`(假 CLI 名/缺失处理)

## 10. s10 — 多 workspace 隔离

- [ ] 10.1 `WorkspaceScope`:每条 `IssueStore` 查询带 `workspace_id` 过滤
- [ ] 10.2 CLI/web 用 `X-Workspace-ID`(或 `--workspace`)选空间;切换不影响其它空间
- [ ] 10.3 `workspace/workspaces.jsonl` 存 workspace 注册表
- [ ] 10.4 `s10_workspaces.md`;`tests/test_workspace_isolation.py`(跨空间不串)

## 11. s11(可选附录)— Squads

- [ ] 11.1 `SquadRouter`:leader agent 把 issue 委派给成员 agent
- [ ] 11.2 派给 squad 的 issue 由 leader 选成员 → 子 issue 派活
- [ ] 11.3 `s11_squads.md`(标注可选、非主线)

## 12. 文档与全局验证

- [ ] 12.1 `docs/explain/architecture-overview.md`、`issue-lifecycle.md`、`daemon-runtime.md`、`realtime.md`、`agent-runner.md`
- [ ] 12.2 `README.md` 章节索引与"前置:`claude` 已装"说明
- [ ] 12.3 全章 `py_compile` 通过
- [ ] 12.4 `tests/` 全绿
- [ ] 12.5 `openspec validate add-mica-core` 通过
- [ ] 12.6 在线验证(需 `claude`):派 issue → 认领 → 流式 → 落盘 → 看板实时 → 完成
