# s10 - 多 CLI 与 runtime 级调度

s10 承接前面章节的 task、runner、session、realtime、skill 和 autopilot 能力,继续解决“一个本地进程如何托管多个 CLI agent”的问题。先看一个需要被拆开的简化假设:

```text
一个 daemon 约等于一个 agent
一个 runner 约等于一种 CLI
所有 queued agent task 都属于同一个执行端
```

真实环境更复杂。同一台机器可能同时安装多个 CLI:

```text
claude
codex
opencode
cursor-agent
```

同一个本地 daemon 也可能托管多个 agent:

```text
agent-claude  使用 claude CLI
agent-codex   使用 codex CLI
agent-review  使用 opencode CLI
```

所以 s10 的目标不只是“探测 PATH 上有什么 CLI”,而是把执行链路推进到三个核心能力:

```text
task 绑定 runtime_id
daemon 只 claim 自己 runtime 的 task
daemon 按 agent.cli 选择对应 runner
```

本章采用单文件教学模型,重点放在多 CLI 调度的主路径:任务归属、runtime claim 和 runner 分派。

## 1. 新增结构

本章关注这些对象和字段:

```text
PathDetector      探测 PATH 上可用的 CLI
Runtime           描述执行端: id / name / clis
Agent.cli         agent 想用哪种 CLI
Agent.runtime_id  agent 属于哪个 runtime
Task.runtime_id   task 被分配给哪个 runtime
MultiCliRunner    cli -> runner 的注册表
Daemon            runtime 级 claim + agent.cli 级 dispatch
```

核心关系可以这样看:

```text
Runtime(rt-1, clis=[claude, codex])
   |
   |-- Agent(agent-claude, cli=claude, runtime_id=rt-1)
   |-- Agent(agent-codex,  cli=codex,  runtime_id=rt-1)
   |
   |-- Task(..., assignee_id=agent-claude, runtime_id=rt-1)
   |-- Task(..., assignee_id=agent-codex,  runtime_id=rt-1)

Daemon(runtime_id=rt-1)
   |
   |-- claim only task.runtime_id == rt-1
   |-- resolve assignee_id -> Agent
   |-- dispatch by agent.cli
```

## 2. PathDetector: 只负责发现 CLI

`PathDetector` 的职责很小:

```python
class PathDetector:
    KNOWN_CLIS = ["claude", "codex", "copilot", "opencode", "cursor-agent"]

    @staticmethod
    def detect() -> list[str]:
        return [cli for cli in PathDetector.KNOWN_CLIS if shutil.which(cli)]

    @staticmethod
    def is_available(cli: str) -> bool:
        return shutil.which(cli) is not None
```

它只回答一个问题:

```text
这个 CLI 名字在本机 PATH 上能不能找到?
```

它不负责创建 agent,不负责决定 task 归属,也不负责执行命令。这样边界更清楚:CLI 探测是 runtime 初始化的一部分,不是调度器本身。

## 3. Runtime: 描述执行端

`Runtime` 表示 daemon 所在的执行环境:

```python
@dataclass
class Runtime:
    id: str
    name: str
    clis: list = field(default_factory=list)
```

`runtime.id` 是 claim 边界。一个 daemon 启动时带着自己的 `runtime_id`,之后只处理属于这个 runtime 的 task。

## 4. Agent: 决定使用哪个 CLI

s10 的 `Agent` 有两个关键字段:

```python
@dataclass
class Agent:
    id: str
    name: str
    cli: str = "claude"
    runtime_id: str = "local"
```

在多 CLI 场景里,agent 需要同时表达身份和执行偏好:

```python
Agent(id="agent-claude", name="Claude", cli="claude", runtime_id="rt-1")
Agent(id="agent-codex", name="Codex", cli="codex", runtime_id="rt-1")
```

`runtime_id` 解决“这个 agent 在哪里跑”,`cli` 解决“这个 agent 用什么跑”。

## 5. Task.runtime_id: claim 的硬边界

s10 给 `Task` 加了 `runtime_id`:

```python
@dataclass
class Task:
    id: str
    issue_id: str
    assignee_type: str = ""
    assignee_id: str = ""
    runtime_id: str = ""
    status: TaskStatus = TaskStatus.QUEUED
```

入队时可以直接指定:

```python
task_store.enqueue(issue.id, "agent", "agent-codex", runtime_id="rt-1")
```

这一步很重要。没有 `runtime_id` 时,所有 daemon 都只能靠 `assignee_id` 或本地约定猜测 task 应该在哪里执行;有了 `runtime_id`,claim 就有明确边界:

```text
rt-1 daemon 只能 claim runtime_id == rt-1 的 task
rt-2 daemon 只能 claim runtime_id == rt-2 的 task
```

教学代码还保留一个空 `runtime_id` 的兜底规则,便于观察最小调度链路:

```text
task.runtime_id 有值     -> 必须等于 daemon.runtime_id
task.runtime_id 为空
  且 agent registry 有值 -> 用 agent.runtime_id 判断
task.runtime_id 为空
  且没有 agent registry -> 允许本 daemon claim
```

评论重跑和手动重跑会继承上一条 task 的 `runtime_id`,避免 follow-up task 丢失归属。

## 6. MultiCliRunner: CLI 到 runner 的路由表

`MultiCliRunner` 把 CLI 名称映射到具体 runner。daemon 不需要知道每种 CLI 的细节,只需要拿到 agent 的 `cli` 字段,再交给 `MultiCliRunner` 找到对应执行器:

```python
class MultiCliRunner:
    def register(self, cli: str, runner) -> None:
        self._runners[cli] = runner

    def get(self, cli: str):
        return self._runners.get(cli)

    def run(self, cli: str, task, issue, context, on_event,
            resume_session_id=None, append_system_prompt=None) -> RunResult:
        runner = self.get(cli)
        if runner is None:
            return {"status": "failed",
                    "output": f"runner not registered for cli: {cli}",
                    "exit_code": 127,
                    "session_id": ""}
        return runner.run(task, issue, context, on_event,
                          resume_session_id=resume_session_id,
                          append_system_prompt=append_system_prompt)
```

注册关系长这样:

```python
runners = MultiCliRunner()
runners.register("claude", ClaudeCliRunner())
runners.register("codex", CodexRunner())
```

测试里用的是 `RecordingRunner` / `StubRunner`,所以不依赖本机真的安装 claude 或 codex。

## 7. Daemon.claim_next: runtime 级 claim

`claim_next()` 的核心规则是:只从队列里挑选属于本 runtime 的 agent task:

```python
def claim_next(self) -> Task | None:
    candidates = [
        t for t in self.task_store.list()
        if t.assignee_type == "agent"
        and t.status == TaskStatus.QUEUED
        and self._task_belongs_to_runtime(t)
    ]
    for task in candidates:
        if self.task_store.claim(task.id):
            return task
    return None
```

这个设计保留了 1:N:

```text
一个 daemon 可以托管多个 agent
```

同时给 1:N 加上 runtime 边界:

```text
一个 daemon 不能 claim 其它 runtime 的 task
```

多 daemon 并发时,最终仍靠 `TaskStore.claim()` 做 CAS,所以同一个 task 即使被两个 daemon 同时看到,也只有一个能把状态从 `queued` 改成 `dispatched`。

## 8. Daemon.execute: 按 agent.cli 分派

执行时 daemon 会先解析 agent:

```text
task.assignee_id -> Agent(id, cli, runtime_id)
```

然后决定用哪个 runner:

```text
如果传了 multi_runner:
    用 agent.cli 找 runner
否则:
    用构造 Daemon 时传入的默认 runner
```

关键逻辑是:

```python
agent = self._resolve_agent(task)
if agent is None:
    result = {"status": "failed", "output": f"unknown agent: {task.assignee_id}", ...}
elif self.multi_runner is not None:
    result = self.multi_runner.run(agent.cli, task, issue, [], on_event, ...)
else:
    result = self.runner.run(task, issue, [], on_event, ...)
```

这里采用 fail-closed:

```text
找不到 agent        -> task failed
agent.cli 未注册    -> task failed
issue 不存在        -> task failed
```

它不会在 `codex` runner 缺失时偷偷用默认 `StubRunner` 或 `ClaudeCliRunner` 执行。这个选择很重要,因为不同 CLI 的参数、权限、session 格式、MCP/skills 注入方式都可能不同。静默 fallback 会让错误隐藏到更深的地方。

## 9. demo 的完整链路

`main()` 演示的是完整多 CLI 调度:

```python
runtime = Runtime(id="rt-1", name="local", clis=PathDetector.detect())

agents = [
    Agent(id="agent-1", name="Claude agent", cli="claude", runtime_id=runtime.id),
    Agent(id="agent-2", name="Codex agent", cli="codex", runtime_id=runtime.id),
]

runners = MultiCliRunner()
runners.register("claude", StubRunner())
runners.register("codex", StubRunner())

daemon = Daemon(
    issue_store,
    task_store,
    StubRunner(),
    runtime_id=runtime.id,
    agents=agents,
    multi_runner=runners,
)
```

然后入队两个 task:

```python
task_store.enqueue(issue1.id, "agent", "agent-1", runtime_id=runtime.id)
task_store.enqueue(issue2.id, "agent", "agent-2", runtime_id=runtime.id)
```

执行路径是:

```text
Daemon.run_once()
   |
   |-- claim runtime_id == rt-1 的 task
   |-- task.assignee_id=agent-1 -> agent.cli=claude -> claude runner
   |-- task.assignee_id=agent-2 -> agent.cli=codex  -> codex runner
```

demo 用 `StubRunner` 注册到两个 CLI 名下,是为了让示例稳定可跑;真实接入时可以把 `"claude"` 换成 `ClaudeCliRunner()`,把 `"codex"` 换成对应的 Codex runner。

## 10. 测试覆盖

运行:

```bash
python -m pytest tests/test_multicli.py -q
```

覆盖点包括:

```text
PathDetector.detect / is_available
Runtime 保存 clis
MultiCliRunner register / has / get / available
1:N daemon 能认领多个 agent
member task 不会被认领
同 runtime 双 daemon 抢同一 task 只有一个成功
daemon 只 claim 自己 runtime_id 的 task
daemon 按 agent.cli 分派到不同 runner
缺失 cli runner 时 fail-closed,issue 进入 blocked
```

这些测试把 s10 的重点从“结构存在”推进到“调度行为正确”。

## 11. 和 multiCa 的区别

s10 对应了 multiCa 生产调度里的几条主线:

```text
task 绑定 runtime_id
runtime claim 有边界
provider/cli 决定 runner
缺 runner fail-closed
```

它和 multiCa 的生产实现主要差在这些地方。

先把三个词分清楚:

```text
workspace  = 协作空间 / 租户边界
runtime    = 某个 daemon 注册出来的执行端记录
provider   = 这个执行端使用哪类后端,例如 claude / codex / opencode
```

所以“runtime 挂在 workspace A 下”不是说 `claude` 或 `codex` 这个 CLI 二进制文件属于 workspace A,而是说数据库里的这条 runtime 记录属于 workspace A。它表达的是:

```text
workspace A 里有一个可用执行端
这个执行端由某个 daemon 上报
它的 provider 可能是 claude,也可能是 codex
它只能 claim workspace A 授权范围内的 task
```

同一个用户可以加入多个 workspace;一个 workspace 下面可以有多个 issue、agent、runtime、skill、squad 等资源。`issue` 是任务/工单,不是 `issuer`。

### 11.1 runtime 注册

mica:

```python
Runtime(id="rt-1", name="local", clis=["claude", "codex"])
```

multiCa:

```text
daemon 启动后注册 runtime
runtime 写入 agent_runtime
包含 workspace_id / daemon_id / provider / runtime_mode / status / device_info
```

mica 的 runtime 只是内存对象;multiCa 的 runtime 是可鉴权、可审计、可下线、可重新连接的数据库实体。

### 11.2 claim 授权

mica:

```text
task.runtime_id == daemon.runtime_id
```

multiCa:

```text
ClaimTaskForRuntime(runtime_id)
ClaimTasksForRuntimes([runtime_id...], max_tasks)
```

生产 claim 还要检查:

```text
runtime 记录是否挂在请求允许访问的 workspace 下
runtime 是否由发起 claim 的 daemon 管理
workspace 是否在 token 权限内
task.workspace_id 是否和 runtime.workspace_id 一致
task.RuntimeID 是否在授权列表里
```

mica 只做 runtime_id 边界,没有 workspace 隔离和 token 授权。

### 11.3 provider backend

mica:

```text
agent.cli -> MultiCliRunner -> runner.run(...)
```

multiCa:

```text
runtime.provider -> provider backend
```

不同 backend 有自己的命令参数、session 恢复、stream parser、MCP 注入、sandbox/env 处理。s10 只保留统一的 `AgentRunner` 接口,没有实现各 provider 的真实差异。

### 11.4 CLI/profile 失败

mica:

```text
PathDetector 未发现 CLI -> 不出现在 clis 列表
执行时 runner 未注册 -> task failed
```

multiCa:

```text
runtime profile 解析失败 -> runtime row 标记 offline/failed
metadata 记录原因
UI/运维能看到“应该存在但启动失败”的 runtime
```

mica 只表达执行时失败;runtime 生命周期管理属于生产系统范围。

### 11.5 对照表

| 维度 | mica s10 | multiCa 生产实现 |
|---|---|---|
| CLI 发现 | `shutil.which` | daemon/profile 上报 provider |
| runtime 存储 | 内存 `Runtime` | 数据库 `agent_runtime` |
| task 归属 | `Task.runtime_id` | `agent_task_queue.runtime_id` |
| claim 边界 | `task.runtime_id == daemon.runtime_id` | runtime + daemon + workspace + token 授权 |
| agent 分派 | `agent.cli` | runtime/provider/backend |
| runner 缺失 | task failed | runtime failed/offline 或 task failed |
| workspace 隔离 | 未实现 | claim 后强校验 workspace |
| 多机器 | 只有概念 | daemon_id + runtime_id + heartbeat |

## 12. 本章边界

s10 本章实现:

```text
PATH CLI 探测
runtime 数据结构
task.runtime_id 持久化
runtime-scoped claim
agent registry
agent.cli -> runner dispatch
缺 agent/runner fail-closed
评论/手动重跑继承 runtime_id
```

s10 本章不展开:

```text
真实 Codex/OpenCode/Cursor runner
runtime 数据库存储
daemon heartbeat
workspace/token 授权
runtime offline/failed 状态
UI 上的 runtime 管理
provider-specific MCP/skills/sandbox 逻辑
```

一句话总结:s10 的核心是 runtime 级多 agent 调度。`runtime_id` 决定谁能 claim task,`agent.cli` 决定用哪个 runner 执行;鉴权、持久化、真实 provider backend 和多机器生命周期管理留到生产系统处理。
