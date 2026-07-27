## Task 1: s13 — Learning Console (Mission Control UI + 25 API endpoints)

# mica Learning Console (s13) — Complete Frontend Design + Implementation Plan

## Context

mica 已完成 s01-s12(12068 行,104 测试)。需要一个**完整的教学控制台**——不是简陋的 MVP,而是每个页面都有实质可视化、交互、内容的前端。基于 s12_squads.py(已含全部代码)。

## Frontend Design

### Aesthetic: "Mission Control"
- **Deep ink background** (#0d1117) + **warm amber accent** (#f0a050) — 仪表盘/CRT 质感
- State colors: green(ok #3fb950) / amber-yellow(warn #d29922) / red(err #f85149) / blue(info #58a6ff)
- Type: sans-serif UI + monospace data — mono/sans 编码"数据 vs 界面"
- 所有 CSS 内联 `<style>`,所有 JS 内联 `<script>`,零构建

### Layout: CSS Grid 全屏
```
┌──────────────────────────────────────────────────────────┐
│ ◆ mica    [ws-1 ▾] [alice ▾] [rt-1 claude] [WS ●]    │  Top Bar (fixed)
├──────────┬───────────────────────┬──────────────────────┤
│          │                       │                      │
│ Overview │  ┌─────────┬────────┐│  ┌────────────────┐ │
│   Issues │  │  List   │ Detail ││  │   Inspector     │ │
│   Tasks  │  │         │        ││  │                 │ │
│   Trace  │  │         │        ││  │  JSON / Timeline│ │
│   Skills │  │         │        ││  │  Related Objects│ │
│   Auto   │  │         │        ││  │  Skill Preview   │ │
│ Runtime  │  └─────────┴────────┘│  └────────────────┘ │
│   Works  │                       │                      │
│  Squads  │  Issue Lifecycle      │                      │
│          │  Timeline (signature) │                      │
├──────────┴───────────────────────┴──────────────────────┤
│ [▶ Run daemon once] [⏰ Autopilot tick] [↺ Reset] [📋 Seed]│  Action Bar
└──────────────────────────────────────────────────────────┘
```

### 10 Page Sections (每个都有实质内容)

#### 1. Overview (Dashboard)
```
┌──────────────┬──────────────┬──────────────┬──────────────┐
│ Issues       │ Tasks        │ Runtimes     │ Autopilots   │
│  3 total     │  2 queued    │  1 active    │  2 enabled   │
│  ○ 1 todo    │  ● 1 running │  claude ✓    │  next: 9:00  │
│  ◐ 1 in_prog │  ✓ 1 done   │              │              │
│  ✓ 1 done    │  ✗ 0 failed  │              │              │
└──────────────┴──────────────┴──────────────┴──────────────┘
┌──────────────────────────────────────────────────────────┐
│ Recent Activity (live feed)                              │
│ 14:32  issue #3 created → task queued                    │
│ 14:33  task claimed → running                             │
│ 14:34  task completed → issue in_review                   │
└──────────────────────────────────────────────────────────┘
```

#### 2. Issue Board
```
┌─────────────────────────────────────────────────────────┐
│ + New Issue  [title____] [assignee: agent ▾] [id____]  │
├─────────┬────────┬──────────┬────────┬────────────────┤
│ ID      │ Status │ Assignee │ Title  │ Tasks/Comments  │
│ 67fa... │ todo   │ agent-1  │ fix... │ 1 task / 0 cm  │
│ ed92... │ review │ agent-1  │ wri... │ 2 tasks / 1 cm │
└─────────┴────────┴──────────┴────────┴────────────────┘
```
- 状态 badge 带颜色(todo=warn, in_progress=warn, in_review=info, done=ok, blocked=err)
- 点击行 → Detail 面板显示 issue 全信息 + tasks + comments + children + timeline
- 创建表单:assignee_type 下拉(member/agent/squad),根据类型决定是否入队 task

#### 3. Task Queue
```
┌─────────┬────────┬──────────┬────────┬────────┬────────┐
│ ID      │ Status │ Issue    │ Agent  │ Session│ Error  │
│ t1...   │ queued │ 67fa...  │ agent-1│        │        │
│ t2...   │ running│ ed92...  │ agent-1│ ses-1  │        │
│ t3...   │ done   │ 67fa...  │ agent-1│ ses-2  │        │
└─────────┴────────┴──────────┴────────┴────────┴────────┘
[▶ Run daemon once] — 执行一轮认领
```
- Status filter tabs: All / Queued / Running / Completed / Failed
- 点击 task → Detail 显示 task 全信息 + session events(可展开)
- "Run daemon once" 按钮 → POST /api/daemon/run-once → 实时刷新

#### 4. Execution Trace
```
┌──────────────────────────────────────────────────────────┐
│ Task: t1...  Issue: 67fa...  Status: running             │
│ session_id: ses-abc-123  prior_session_id: (none)        │
├──────────────────────────────────────────────────────────┤
│ Session Events (SessionStore.load)                        │
│ 14:32:01  {"type":"claim","message":"claimed 'fix...'"}   │
│ 14:32:02  {"type":"progress","kind":"system","sid":"..."}  │
│ 14:32:03  {"type":"progress","kind":"text","text":"..."}   │
│ 14:32:04  {"type":"progress","kind":"tool_use","tool":"B"} │
│ 14:32:05  {"type":"result","status":"done","output":"..."} │
├──────────────────────────────────────────────────────────┤
│ Live WebSocket Stream (if watching)                       │
│ [WS ● connected] events arriving in real-time...           │
└──────────────────────────────────────────────────────────┘
```
- 左半:session events(JSON 行,带时间戳,颜色 by type)
- 右半:task 元信息(session_id, prior_session_id, resume 链)
- "Watch" 按钮 → 连 WS,实时追加事件

#### 5. Skills
```
┌──────────────────────────────────────────────────────────┐
│ Loaded Skills                                             │
│ ┌────────────────────────────────────────────────────────┐│
│ │ name: code-review                                      ││
│ │ description: 审查代码变更                              ││
│ │ keywords: review, 审查, PR, code review                ││
│ │ path: workspace/skills/code-review/SKILL.md            ││
│ │ body preview: "You are reviewing code. Focus on..."   ││
│ └────────────────────────────────────────────────────────┘│
├──────────────────────────────────────────────────────────┤
│ Skill Injection Preview                                  │
│ Issue title: [____review the auth module____]            │
│ Issue body:  [_____________________________________]      │
│                                                           │
│ → Matched: code-review ✓                                 │
│   keyword "review" found in title                        │
│                                                           │
│ Injected prompt preview:                                  │
│ ┌──────────────────────────────────────────────────────┐│
│ │ review the auth module                                ││
│ │                                                      ││
│ │ [Skill]                                              ││
│ │ You are reviewing code. Focus on:                    ││
│ │ 1. Security: injection, auth bypass, data leak       ││
│ │ ...                                                  ││
│ └──────────────────────────────────────────────────────┘│
│                                                           │
│ (no match → "No skill selected — running without skill")  │
└──────────────────────────────────────────────────────────┘
```

#### 6. Autopilot
```
┌──────────┬───────────┬──────────┬──────────┬──────────┬──────┐
│ ID       │ Cron      │ Agent    │ Title    │ Next Run │ Enbl │
│ ap-1     │ 0 9 * * * │ agent-1  │ daily... │ 09:00    │ ✓    │
│ ap-2     │ * * * * * │ agent-2  │ tick...  │ :30      │ ✗    │
└──────────┴───────────┴──────────┴──────────┴──────────┴──────┘
[+ New Autopilot] [⏰ Tick Now]
```
- 新建表单:cron expression, agent_id, title, template
- "Tick Now" → POST /api/autopilots/tick → 显示新建的 issue/task
- 展开:fail_count, enabled toggle, next_run 倒计时

#### 7. Runtimes & Agents
```
┌──────────────────────────────────────────────────────────┐
│ Runtime: rt-1 (local)                                     │
│ Detected CLIs: claude ✓  codex ✓  copilot ✗             │
├──────────────────────────────────────────────────────────┤
│ Agents on this runtime:                                   │
│ ┌──────────┬──────────┬──────────┬──────────────────────┐│
│ │ Agent ID │ Name     │ CLI      │ Tasks (queued/total)  ││
│ │ agent-1  │ Claude   │ claude   │ 1 / 3                 ││
│ │ agent-2  │ Codex    │ codex    │ 0 / 2                 ││
│ └──────────┴──────────┴──────────┴──────────────────────┘│
├──────────────────────────────────────────────────────────┤
│ Claim Diagram                                             │
│ daemon (rt-1) claims: all agent tasks (1:N)              │
│ ┌─────────────┐                                          │
│ │ task queued │ ──CAS──► dispatched ──start──► running   │
│ │  (rt-1)     │   ✓                                      │
│ └─────────────┘                                          │
│ ┌─────────────┐                                          │
│ │ task queued │ ──CAS──► ✗ (already dispatched)         │
│ │  (rt-1)     │                                          │
│ └─────────────┘                                          │
└──────────────────────────────────────────────────────────┘
```

#### 8. Workspaces
```
┌──────────┬──────────┬──────────────┬──────────────────────┐
│ WS ID    │ Slug     │ Members      │ Issues               │
│ ws-1     │ default  │ alice, bob   │ 3 issues             │
│ ws-2     │ team-b   │ charlie      │ 1 issue              │
└──────────┴──────────┴──────────────┴──────────────────────┘
[+ Add Member] [Switch workspace ▾]
```
- 切换 workspace → 切换 member → 演示 membership gate(非成员 → PermissionError)
- 显示当前 member + workspace 在 top bar

#### 9. Squads
```
┌──────────────────────────────────────────────────────────┐
│ Squad: Frontend Team (sq-1)                               │
│ Leader: agent-1                                           │
│ Members: agent-2, agent-3                                 │
├──────────────────────────────────────────────────────────┤
│ Issue Lifecycle:                                          │
│ parent issue assigned to squad                            │
│   → leader task queued (agent-1)                        │
│     → leader creates child issue 1 (agent-2)             │
│       → worker task queued → completed                   │
│     → leader creates child issue 2 (agent-3)             │
│       → worker task queued → completed                   │
│   → leader re-awakened (child done)                     │
│     → parent issue completed                              │
├──────────────────────────────────────────────────────────┤
│ Activities:                                               │
│ 14:32  leader briefed                                     │
│ 14:33  child issue 1 created → worker task               │
│ 14:34  worker completed → notify_child_done              │
│ 14:35  leader re-awakened                                │
└──────────────────────────────────────────────────────────┘
```

#### 10. Issue Lifecycle Timeline (signature — 贯穿所有页面)
```
当选中一条 issue 时,底部或 Inspector 显示其完整生命周期:

  ┌──────┐    ┌───────┐    ┌────────┐    ┌────────┐    ┌──────────┐    ┌──────────┐
  │created│───►│task   │───►│claimed │───►│running │───►│completed │───►│in_review  │
  │14:32  │    │queued │    │14:33   │    │14:33   │    │14:34     │    │14:34     │
  └──┬────┘    └───┬───┘    └────────┘    └────────┘    └────┬─────┘    └────┬─────┘
     │             │                                         │              │
     │             ▼                                         │              ▼
     │        session events                              comment      resume task
     │        (claim/progress/done)                         "fix X"      (queued →
     │                                                    → task        claimed → ...)
     │                                                    queued
     ▼
  [clickable: each node jumps to the related object]
```

### Top Bar
```
◆ mica  [ws-1 ▾] [alice ▾] [rt-1: claude] [WS ●/○]
```
- workspace 下拉切换
- member 下拉切换(演示 membership gate)
- runtime 状态(CLI detected)
- WS 连接状态(● connected / ○ disconnected)

### Action Bar (底部固定)
```
[▶ Run daemon once] [⏰ Autopilot tick] [↺ Reset all] [📋 Seed demo data]
```
- Run daemon once: POST /api/daemon/run-once → 刷新 tasks + issues + timeline
- Autopilot tick: POST /api/autopilots/tick → 刷新 autopilots + issues
- Reset: 清空所有 stores
- Seed: 创建示例 issue/task/skill/autopilot/squad(一键 demo)

### Inspector (右侧)
- 上半:选中对象的 JSON(可展开/折叠)
- 下半:Related Objects(可点击跳转)
  - 选中 issue → 显示其 tasks, comments, children, assignee agent
  - 选中 task → 显示其 issue, session events, agent, prior task
  - 选中 skill → 显示其 keywords + body preview

## API Endpoints (~25)

### Overview
- `GET /api/overview` — 全局计数 + recent activity

### Issues
- `GET /api/issues` — list(+ child_count, latest_task, comment_count)
- `POST /api/issues` — create(title, assignee_type, assignee_id)
- `GET /api/issues/{id}` — detail(issue + tasks + comments + children)
- `PATCH /api/issues/{id}/status` — transition
- `POST /api/issues/{id}/comments` — comment + enqueue follow-up
- `POST /api/issues/{id}/rerun` — rerun task

### Tasks
- `GET /api/tasks` — list(?status=queued filter)
- `POST /api/daemon/run-once` — daemon.run_once()
- `GET /api/tasks/{id}/events` — session events

### Realtime
- `WS /ws/issues/{id}` — push events

### Skills
- `GET /api/skills` — list
- `POST /api/skills/preview` — match preview(title, body → matched skill + injected prompt)

### Autopilot
- `GET /api/autopilots` — list
- `POST /api/autopilots` — create
- `POST /api/autopilots/tick` — tick

### Runtimes
- `GET /api/runtimes` — detect + runtime info
- `GET /api/agents` — agent list

### Workspaces
- `GET /api/workspaces` — list + members
- `POST /api/workspaces/{id}/members` — add member

### Squads
- `GET /api/squads` — list + roster
- `POST /api/squads` — create
- `POST /api/issues/{id}/squad/children` — create child
- `POST /api/squads/{id}/worker-report` — worker report

### Demo
- `POST /api/demo/seed` — 一键创建示例数据
- `POST /api/demo/reset` — 清空所有 stores

## Implementation

### Files
- `sessions/zh/s13_console.py` — s12 原样 + helpers + create_console_app + CONSOLE_HTML + main
- `tests/test_console.py` — API tests
- `docs/plans/2026-07-27-mica-s13-console.md`

### New helpers
```python
def _squad_to_dict(s): return {"id":s.id, "name":s.name, "leader_agent_id":s.leader_agent_id, "member_agent_ids":s.member_agent_ids}
def _skill_to_dict(s): return {"name":s.name, "description":s.description, "keywords":s.keywords, "path":s.path, "body":s.body[:200]}
def _runtime_to_dict(r): return {"id":r.id, "name":r.name, "clis":r.clis}
def _member_to_dict(m): return {"id":m.id, "name":m.name}
```

### CONSOLE_HTML (~300-400 行)
- `<style>`: dark theme + amber accent + CSS Grid + status badges + timeline nodes
- `<body>`: grid layout (top/nav/main-split/inspector/bottom)
- `<script>`: 
  - `state` object (current view, selected object, stores)
  - `loadView(view)` — fetch API, render list + detail
  - `selectObject(type, id)` — fetch detail, render inspector
  - `renderTimeline(issue)` — 生成 lifecycle timeline HTML
  - `runDaemonOnce()` / `autopilotTick()` / `seedDemo()` / `resetDemo()`
  - `watchIssue(id)` — WebSocket, append events
  - `switchWorkspace(ws)` / `switchMember(m)` — 演示 membership gate
  - 所有列表项可点击 → selectObject → Inspector + Timeline

### Tests `tests/test_console.py` (~10 tests)
- `test_overview` — GET /api/overview 返回计数
- `test_create_agent_issue` — POST → task 入队
- `test_create_squad_issue` — POST → squad router 路由
- `test_comment_creates_resume_task` — POST comment → follow-up task
- `test_daemon_run_once` — POST → task completed, issue in_review
- `test_autopilot_tick` — POST autopilot + tick → issue created
- `test_skills_preview` — POST preview → matched skill
- `test_squad_worker_report` — POST → leader re-awakened
- `test_demo_seed` — POST seed → 有数据
- `test_demo_reset` — POST reset → 空数据

### main()
```python
def main(data_dir=None):
    # wire: IssueStore + TaskStore + SessionStore + MemberStore + AutopilotStore
    #       + RealtimeHub + SkillLoader + Scheduler + SquadRouter + Daemon(StubRunner)
    #       + PathDetector + Runtime + Agent(s)
    app = create_console_app(all_stores...)
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

## Verification
- `python -m py_compile sessions/zh/s13_console.py`
- `python -m pytest tests/ -v` (~114 tests)
- `python sessions/zh/s13_console.py` → http://localhost:8000/
- 浏览器:10 个页面都有内容,对象可点击跳转,timeline 可见,action 按钮工作
- `openspec validate add-mica-core`
