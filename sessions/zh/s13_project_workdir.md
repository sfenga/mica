# s13 — Project 与 Workdir:让 agent 在正确的代码目录里执行

s13 学的是多 agent 系统里一个很容易混淆、但非常关键的问题:agent 到底应该在哪个目录里运行?

在 s01-s12 中,issue、task、daemon、runtime、workspace、squad 都已经有了。但如果 agent CLI 真正要修改代码,还缺一个执行边界:它不能总是在启动 `mica` 的目录里运行,而应该进入当前任务对应的项目目录。s13 用一个最小实现把这条链路打通:

```text
Project.local_path
   -> Issue.project_id
   -> Daemon.execute() 解析 workdir
   -> Task.workdir 记录本次执行目录
   -> Runner.run(..., cwd=workdir)
   -> subprocess.Popen(..., cwd=cwd)
```

这一章的重点不是做完整 repo checkout,而是先建立“项目目录是执行上下文”的概念。workspace 解决的是协作和权限边界;project/workdir 解决的是 agent 进哪个代码目录干活。

## 1. 为什么需要 workdir

如果 runner 不指定 `cwd`,Python 的 `subprocess.Popen()` 会继承当前 Python 进程的工作目录。也就是说,你从 `/path/to/mica` 启动 demo,agent CLI 默认也会在 `mica` 目录里运行。

这对教学前几章没问题,因为 s01-s12 主要学习调度链路,并不真的要求 agent 修改另一个项目。但真实使用中,用户可能希望:

```text
issue A -> 在 /path/to/app-a 里修 bug
issue B -> 在 /path/to/app-b 里写测试
issue C -> 在 /path/to/docs-site 里改文档
```

这些任务可以属于同一个 workspace,也可以由同一个 daemon 执行,但它们的代码目录不同。如果没有 workdir,agent 只能继承 daemon 启动目录,就会出现三个问题:

```text
1. agent 看不到真正的项目文件
2. agent 可能误改 mica 教学项目本身
3. resume / rerun 时无法审计当时在哪个目录执行
```

s13 的目标就是把“当前 issue 属于哪个项目目录”变成显式数据,而不是让它藏在 shell 当前目录里。

## 2. Workspace、Project、Workdir 的关系

读 s13 时,先把三个词分开:

```text
Workspace  协作/租户边界:谁能看见、创建、操作这些对象
Project    工作容器:一组 issue 共同指向的代码/业务项目
Workdir    执行目录:agent CLI 子进程真正运行的本地路径
```

一个 workspace 可以有多个 project。一个 project 可以有自己的本地目录。一个 issue 可以通过 `project_id` 关联到某个 project。daemon 执行这个 issue 的 task 时,根据 `project_id` 找到 project,再把 `project.local_path` 传给 runner。

所以不要把 workspace 理解成“代码目录”。workspace 更像团队空间或协作边界;workdir 才是 agent 执行命令时的 `cwd`。

## 3. Project:把项目目录建模成数据

s13 新增了 `Project`:

```python
@dataclass
class Project:
    id: str
    workspace_id: str
    name: str
    local_path: str = ""
    repo_url: str = ""
```

核心字段是:

```text
workspace_id   project 属于哪个 workspace
name           给人看的项目名
local_path     本地项目目录;空字符串表示没有显式目录
repo_url       预留字段,教学版暂不做 checkout
```

`local_path` 是本章最重要的字段。它表示 agent 应该进入哪个本地目录执行。这里的路径可以是 `/tmp/mica-app`,也可以是你机器上的真实项目目录,例如 `/path/to/my-app`。

`repo_url` 目前只是数据占位。完整系统会区分本地目录和远程 repo checkout:s13 只实现 local path 这一条最短链路,让读者先理解 workdir 如何进入 task 执行。

## 4. ProjectStore:和 IssueStore 一样的教学存储模式

`ProjectStore` 的结构和前面几个 store 保持一致:

```python
class ProjectStore:
    def create(self, workspace_id, name, local_path="", repo_url="") -> Project
    def list(self, workspace_id=None) -> list[Project]
    def get(self, project_id) -> Project | None
```

如果传入 `data_dir`,它会把 project 快照追加到 `projects.jsonl`,启动时再按 last-wins 重放。这样 s13 延续了 s04 的持久化模型:

```text
issues.jsonl      issue 快照
tasks.jsonl       task 快照
projects.jsonl    project 快照
```

这让 project 不是一次性的局部变量,而是系统里的业务对象。issue 只保存 `project_id`,不直接复制 `local_path`;执行时再通过 `ProjectStore` 查询最新 project。

## 5. Issue.project_id:issue 关联到项目

s13 给 `Issue` 增加了:

```python
project_id: str = ""
```

创建 issue 时可以直接指定:

```python
project = project_store.create(
    "ws-1",
    "mica-app",
    local_path="/tmp/mica-app",
)

issue = issue_store.create(
    "ws-1",
    "implement workdir resolution",
    assignee_type="agent",
    assignee_id="agent-1",
    project_id=project.id,
)
```

也可以先创建 issue,再关联 project:

```python
issue_store.set_project(issue.id, project.id)
```

这里的设计很重要:issue 和 project 是弱关联,不是把 project 内容复制进 issue。issue 只记录“我属于哪个 project”。这样 project 的 `local_path` 如果后续调整,daemon 下一次执行时会解析到新的目录。

教学版没有做 project_id 的存在性校验。也就是说,`IssueStore.create(..., project_id=...)` 不会检查 `ProjectStore` 里是否真的有这个 project。真正解析发生在 daemon 执行阶段:找得到 project 就用它的 local_path;找不到就退回空 workdir。

## 6. Task.workdir:记录这次执行到底在哪里跑

s13 给 `Task` 增加了:

```python
workdir: str = ""
```

它不是 task 入队时就一定有值。当前实现是在 daemon 执行 task 时解析:

```text
task queued 时:
  workdir = ""

daemon execute 时:
  issue = IssueStore.get(task.issue_id)
  project = ProjectStore.get(issue.project_id)
  task.workdir = project.local_path
```

为什么要把解析结果写回 `Task.workdir`?

因为它是审计信息。执行结束后,你不仅想知道这个 task 是否完成,还想知道:

```text
这个 task 当时在哪个目录运行?
它有没有继承 daemon cwd?
如果用户后来改了 project.local_path,这条历史 task 用的是旧目录还是新目录?
```

`Task.workdir` 记录的是“本次执行实际解析出的目录”。这和 `Project.local_path` 的当前值不是同一个概念。

## 7. Daemon.execute():workdir 的解析点

s13 的关键逻辑在 `Daemon.execute()`:

```python
issue = self.issue_store.get(task.issue_id)

workdir = ""
if issue is not None and issue.project_id and self.project_store is not None:
    project = self.project_store.get(issue.project_id)
    if project:
        workdir = project.local_path

task.workdir = workdir
self.task_store._persist(task)
```

随后 runner 被调用:

```python
result = self.runner.run(
    task,
    issue,
    [],
    on_event,
    resume_session_id=prior_sid or None,
    append_system_prompt=skill.body if skill else None,
    cwd=workdir or None,
)
```

这里有几个细节值得注意。

**解析发生在 claim 之后、runner 执行之前。** task 被 daemon 抢到后,daemon 才读取 issue 和 project,决定本次 cwd。这符合执行模型:workdir 是执行环境的一部分,不是用户看板展示字段。

**空字符串会变成 `None`。** `cwd=workdir or None` 表示如果没有项目目录,就不显式传 cwd,让 subprocess 继承当前进程目录。

**project_store 是可选依赖。** 如果 daemon 没有注入 `ProjectStore`,即使 issue 有 project_id,也解析不到 workdir。这让前面章节的测试和 demo 仍然可以在没有 project 系统的情况下运行。

## 8. Runner.run():把 cwd 传给 CLI 子进程

s13 修改了 `AgentRunner` 协议:

```python
class AgentRunner(Protocol):
    def run(
        self,
        task: Task,
        issue: Issue,
        context: list,
        on_event: Callable[[dict], None],
        resume_session_id: str | None = None,
        append_system_prompt: str | None = None,
        cwd: str | None = None,
    ) -> RunResult: ...
```

`StubRunner` 接收 `cwd`,但不使用它。这样测试可以先验证 daemon 是否把 workdir 写到 task 上,不需要真的启动 CLI。

真正使用 cwd 的是 `ClaudeCliRunner`:

```python
proc = subprocess.Popen(
    self._cmd(...),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    cwd=cwd,
)
```

这行代码就是 s13 的终点。到这里,project 的本地路径已经从业务对象一路传到了操作系统子进程。Claude CLI 看到的当前目录就是 `project.local_path`。

如果 `cwd=None`,Python 不改变子进程目录;如果 `cwd="/tmp/mica-app"`,子进程就在 `/tmp/mica-app` 里运行。

## 9. 一条完整链路

s13 demo 的流程是:

```python
is_ = IssueStore(data_dir=data_dir)
ts = TaskStore(data_dir=data_dir)
ps = ProjectStore(data_dir=data_dir)
daemon = Daemon(is_, ts, StubRunner(), project_store=ps)

project = ps.create("ws-1", "mica-app", local_path="/tmp/mica-app")

issue = is_.create(
    "ws-1",
    "implement workdir resolution",
    body="agent should run inside project.local_path",
    assignee_type="agent",
    assignee_id="agent-1",
    project_id=project.id,
)

task = ts.enqueue(issue.id, "agent", "agent-1")
daemon.run_once()
```

运行后输出类似:

```text
=== s13 demo: project + workdir ===
1) created project: id=... name=mica-app local_path=/tmp/mica-app
2) created issue: id=... project_id=...
3) enqueued task: id=... status=queued workdir(before)=''
4) daemon ran task: status=completed workdir(resolved)='/tmp/mica-app'
   agent ran in workdir: /tmp/mica-app
```

注意第三步和第四步的差异:

```text
入队时 workdir(before)=''
执行后 workdir(resolved)='/tmp/mica-app'
```

这说明 workdir 不是 issue 创建时硬写到 task 里的,而是 daemon 执行时从 project 解析出来的。

## 10. 和 resume 的关系

s05 学过 comment resume:新 task 会查同一个 issue、同一个 agent 最近 completed task 的 `session_id`,再传给 runner 的 `resume_session_id`。

s13 之后,runner 同时拿到两个上下文:

```text
resume_session_id   接上哪一次 CLI 会话
cwd                 在哪个项目目录里继续执行
```

这两个值解决不同问题。

`resume_session_id` 让 agent 接上对话上下文。`cwd` 让 agent 在正确的文件系统目录里运行。只有 session 没有 cwd,agent 可能记得自己要修 bug,但看不到项目文件;只有 cwd 没有 session,agent 能看到文件,但不知道上一次讨论和执行结果。

当前教学版只实现了“每次执行从 project 解析 cwd”。它没有实现“手动 rerun 必须复用源 task 的旧 workdir”。因此如果 project.local_path 在两次执行之间被修改,下一次 task 会解析到新的 local_path。这一点要和完整系统区分开。

## 11. 和 skills / AGENTS.md 的关系

很多 agent CLI 会从当前目录向上查找项目配置,例如:

```text
AGENTS.md
.claude/skills/
.opencode/skills/
package.json
pyproject.toml
```

如果 cwd 错了,agent 不只是“看不到代码”,还可能读不到项目级 instructions 和技能配置。s13 把 cwd 传给 subprocess,就是为了让 CLI 的项目发现逻辑从目标 project 开始,而不是从 daemon 启动目录开始。

教学版没有把 s08 的 markdown skills 写入 workdir,也没有生成 AGENTS.md。s13 只完成最底层的 cwd 传递:daemon 根据 issue.project_id 找到 project.local_path,再把它作为 runner 的 cwd。后续 s14 会通过 Agent.skill_names 决定执行时加载哪些 skill。完整系统会在 workdir 里写入 issue context、技能文件、runtime marker、sidecar manifest 等辅助文件,并在 local directory 模式结束时清理这些 sidecar。

## 12. 和 multiCa 的对应关系

multiCa 中有更完整的 project/resource/workdir 模型。项目资源可以是远程 repo,也可以是本地目录。远程 repo 通常进入 daemon 管理的隔离 workdir;本地目录则表示在用户指定路径上原地执行。

s13 对应的是这条最小链路:

```text
project resource local_directory
   -> issue belongs to project
   -> task claim resolves workdir
   -> daemon passes Cwd to provider runner
```

但 s13 保留了很多教学简化:

| 主题 | s13 教学版 | 完整 multiCa |
|---|---|---|
| project 数据 | `Project(id, workspace_id, name, local_path, repo_url)` | project + project_resource,资源可扩展 |
| 本地目录 | `Project.local_path` 一个字符串 | `local_directory` resource,包含 local_path、daemon_id、label |
| 远程 repo | 只保留 `repo_url` 字段 | 支持 repo checkout / agent 按需 checkout |
| workdir 准备 | 不创建目录,不写上下文文件 | prepare/reuse envRoot/workdir,写 issue context、skills、markers |
| 并发保护 | 没有 local_path 锁 | local_directory 忙时 task 可进入 waiting_local_directory |
| 清理策略 | 不清理 | 区分托管 workdir 和用户 local_directory,避免删除用户目录 |
| rerun workdir | 每次重新按 project 解析 | 手动 rerun 会优先复用源 task workdir |

这张表不是为了说 s13 不完整,而是帮助你把学习边界放准。s13 学的是“workdir 如何从 project 数据进入 runner 子进程”。完整 multiCa 在这条链路周围还有安全、并发、清理、checkout、复用等工程层。

## 13. 测试覆盖了什么

`tests/test_workdir.py` 覆盖了 s13 的关键行为:

```text
test_project_create
  ProjectStore 可以创建带 local_path 的 project

test_project_store_list_get
  ProjectStore 可以按 workspace list 和按 id get

test_issue_with_project
  Issue.create 可以写入 project_id

test_issue_set_project
  IssueStore.set_project 可以后绑定 project

test_task_workdir_resolved
  daemon 执行 task 时会从 issue.project_id 解析 project.local_path

test_task_workdir_empty_when_no_project
  没有 project_store / project 时,task.workdir 保持空

test_runner_uses_cwd
  ClaudeCliRunner 调 Popen 时传入 cwd

test_runner_cwd_none_when_no_project
  没有项目时 Popen 的 cwd 是 None
```

其中最核心的是后两类测试。前面几个证明数据模型存在;后面几个证明这条数据真的进入了执行层。

## 14. 本章边界

s13 已经解决了“agent 不应该只能在 mica 目录里执行”的核心问题,但它仍然是教学版:

- 不校验 `local_path` 是否存在。
- 不限制 `local_path` 是否属于当前 daemon。
- 不支持多个 project resource。
- 不做 git clone / checkout。
- 不做 local directory 并发锁。
- 不写 issue context / skills 到 workdir。
- 不做 sidecar manifest 和清理。
- 不实现 task workdir 复用策略。
- 不把 project 接入 Web UI。

这些边界都可以作为后续章节继续扩展。当前最重要的是理解这条链路:

```text
workspace 决定谁能访问
project 决定任务属于哪个代码项目
workdir 决定 agent CLI 在哪个目录执行
task 记录本次实际执行目录
```

只要这四层分清,后面再学习 repo checkout、local_directory 锁、workdir reuse、sidecar cleanup,就不会把“协作空间”和“代码目录”混在一起。

## 15. 试一试

运行 s13 demo:

```bash
python sessions/zh/s13_project_workdir.py
```

你应该看到类似:

```text
=== s13 demo: project + workdir ===
1) created project: id=... name=mica-app local_path=/tmp/mica-app
2) created issue: id=... project_id=...
3) enqueued task: id=... status=queued workdir(before)=''
4) daemon ran task: status=completed workdir(resolved)='/tmp/mica-app'
   agent ran in workdir: /tmp/mica-app
```

运行测试:

```bash
python -m pytest tests/test_workdir.py -q
```

如果测试通过,说明三件事成立:

```text
Project 可以保存 local_path
Issue 可以关联 Project
Daemon/Runner 会把 local_path 变成 subprocess cwd
```
