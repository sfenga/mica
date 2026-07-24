# s02 — Task 队列与 Daemon 认领

s01 只有 issue 看板项。问题是:一张派给 agent 的 issue 放在 `todo`,谁去执行它?

一个直觉方案是让 daemon 直接认领 issue。但这会很快卡住:同一张 issue 以后可能因为评论、重跑、失败恢复产生多次执行。如果 issue 自己就是执行单元,历史会被覆盖。

s02 引入一层新的实体:**Task**。

```text
Issue   用户看见的看板项,描述目标和整体生命周期
Task    agent 的一次执行尝试,描述这次跑到哪、结果是什么
```

从这一章开始,主链路变成:

```text
issue -> enqueue task -> daemon claim task -> runner run task -> task result drives issue
```

## 1. 为什么要有 Task

一条 issue 可以执行多次:

```text
第一次执行
评论后继续执行
失败后重跑
手动 rerun
```

所以 task 必须是独立记录:

```python
@dataclass
class Task:
    id: str
    issue_id: str
    assignee_type: str = ""
    assignee_id: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    session_id: str = ""
    trigger_comment_id: str = ""
    rerun_of_task_id: str = ""
    prior_session_id: str = ""
    created_at: str = field(default_factory=now_iso)
    claimed_at: str = ""
    completed_at: str = ""
    error: str = ""
```

s02 暂时只用到核心字段:

```text
issue_id       这次执行属于哪张 issue
assignee_*     这次执行派给哪个 agent/member
status         task 自己的生命周期
session_id     以后接真实 CLI 会话时使用
error          失败原因
```

生产 multiCa 里对应的是 `agent_task_queue` 行。初始迁移中它同样有 `queued/dispatched/running/completed/failed/cancelled` 这些状态。mica 把它压缩成一个 dataclass 和内存 `TaskStore`。

## 2. Task 有自己的状态机

task 状态和 issue 状态不是一回事:

```text
IssueStatus: backlog / todo / in_progress / in_review / done / blocked / cancelled
TaskStatus:  queued / dispatched / running / completed / failed / cancelled
```

task 的正常路径:

```text
queued -> dispatched -> running -> completed
                               `-> failed
```

含义:

```text
queued       已入队,还没人拿
dispatched   daemon 已认领
running      runner 已开始执行
completed    这次执行成功结束
failed       这次执行失败
cancelled    这次执行取消
```

issue 是“这张卡整体到哪了”,task 是“这一次执行到哪了”。两者分开以后,一张 issue 可以保留 N 条 task 历史。

## 3. Daemon 认领的是 task,不是 issue

daemon 每次从 `TaskStore` 找:

```text
assignee_type == "agent"
assignee_id == 当前 daemon 的 agent.id
status == queued
```

找到后用 CAS 认领:

```python
def claim(self, task_id: str) -> bool:
    with self._lock:
        task = self._tasks.get(task_id)
        if task is None or task.status != TaskStatus.QUEUED:
            return False
        task.status = TaskStatus.DISPATCHED
        task.claimed_at = now_iso()
        return True
```

CAS 的意思是:只有当前仍然是 `queued`,才允许改成 `dispatched`。如果两个 daemon 同时抢同一个 task,只有一个会成功。

生产 multiCa 的 `ClaimAgentTask` SQL 也是这个思想,只是更完整:它用 `FOR UPDATE SKIP LOCKED`,并且还会避免同一个 agent 同时跑同一 issue 的多个 active task。mica s02 用锁和状态检查保留最小核心。

## 4. Task 结果驱动 issue 状态

daemon 执行 task 后,会把结果同步回 issue:

```text
task running     -> issue in_progress
task completed   -> issue in_review
task failed      -> issue blocked
```

代码入口是 `_sync_issue_status`:

```python
if running:
    if issue.status == IssueStatus.BLOCKED:
        self.issue_store.transition(issue.id, IssueStatus.TODO)
        self.issue_store.transition(issue.id, IssueStatus.IN_PROGRESS)
    elif issue.status in (IssueStatus.TODO, IssueStatus.IN_REVIEW):
        self.issue_store.transition(issue.id, IssueStatus.IN_PROGRESS)
elif completed and issue.status == IssueStatus.IN_PROGRESS:
    self.issue_store.transition(issue.id, IssueStatus.IN_REVIEW)
elif failed and issue.status == IssueStatus.IN_PROGRESS:
    issue.error = task.error
    self.issue_store.transition(issue.id, IssueStatus.BLOCKED)
```

这里有两个细节:

```text
completed -> in_review,不是 done
blocked 重跑时要 blocked -> todo -> in_progress,不能直跳
```

agent 跑完只是提交产物,人还要评审,所以进入 `in_review`。如果任务之前 blocked,状态机不允许 `blocked -> in_progress`,所以代码走两步。

## 5. StubRunner: 先验证认领链路

s02 不接真实模型。它引入 `AgentRunner` 协议:

```python
class AgentRunner(Protocol):
    def run(self, task, issue, context, on_event,
            resume_session_id=None) -> RunResult: ...
```

daemon 只依赖这个协议,不关心背后是真 CLI 还是假 runner。

本章用 `StubRunner`:

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

这样可以先验证:

```text
enqueue -> claim -> start -> runner.run -> complete/fail -> issue status
```

这条链路不依赖 `claude` 是否安装,也不会产生模型调用成本。s03 再把 StubRunner 换成真实 `ClaudeCliRunner`。

## 6. 执行链路

```text
IssueStore.create(...)
   |
   v
TaskStore.enqueue(issue.id, issue.assignee_type, issue.assignee_id)
   |
   v
Daemon.claim_next()
   |
   |-- TaskStore.claim(): queued -> dispatched
   |-- TaskStore.start(): dispatched -> running
   |-- issue: todo/in_review/blocked -> in_progress
   |
   v
runner.run(task, issue, ...)
   |
   |-- done   -> task completed -> issue in_review
   `-- failed -> task failed    -> issue blocked
```

旧 task 不会被覆盖。新执行就新建一条 task。

## 7. 试一试

运行 demo:

```bash
python sessions/zh/s02_task_daemon.py
```

你会看到:

```text
派活前: issue = todo
daemon claimed task ...
StubRunner 发 progress/done 事件
执行后: task = completed, issue = in_review
```

运行测试:

```bash
python -m pytest tests/test_task_claim.py -v
```

测试覆盖:

```text
enqueue 创建 queued task
claim 改成 dispatched
双 daemon 抢同一 task 只有一个成功
StubRunner 完成后 issue 进 in_review
失败后 issue blocked
member 指派不会被 agent daemon 认领
一条 issue 可以有多条 task 历史
blocked 重跑走两步状态转移
```

## 8. 本章边界

s02 做了:

```text
Task 数据形状
TaskStore 执行队列
daemon CAS 认领 task
StubRunner 占位执行器
task 生命周期驱动 issue 状态
```

s02 不做:

```text
真实 claude CLI      s03
JSONL 持久化         s04
评论触发新 task      s05
实时推送             s06
```

一句话总结:**s02 把 issue 和执行尝试拆开。daemon 认领 task,task 结果再推动 issue,于是“一张卡多次执行”有了位置。**
