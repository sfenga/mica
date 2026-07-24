# s02 — Task 实体 + Daemon 认领 task + StubRunner

s01 把 issue 看板项 + 7 态状态机立住了,但没人去执行——派给 agent 的 issue 永远停在 `todo`。s02 引入 **Task**(一次执行尝试)和 **TaskStore**(执行队列),daemon 从轮询 issue 改成**轮询 task 队列、CAS 认领**。一条 issue 可以有多个 task(1 issue : N tasks),task 生命周期驱动 issue 状态(running→in_progress, completed→in_review, failed→blocked)。这是新模型的核心:issue 是看板项,task 是执行单元,两者分层。

## 一、核心设计思想

第一个思想:**issue 和 task 是两个实体**。issue 是用户看到的看板项(状态反映整体生命周期);task 是一次 agent 执行尝试(有自己的 status/session_id)。一条 issue 对应多个 task——每跑一次多一行,旧的留作历史。

```python
@dataclass
class Task:
    id: str
    issue_id: str
    assignee_type: str = ""        # "agent" | "member"
    assignee_id: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    session_id: str = ""
    trigger_comment_id: str = ""   # 评论触发时指向评论
    rerun_of_task_id: str = ""     # 手动重跑时指向源 task
    prior_session_id: str = ""     # resume 用
    created_at: str = field(default_factory=now_iso)
    claimed_at: str = ""
    completed_at: str = ""
    error: str = ""
```

`status ∈ {queued, dispatched, running, completed, failed, cancelled}`:`queued`(已入队未认领)→ `dispatched`(daemon 认领)→ `running`(开始执行)→ `completed`/`failed`。这与 issue 的 7 态独立:issue 状态是板子生命周期,task 状态是执行生命周期。

第二个思想:**daemon 认领 task,不认领 issue**。daemon 轮询 task 队列中派给本 agent 且 `queued` 的 task,CAS 原子认领:

```python
def claim(self, task_id: str) -> bool:
    """CAS:仅当 status==queued 才置 dispatched。"""
    with self._lock:
        task = self._tasks.get(task_id)
        if task is None or task.status != TaskStatus.QUEUED:
            return False
        task.status = TaskStatus.DISPATCHED
        task.claimed_at = now_iso()
        return True
```

CAS 保证多 daemon 抢同一 task 只有一个赢(和 s01 的 issue CAS 同理,只是从 issue 维度换到 task 维度)。daemon 不直接碰 issue——它认领 task、执行 task,然后让 task 的结果驱动 issue 状态。

第三个思想:**task 生命周期驱动 issue 状态**。task running → issue `in_progress`;task completed → issue `in_review`(不直接 done,等评审);task failed → issue `blocked`。这把两个状态机耦合起来——task 的执行推进 issue 在板子上的位置:

```python
def _sync_issue_status(self, task, running=False, completed=False, failed=False):
    issue = self.issue_store.get(task.issue_id)
    if issue is None:
        return
    if running:
        if issue.status == IssueStatus.BLOCKED:
            self.issue_store.transition(issue.id, IssueStatus.TODO)
            self.issue_store.transition(issue.id, IssueStatus.IN_PROGRESS)  # 两步:blocked→todo→in_progress
        elif issue.status in (IssueStatus.TODO, IssueStatus.IN_REVIEW):
            self.issue_store.transition(issue.id, IssueStatus.IN_PROGRESS)
    elif completed and issue.status == IssueStatus.IN_PROGRESS:
        self.issue_store.transition(issue.id, IssueStatus.IN_REVIEW)
    elif failed and issue.status == IssueStatus.IN_PROGRESS:
        issue.error = task.error
        self.issue_store.transition(issue.id, IssueStatus.BLOCKED)
```

注意 `completed → in_review` 而非 `done`:agent 跑完只是"提交评审",不是"终态完成"——评审通过才 `in_review → done`。这让 review 环节在状态机里自然存在(对应 multiCa 的 `in_review` 评审态)。`blocked` issue 上新 task 重跑时,走 `blocked→todo→in_progress` 两步(状态机不允许 `blocked→in_progress` 直跳)。

第四个思想:**执行器可替换,StubRunner 是占位实现**。Daemon 不直接调 claude——它通过 `AgentRunner` Protocol 调 `runner.run(task, issue, ...)`:

```python
class AgentRunner(Protocol):
    def run(self, task, issue, context, on_event,
            resume_session_id=None) -> RunResult: ...
```

任何符合这个签名的执行器都能插进 daemon。s02 自带 `StubRunner`,它不 spawn 进程,只发合成事件 + 返回 done:

```python
class StubRunner:
    def run(self, task, issue, context, on_event, resume_session_id=None):
        on_event({"type": "progress", "task_id": task.id,
                  "message": f"stub: start '{issue.title}'"})
        on_event({"type": "progress", "task_id": task.id, "message": "stub: working..."})
        on_event({"type": "done", "task_id": task.id,
                  "output": f"stub completed: {issue.title}"})
        return {"status": "done", "output": f"stub completed: {issue.title}",
                "exit_code": 0, "session_id": ""}
```

它"隔离"的是:认领(CAS queued→dispatched)→ 启动(dispatched→running)→ 执行(`runner.run`)→ 标终态(complete/fail)→ 驱动 issue 状态——这条链路的验证不依赖 claude 是否安装/能用。daemon 对 runner 的调用就是 `runner.run(...)`,StubRunner 满足契约就跑通全链路;真实 runner 也满足同一契约,daemon 一行不改。认领机制和具体执行器解耦,各测各的。

## 二、为什么这么设计

### 为什么认领 task 而不是 issue?

issue 是看板项(用户产物),它的高层状态(todo/in_progress/in_review...)反映整体生命周期。如果 daemon 直接认领 issue(把 issue 状态 todo→in_progress),那一条 issue 同时只能跑一次,没法表达"同一条 issue 多次执行"(评论续聊、手动重跑)。引入 task 层:每次执行是一条 task 行,daemon 认领 task;issue 状态由 task 结果驱动。这样"一条 issue 有 N 次执行"天然成立(旧 task 留历史,新 task 入队),对应生产 multiCa 的 issue + agent_task_queue 两层。

### 为什么 task completed → issue in_review 而不是 done?

agent 跑完不等于任务完成——产出的成果可能需要人评审。`completed → in_review` 把评审环节留出来:agent 提交"我做完了",人评审通过才 `in_review → done`。这比 `completed → done` 更真实(代码评审是独立环节),也让 review 状态在状态机里有实际触发路径。如果不需要评审(小改动),s01 状态机也允许 `in_progress → done` 直接跳过。

### 为什么用 StubRunner 验证链路?

认领(CAS queued→dispatched)、启动(dispatched→running)、标终态(completed/failed)、驱动 issue 状态——这条链路是 s02 要验证的机制,不依赖 claude 是否安装/能用。StubRunner 发合成事件(progress + done),让链路在没有 claude 的情况下跑通、可单测。真实 runner 不在本章范围,daemon 代码不变(AgentRunner Protocol)。

### 为什么 task 状态机和 issue 状态机独立?

issue 状态(backlog/todo/in_progress/in_review/done/blocked/cancelled)是板子生命周期;task 状态(queued/dispatched/running/completed/failed/cancelled)是执行生命周期。它们耦合但不同:一条 issue 的 in_progress 可能对应多个 task(第一次跑 failed→issue blocked,评论触发新 task→issue 重回 in_progress→todo...)。独立状态机让两者各自清晰,通过 `_sync_issue_status` 显式耦合。

## 三、认领执行怎么流转

```
  为 issue 入队 task(enqueue,assignee 取自 issue)
        │  task.status = queued
        ▼
  daemon 轮询 task 队列: assignee==me && queued
        │  CAS: queued -> dispatched(只一个 daemon 赢)
        │  start: dispatched -> running
        │  issue: todo/in_review/blocked -> in_progress
        ▼
  AgentRunner.run(task, issue, ...)
        ├─ StubRunner: 合成 progress + done
        └─ (StubRunner 占位)
        │
        ▼
  done  → task completed, issue -> in_review
  fail  → task failed,    issue -> blocked + error
```

旧 task 行(completed/failed)保留;新 task 入队(评论触发或手动重跑)。一条 issue 的多次执行 = task 表里多行。

## 试一试

```bash
python sessions/zh/s02_task_daemon.py          # 看 daemon 认领 task + task 驱动 issue 状态
python -m pytest tests/test_task_claim.py -v   # CAS + task→issue 状态 + member 不被认领 + 一 issue 多 task
```

demo:为 issue "修登录 bug" 入队一条 task → daemon 认领 → StubRunner 执行 → task completed → issue `todo → in_progress → in_review`:

```
--- 派活前 ---
ID            STATUS       ASSIGNEE                TITLE
...           todo         agent:agent-1           修登录 bug
[daemon] claimed task ... for issue ...
[event] {'type': 'progress', ...}
[event] {'type': 'done', ...}
--- 认领执行后 ---
ID            STATUS       ASSIGNEE                TITLE
...           in_review    agent:agent-1           修登录 bug
```

注意 issue 从 `todo` 经过 `in_progress` 到 `in_review`(没直接 done)——agent 跑完只是提交评审。

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s02 |
|---|---|---|
| 执行单元 | agent_task_queue 行 | Task(dataclass + TaskStore) |
| task 状态 | queued/dispatched/running/completed/failed/cancelled + waiting_local_directory/deferred | queued/dispatched/running/completed/failed/cancelled(精简) |
| daemon 认领 | poll task queue + CAS ClaimAgentTask(queued→dispatched) | 同(TaskStore.claim CAS) |
| task→issue 耦合 | task running→issue in_progress;completed→in_review;failed→blocked | **完全一致** |
| 执行器 | claude 长开 stdin stream-json | StubRunner(合成事件) |
| session_id | task 行 session_id 列,完成时写回 | Task.session_id(StubRunner 留空) |
| 评论三路分流(合并/暂存/新建) | 有 | 简化:始终新建 queued task |

一句话:**s02 把 multiCa「task 队列 + daemon 认领 task + task 驱动 issue 状态」做实了——issue/task 分层,daemon 认领 task 不碰 issue,task 生命周期驱动 issue 在板子上流转**;StubRunner 是执行器占位,真实 runner 接入时 daemon 不变。
