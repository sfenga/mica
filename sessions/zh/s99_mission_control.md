# s99 — Mission Control:把学习系统变成可操作控制台

s99 的目标不是再发明一个核心模型,而是把 s01-s17 的能力接到一个本地 Web 控制台里。学习者可以不写 Python 调用代码,直接在页面上完成一条真实流程:

```text
创建 Project
  -> 创建/编辑 Agent Profile
  -> 绑定 Agent skills
  -> 创建 Issue 并指定 assignee
  -> runtime slot 可用后后台 daemon claim 并执行 task
  -> 查看 trace/events
  -> 准备并查看 Execution Context 文件
  -> 在 System 页观察 runtime slot 占用和释放
```

这就是 Mission Control 的定位:它既是操作入口,也是系统理解工具。每个按钮背后都对应前面章节的一个对象或一条数据流。

## 1. 控制台集成了哪些能力

```text
s01-s04  Issue / Task / Daemon / Runner / Persistence     任务从创建到执行和落盘
s05-s07  Comment resume / Realtime / Web panel            续跑、事件流和基础看板
s08-s10  SkillLoader / Autopilot / Runtime + CLI          技能、定时触发和 runtime 边界
s11-s14  Workspace / SquadRouter / Project / AgentStore   协作边界、workdir 和 agent profile
s15      DaemonWorker                                     常驻后台消费 queued task
s16      ExecutionContextBuilder                          把 task 执行上下文写入 workdir
s17      RuntimeSlotPool + SlotDaemonWorker               先有执行空位,再 claim task
```

s99 把这些能力放在同一个 FastAPI 应用里:

```text
API layer
  接收页面操作,读写 stores,触发 daemon/context/squad/autopilot

Stores
  保存 issues、tasks、projects、agents、skills、squads、sessions

RuntimeSlotPool + SlotDaemonWorker
  后台等待 wakeup 或 poll interval,先 acquire slot,再 claim/execute task

ExecutionContextBuilder
  按 task 解析 issue/project/agent/skills,写入 .mica/tasks/<task_id>/

Console UI
  提供可点击的 Project / Issue / Task / Agent / Skill / Daemon / Runtime slots / Trace 页面
```

## 2. 一条完整操作路径

最推荐的学习方式是按下面顺序走一遍。

### 2.1 创建 Project

Projects 页保存的是“这个 issue 应该在哪个本地目录执行”:

```text
Project.name       repo 或业务项目名
Project.local_path 本地工作目录
Project.repo_url   可选,用于记录来源
```

创建 issue 时选择 project 后,daemon 执行 task 会把 `cwd` 解析为 `Project.local_path`;s16 准备 execution context 时也会把文件写到这个目录下:

```text
<Project.local_path>/.mica/tasks/<task_id>/
```

所以 Project 不是展示字段,它决定 runtime/CLI 面向哪个代码目录工作。

### 2.2 创建 Agent Profile

Agents 页对应 s14 的 `AgentStore`:

```text
Agent.id             task.assignee_id 用它查 agent
Agent.name           页面展示名
Agent.cli            使用哪个 CLI,例如 codex / claude
Agent.runtime_id     属于哪个 runtime
Agent.system_prompt  agent 的长期身份说明
Agent.skill_names    agent 绑定的 skill name 列表
```

如果没有显式创建 agent profile,控制台会从 runtime.clis 派生默认 agent,例如 `agent-1 / Claude`、`agent-2 / Codex`。一旦你创建了同 id 的 profile,显式配置会覆盖派生配置。

### 2.3 绑定 Skills

Skills 页读取的是 workspace 下的:

```text
skills/<skill-name>/SKILL.md
```

Mission Control 只把 skill name 绑定到 agent:

```text
Agent.skill_names = ["code-review", "playwright"]
```

Issue 不直接绑定 skill。执行时 daemon 根据 `task.assignee_id` 找到 Agent,再按 `Agent.skill_names` 调用 `SkillLoader.get(name)` 加载 skill 内容。这个边界很重要:

```text
Issue
  描述要解决的问题

Agent
  声明自己会哪些技能

Daemon.execute
  在执行 task 时把 agent 身份和 skill 上下文注入 runner
```

这样设计可以避免“某个 issue 标题写了 review,就隐式拿到 code-review skill”的不透明行为。权限和能力由 agent profile 决定,而不是由任务文本临时决定。

### 2.4 创建 Issue 并入队

Issues 页创建 issue 时需要选择:

```text
workspace_id
project_id
assignee_type = agent 或 squad
assignee_id
title / body
```

如果 assignee 是 agent,创建 issue 后会直接生成一个 queued task:

```text
IssueStore.create(...)
TaskStore.enqueue(issue.id, "agent", assignee_id)
SlotDaemonWorker.wakeup()
```

如果 assignee 是 squad,请求会进入 `SquadRouter.assign_issue_to_squad(...)`,由 squad leader 获得 leader task。后续 leader 决定是否创建 child issue、派给哪个 worker、等待哪个 worker report。

### 2.5 让 Daemon 自动执行

Daemon 页是 s15 + s17 的可视化入口:

```text
Start daemon   启动后台 SlotDaemonWorker
Stop           停止后台 SlotDaemonWorker
Wakeup         手动唤醒 worker
Run once       调试用,同步执行一轮 daemon.run_once()
Status         查看 running / queued / claimed_count / completed_count / poll_interval
Slots          查看 max_slots / available / occupied task
```

后台模式下,任务入队后不需要再手动点 `Run once`。`SlotDaemonWorker` 会被唤醒,随后执行 slot-before-claim:

```text
slot_pool.acquire()
  -> 没有 slot:不 claim task,queued task 留在队列
  -> 有 slot:daemon.claim_next(...)
  -> daemon.execute(task)
  -> runner.run(...)
  -> finally slot_pool.release(slot_id)
  -> 写 task status、issue status、session events
```

这就是 s17 接入 s99 后最重要的行为:没有本地执行容量时,控制台不会提前把 queued task claim 成 dispatched。`Run once` 仍然有价值:它让测试和调试可以在没有后台线程的情况下同步 drain 当前队列,但主路径是 `Start daemon` 后由 slot-aware worker 自动执行。

### 2.6 查看 Task Trace

Tasks 或 Issue detail 里可以进入 task detail。这里展示两类信息:

```text
task 本身
  status / issue_title / assignee / workdir

events
  daemon claim
  runner progress
  runner done
  result/session id
```

Trace 的学习价值在于把“任务完成了”拆开看:你能看到 daemon 什么时候 claim,runner 收到了什么,执行完成后 session 里留下了哪些事件。

### 2.7 准备 Execution Context

Task detail 里的 `Prepare context` 对应 s16:

```text
POST /api/tasks/{task_id}/context/prepare
```

它会调用 `ExecutionContextBuilder.build(task)`,在 project workdir 下写出:

```text
.mica/tasks/<task_id>/
  agent-identity.md
  task-context.md
  skills/<skill-name>/SKILL.md
  manifest.json
```

页面会显示 context 是否已准备、目录位置、manifest 文件列表,并允许直接打开某个 context 文件查看内容。

这一步不是执行 task,而是把执行前 runtime 应该可见的上下文物化出来。你可以把它理解成“给 CLI 进入项目目录前摆好的任务资料夹”。

## 3. API 地图

Mission Control 的 API 与页面是一一对应的。

### Issues / Tasks

```text
GET    /api/issues
POST   /api/issues
GET    /api/issues/{issue_id}
PATCH  /api/issues/{issue_id}/status
POST   /api/issues/{issue_id}/comments
POST   /api/issues/{issue_id}/rerun

GET    /api/tasks
GET    /api/tasks/{task_id}
GET    /api/tasks/{task_id}/events
```

### Daemon

```text
GET    /api/daemon/status
POST   /api/daemon/start
POST   /api/daemon/stop
POST   /api/daemon/wakeup
POST   /api/daemon/run-once
```

`start/stop/wakeup` 操作的是 `SlotDaemonWorker`;`run-once` 操作的是底层 `Daemon`。这两个入口共用同一个 task queue。`/api/daemon/status` 会同时返回 `slots` 汇总,方便页面展示 runtime 容量。

### Execution Context

```text
POST   /api/tasks/{task_id}/context/prepare
GET    /api/tasks/{task_id}/context
GET    /api/tasks/{task_id}/context/files/{file_path}
```

`prepare` 会写文件;`context` 和 `files` 只读已准备好的 context。

### Agents / Skills

```text
GET    /api/agents
POST   /api/agents
GET    /api/agents/{agent_id}
PATCH  /api/agents/{agent_id}
POST   /api/agents/{agent_id}/skills
DELETE /api/agents/{agent_id}/skills/{skill_name}

GET    /api/skills
GET    /api/skills/{name}
```

这些 API 体现 s14 的模型:agent 保存 skill names,skill loader 从文件系统读取 skill 正文。

### Runtime Slots

```text
GET    /api/runtime/slots
```

这个 API 直接返回 `RuntimeSlotPool` 的 `max_slots / available / occupied`,用于观察 s17 的 slot-before-claim 行为。

### Projects / Squads / Autopilots

```text
GET    /api/projects
POST   /api/projects

GET    /api/squads
POST   /api/squads
POST   /api/issues/{issue_id}/squad/children
POST   /api/squads/{squad_id}/worker-report

GET    /api/autopilots
POST   /api/autopilots
POST   /api/autopilots/tick
```

这些页面让 s08-s13 的概念可以在同一个系统里联动:project 决定 workdir,autopilot 定时创建 issue,squad 把 issue 转成 leader/worker 协作流程。

## 4. 四个关键边界

### 4.1 Mission Control 不替代 Daemon

页面负责发起操作,daemon 负责 claim 与 execute:

```text
Console click
  -> API route
  -> store mutation
  -> wakeup daemon
  -> SlotDaemonWorker acquire slot
  -> daemon.claim_next() / daemon.execute(task)
```

所以调试执行问题时,优先看 task status 和 events,而不是只看按钮有没有点成功。

### 4.2 Issue 不决定 Skill

Skill 能力来自 agent profile:

```text
task.assignee_id
  -> AgentStore.get(agent_id)
  -> Agent.skill_names
  -> SkillLoader.get(name)
  -> runner skill_body / context files
```

Issue 只描述任务需求。这个边界让同一个 issue 派给不同 agent 时,可见技能可以不同。

### 4.3 Execution Context 不等于执行结果

`Prepare context` 只写文件,不改变 task 状态:

```text
queued task
  -> prepare context
  -> task 仍然 queued
```

它的价值是让 runtime 启动前的输入可见、可审计、可测试。执行结果仍然由 daemon/runner 写回 task 和 session events。

### 4.4 Runtime Slot 不等于 Task Queue

queued task 表示“有活可领”,runtime slot 表示“本机现在有执行空位”。s99 现在使用 s17 的 `RuntimeSlotPool + SlotDaemonWorker`,所以后台主路径是:

```text
有 queued task + 有 available slot
  -> claim task
  -> occupied slot 绑定 task_id
  -> execute 完成后 release slot

有 queued task + 没有 available slot
  -> 不 claim task
  -> task 保持 queued
```

这个边界避免了“任务已经被 claim,但本机其实没有能力执行”的假运行状态。

## 5. 与 multiCa 的对应关系

| 维度 | mica s99 | multiCa |
|---|---|---|
| 控制台 | FastAPI + 内联 vanilla JS | 更完整的 Web/CLI 操作面 |
| 后台执行 | `SlotDaemonWorker.start()` | daemon 常驻进程与 poll/wakeup loop |
| task claim | `slot_pool.acquire()` 后 `Daemon.claim_next()` | server/daemon 协作 claim task |
| project workdir | `Project.local_path` | workspace/project checkout 或执行目录 |
| agent 配置 | `AgentStore` + `Agent.skill_names` | agent profile 与 agent-skill 关系 |
| skill 加载 | `SkillLoader` 读取 `skills/**/SKILL.md` | workspace/builtin/imported skill bundle |
| context prepare | `.mica/tasks/<task_id>/...` | execenv/runtime 启动前准备上下文 |
| runtime capacity | `RuntimeSlotPool` + `/api/runtime/slots` | runtime max concurrency / slot semaphore |
| squad | `SquadRouter` leader-first 教学模型 | squad coordinator、leader/worker task 编排 |

可以用一句话理解 s99:

```text
s99 把 mica 的对象模型接成一个可点击、可执行、可观察、可调试的本地学习系统。
```

## 试一试

运行测试:

```bash
python -m pytest tests/test_console.py tests/test_mission_control_agents.py -q
```

启动控制台:

```bash
python sessions/zh/s99_mission_control.py
```

打开 `http://127.0.0.1:8000/`,建议按这个顺序操作:

```text
1. Projects:创建一个指向本地 repo 的 project
2. Agents:创建或编辑 agent,填写 system_prompt,绑定 skill
3. Daemon:点击 Start daemon
4. Issues:创建 issue,选择 project 和 agent
5. Tasks/Trace:查看 task 自动完成和事件流
6. System:观察 runtime slots 的 available / occupied
7. Task detail:点击 Prepare context,查看 agent-identity.md / task-context.md / manifest.json
```

走完这条路径后,你看到的就不是一个静态观察页,而是 s01-s17 串起来后的最小可用 Mission Control。
