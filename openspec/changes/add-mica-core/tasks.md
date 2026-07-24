# Tasks: add-mica-core

> 实现清单。apply 阶段照此勾选。每章一个 `.py` + `.md`,前置代码原样保留。章数弹性。

## 0. 仓库骨架
- [ ] 0.1 `README.md`(项目说明 + 章节索引 + 快速开始)
- [ ] 0.2 `.env.example`(`MULTICA_DEFAULT_CLI`/`POLL_INTERVAL`/`DATA_DIR`/`WS_PORT`/`WEB_PORT`/`AGENT_ID`)
- [ ] 0.3 `requirements.txt`(python-dotenv/websockets/croniter/fastapi/uvicorn/httpx/pytest)
- [ ] 0.4 `.gitignore`(`.venv/`、`workspace/`、`__pycache__/`)
- [ ] 0.5 `conftest.py` + `sessions/__init__.py` + `sessions/zh/__init__.py`(tests 导入用)

## 1. s01 — Issue 模型 + 7 态状态机 + 多态 assignee + 看板
- [x] 1.1 `Issue`/`Agent`/`Member`/`Workspace` dataclass;`IssueStatus ∈ {backlog, todo, in_progress, in_review, done, blocked, cancelled}`;`assignee_type ∈ {member, agent}`
- [x] 1.2 `IssueStore`(内存):create/list/get/assign/transition + 状态机校验(仅允许 backlog→todo→in_progress→in_review|done|blocked;in_review→done|in_progress|blocked;blocked→todo|cancelled)
- [x] 1.3 CLI:`issue create/list/assign/show`,终端看板
- [x] 1.4 `s01_issue_board.md`
- [x] 1.5 `tests/test_issue_state.py`(合法/非法转移、review 流转)

## 2. s02 — Task 实体 + Daemon 认领 task + StubRunner
- [x] 2.1 `Task` dataclass:`id, issue_id, assignee_type, assignee_id, status, session_id?, trigger_comment_id?, rerun_of_task_id?, prior_session_id?, created_at, claimed_at, completed_at, error?`;`TaskStatus ∈ {queued, dispatched, running, completed, failed, cancelled}`
- [x] 2.2 `TaskStore`:enqueue(for issue,assignee 取自 issue)/list/get/claim(CAS queued→dispatched)/start(dispatched→running)/complete/failed;1 issue : N tasks,旧 task 保留
- [x] 2.3 `AgentRunner` Protocol:`run(task, issue, context, on_event, resume_session_id=None) -> RunResult{status, output, exit_code, session_id}`;`StubRunner`(合成事件,无子进程)
- [x] 2.4 `Daemon`:轮询 TaskStore(assignee==me && queued)→ CAS claim → start → run → complete/failed;task 生命周期驱动 issue 状态(running→in_progress, completed→in_review, failed→blocked+error)
- [x] 2.5 `main()`:建 store+agent+daemon,演示一条 issue 入队 task 被认领执行到 completed、issue in_review
- [x] 2.6 `s02_task_daemon.md`;`tests/test_task_claim.py`(双 daemon 抢同一 task 只一个赢;task 驱动 issue 状态)

## 3. s03 — `claude` CLI runner on task
- [x] 3.1 `parse_stream_line`(纯函数):system/assistant(text/thinking/tool_use)/user(tool_result)/result(success/error+session_id)/log;control_request 忽略;thinking_tokens/hook 子类型丢弃
- [x] 3.2 `ClaudeCliRunner`:`_cmd` = `[cli, "-p", "--verbose", "--output-format", "stream-json", prompt, ...]`;有 `resume_session_id` 加 `--resume`;SHALL NOT 含 `--no-session-persistence`
- [x] 3.3 `run`:Popen 流式逐行 → on_event 转发;捕获 session_id(system init + result);fail-closed(无 result→failed, is_error→failed, exit≠0→failed);`FileNotFoundError`→failed(CLI not found)
- [x] 3.4 daemon 默认 runner 切换为 `ClaudeCliRunner`
- [x] 3.5 `s03_claude_runner.md`(前置:`claude` 已装并登录)
- [x] 3.6 `tests/test_stream_parse.py` + `tests/test_cli_runner_finalize.py`(canned stream + monkeypatched Popen)

## 4. s04 — JSONL 持久化(issues + tasks + sessions)+ orphan + 截断 summarize
- [x] 4.1 `IssueStore`/`TaskStore` 加 `data_dir`:`issues.jsonl`/`tasks.jsonl` 快照追加 + last-wins 重放
- [x] 4.2 `SessionStore`:`sessions/<task_id>.jsonl` 事件流(claim/progress/done/fail);append/load
- [x] 4.3 重启重放:加载 issue+task,重建状态;`running`/`dispatched` 无 runner 的 task → `failed`+error(orphan 恢复,不重派)
- [x] 4.4 溢出 summarize:截断式(留近 N 条 + 1 条 `{type:summary,dropped_count,range}`),不调模型;原子重写
- [x] 4.5 daemon 落盘事件到 SessionStore;`data_dir=None` 退化为内存(s01–s03 测试不受影响)
- [x] 4.6 `s04_persistence.md`;`tests/test_persistence.py` + `tests/test_orphan_recovery.py`

## 5. s05 — 评论建新 task + `--resume` 上一 task.session_id
- [ ] 5.1 `IssueStore.comment(issue_id, text)`:追加评论记录;**不**改 issue 状态;为 issue assignee 入队新 `queued` task(`trigger_comment_id` 设)
- [ ] 5.2 daemon 认领 task 时查同 (issue,agent) 上一 task.session_id → `prior_session_id`;`ClaudeCliRunner.run(resume_session_id=...)` 加 `--resume`;无上一 task 则全新
- [ ] 5.3 resume 失效兜底:claude 吐回新 session_id ≠ 传入 → 覆盖本 task.session_id,不沿用失效 id
- [ ] 5.4 手动重跑:`rerun_task(issue_id)` 建新 task(`rerun_of_task_id` 指向源 task),不在原 task 上续
- [ ] 5.5 `s05_comment_resume.md`;`tests/test_comment_resume.py`(评论入队新 task+issue 不变、--resume 上一 task、resume 失效降级、不传 no-session-persistence)

## 6. s06 — WebSocket 实时
- [ ] 6.1 `RealtimeHub`:task/issue→订阅者集合;`publish(id, event)` 推给所有订阅者
- [ ] 6.2 daemon `on_event` 调 hub.publish;WS server 在 `MULTICA_WS_PORT`
- [ ] 6.3 终端客户端 `task watch <id>`:连 WS、订阅、实时打印
- [ ] 6.4 晚到订阅者不获历史回放(历史走 s04 JSONL,daemon 不缓冲);`tests/test_realtime.py`
- [ ] 6.5 `s06_realtime.md`

## 7. s07 — FastAPI 单页看板
- [ ] 7.1 `GET /`(单页 HTML,内联 JS,无构建);`GET /api/issues` 列 issue
- [ ] 7.2 `POST /api/issues` 创建+入队 task;`PATCH /api/issues/{id}/status` 状态机校验转移(复用 s01);`POST /api/issues/{id}/comments` 评论入队 follow-up task;`WS /ws/issues/{id}` 复用 RealtimeHub
- [ ] 7.3 页面:issue 行表 + 创建表单 + 行内状态转移 + 评论框 + 选中行实时进度;`uvicorn` 起 `MULTICA_WEB_PORT`
- [ ] 7.4 `tests/test_web_panel.py`(创建/状态转移/评论各合法与非法路径)
- [ ] 7.5 `s07_web_panel.md`

## 8. s08 — markdown 技能
- [ ] 8.1 `Skill`:带 frontmatter(`name`/`description`/`match` 正则)的磁盘 markdown
- [ ] 8.2 `SkillLoader`:扫 `workspace/skills/**/SKILL.md`,用 `match` 测 issue.title+body,取首个命中;无 `match` 不自动选中
- [ ] 8.3 `ClaudeCliRunner` 经 `--append-system-prompt "<skill 正文>"` 注入选中技能
- [ ] 8.4 示例 skill `workspace/skills/code-review/SKILL.md`(带 `match`)
- [ ] 8.5 `tests/test_skill_loader.py`;`s08_skills.md`

## 9. s09 — Autopilot(cron)
- [ ] 9.1 `Scheduler`:croniter 解析 cron,算 next_run,每 tick 检查到期
- [ ] 9.2 到期:建 issue(指派 agent)+ 入队 task,进正常认领
- [ ] 9.3 配置 `workspace/autopilots.jsonl`(字段 `id`/`cron`/`agent_id`/`title`/`template`/`next_run`/`fail_count`/`enabled`/`timezone`)
- [ ] 9.4 失败语义:到期后无论成败推进 next_run 并落盘;成功清零 fail_count;连续 5 次失败置 enabled=false;禁用后手动改 enabled=true 恢复;cron 默认 UTC 可配 timezone;重启不重触发已过槽位
- [ ] 9.5 `tests/test_autopilot.py`;`s09_autopilot.md`

## 10. s10 — 多 CLI + PATH 探测(1 daemon : N agent)
- [ ] 10.1 `PathDetector`:探测 PATH 上 `claude`/`codex`/…,返回可用 CLI 列表
- [ ] 10.2 `MultiCliRunner`:按 `agent.cli` 分派到对应 runner;缺失 CLI 报错不派活
- [ ] 10.3 `Runtime` 实体:daemon 机器 + 可用 CLI 列表;`Agent.runtime_id` 指向 runtime
- [ ] 10.4 重构 `Daemon` 为 1:N:持 `runtime_id`;`claim` 过滤改为「task 对应 agent 的 `runtime_id == self.runtime_id`」;认领后按 agent.cli 经 MultiCliRunner 分派
- [ ] 10.5 `tests/test_path_detect.py` + `tests/test_multidaemon.py`(一 daemon 多 agent+按 cli 分派;同 runtime 双 daemon 抢同一 task 只一个赢;跨 runtime 不串);`s10_multicli.md`

## 11. s11 — 多 workspace 隔离
- [ ] 11.1 `WorkspaceScope`:每条 issue/task 查询带 `workspace_id` 过滤 + 成员资格闸门(当前 member 须属该空间,非成员拒)
- [ ] 11.2 CLI/web 用 `X-Workspace-ID`/`--workspace` 选空间 + `X-Member-ID`/`MULTICA_MEMBER_ID` 标识 member
- [ ] 11.3 `workspace/workspaces.jsonl`(注册表)+ `workspace/members.jsonl`(每空间 member 列表)
- [ ] 11.4 `tests/test_workspace_isolation.py` + `tests/test_membership_gate.py`;`s11_workspaces.md`

## 12. s12(可选附录)— Squads
- [ ] 12.1 `SquadRouter`:leader agent 把 issue 委派给成员 agent
- [ ] 12.2 派给 squad 的 issue 由 leader 选成员 → 子 issue/task 派活
- [ ] 12.3 `s12_squads.md`(标注可选、非主线)

## 13. 文档与全局验证
- [ ] 13.1 `docs/explain/`(架构总览、issue/task 生命周期、daemon、realtime、agent-runner)
- [ ] 13.2 `README.md` 章节索引与"前置:`claude` 已装"说明
- [ ] 13.3 全章 `py_compile` 通过
- [ ] 13.4 `tests/` 全绿
- [ ] 13.5 `openspec validate add-mica-core` 通过
- [ ] 13.6 在线验证(需 `claude`):建 issue+task → 认领 → 流式 → 落盘 → issue in_review → 评论 → follow-up task → resume → 完成
