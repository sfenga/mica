# s14 — Mission Control:把 s01-s13 集成到一个学习控制台

s01-s13 分别讲了 issue、task、daemon、真实 CLI、持久化、resume、realtime、web panel、skills、autopilot、多 CLI、workspace、squads、project/workdir。s14 把这些能力集成进一个 **Mission Control** 学习控制台:一个 FastAPI app + 单页 HTML,打开浏览器就能看到系统对象之间的关系,也能直接操作 issue、task、project、squad、autopilot、workspace 和 daemon。

这一章的学习目标不是做一个生产级前端,而是让前面十三章的对象第一次同时出现在同一个界面里。你可以一边点页面,一边回到代码里看每个 API 如何调用前面章节的 store/router/daemon。

## 一、核心设计思想

第一个思想:**一个 app 集成全部对象**。`create_console_app()` 闭包引用 issue/task/session/hub/skill/autopilot/member/squad/project/daemon/runtime 等对象,把它们通过 API 暴露给页面。

第二个思想:**控制台是学习工具,不是营销页面**。页面从 Overview 进入,再切到 Issues、Tasks、Trace、Skills、Autopilot、Runtimes、Workspaces、Squads、Projects、Timeline 等视图。每个视图都服务一个学习问题:当前系统里有什么对象?谁触发了谁?task 为什么会变成 completed?issue 的 workdir 是从哪里来的?

第三个思想:**对象可以互相跳转**。选中 issue 后,右侧 inspector 会显示它的 tasks、comments、children、project/workdir;选中 task 后,可以看到它所属 issue、session、prior session、workdir 和执行状态。

第四个思想:**默认使用 StubRunner 保持可复现**。真实 CLI runner 在 s03 已经学过,但控制台默认要适合反复演示和测试,所以 seed/run/reset 都围绕可预测的 StubRunner 工作。

第五个思想:**project/workdir 被纳入 UI**。s13 解决了 agent 在哪个目录执行的问题;s14 让 project 和 workdir 可以在页面里创建、选择和观察。

## 二、主要页面

| 页面 | 学习重点 |
|---|---|
| Overview | issue、task、agent、runtime 等总览指标 |
| Issues | 创建 issue、选择 assignee/project、查看状态 |
| Tasks | 观察 queued/running/completed/failed 以及 workdir |
| Trace | 查看 SessionStore 里的执行事件 |
| Skills | 预览 `keywords` 关键词是否让 skill 命中当前 issue |
| Autopilot | 创建 cron autopilot,手动 tick 触发 issue/task |
| Runtimes | 查看本地 runtime 和可用 CLI |
| Workspaces | 观察 member gate 的教学模型 |
| Squads | 创建 squad,观察 leader/worker/report 路径 |
| Projects | 创建 project,填写 local_path/repo_url |
| Timeline | 用统一视角看 issue/task/comment/activity |

## 三、关键 API

控制台 API 仍然复用前面章节的业务对象,不是另写一套逻辑:

```text
POST /api/issues            -> IssueStore.create + TaskStore.enqueue 或 SquadRouter.assign
POST /api/daemon/run-once   -> Daemon.run_once()
POST /api/comments          -> comment + resume task
POST /api/autopilots/tick   -> Scheduler.tick()
POST /api/squads            -> SquadRouter.register()
POST /api/projects          -> ProjectStore.create()
GET  /api/fs/browse         -> 只读目录浏览,辅助填写 project.local_path
```

这也是 s14 的核心价值:你能从页面动作一路追到前面章节的代码。

## 四、怎么用

运行:

```bash
python sessions/zh/s14_console.py
```

然后打开:

```text
http://localhost:8000/
```

推荐操作顺序:

```text
Seed demo
  -> Issues 选择一条 issue
  -> Run daemon once
  -> Tasks 查看 task 状态
  -> Trace 查看 session events
  -> Projects 创建一个 local_path
  -> Issues 创建关联 project 的新 issue
  -> Run daemon once 后观察 task.workdir
  -> Squads 创建 squad 并测试 worker report
  -> Reset 清空演示数据
```

## 试一试

```bash
python -m pytest tests/test_console.py -q
python sessions/zh/s14_console.py
```

## 五、安全边界

控制台默认绑定 `127.0.0.1`,只适合本机学习。不要把它改成公网服务。

特别注意目录浏览:

```text
GET /api/fs/browse
```

这个 API 只列目录、不读文件内容,目的是帮助填写 `Project.local_path`。但它仍然会暴露服务端文件系统结构。如果把服务绑定到 `0.0.0.0`,同网段访问者就可能浏览你的目录树。教学场景请保持本地绑定。

真实 CLI runner 也要谨慎。`ClaudeCliRunner` 仍沿用前面章节的教学默认值,会使用较宽松的 permission mode。控制台默认使用 StubRunner,如果你改成真实 CLI,请只在可信目录里运行可信任务。

## 六、与完整 multiCa 的区别

| 维度 | multiCa | mica s14 |
|---|---|---|
| 前端 | Web/desktop/mobile 多端应用 | 单页 HTML,零构建 |
| API | Go Chi | FastAPI |
| 存储 | 数据库 + migration | 内存 / JSONL |
| 认证授权 | 完整 auth + workspace guard | 教学版 member gate |
| runner | daemon/provider/runtime 生产链路 | 默认 StubRunner,可切真实 CLI |
| workdir | prepare/reuse/local_directory/cleanup | project.local_path -> cwd |
| 目标 | 生产协作平台 | 学习和观察系统主链路 |
