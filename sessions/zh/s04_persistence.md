# s04 — JSONL 持久化(issues+tasks+sessions)+ orphan 恢复 + 截断 summarize

前三章的 mica 一退出就什么都没了:IssueStore/TaskStore 是内存 `dict`,daemon 跑完进程一退,issue/task 状态、执行过程全丢。s04 给它加上持久化:issue/task 状态与 agent 会话事件都落 JSONL,重启重放重建,崩溃留下的 running/dispatched task 会被识别成 orphan 标 failed,会话太长截断式压缩。从这一章起,mica 经得起重启。

## 一、核心设计思想

第一个思想:**三个文件,各管一摊**。issue 状态落 `issues.jsonl`(快照追加 + last-wins 重放),task 行落 `tasks.jsonl`(同理),会话事件流落 `sessions/<task_id>.jsonl`(全保留 + 截断)。三者分开是因为它们回答不同的问题:issue 文件回答"这条看板项现在啥状态",task 文件回答"跑了几次、每次啥结果",session 文件回答"执行时发生了啥"。

### 三个文件长什么样(对照看)

跑一次 issue "修登录 bug"(StubRunner)后,三个文件并排:

`workspace/issues.jsonl`(issue 状态快照,每行一次变更后的整条 issue):
```
{"id":"67fa…","status":"todo",       "assignee_id":"agent-1","title":"修登录 bug",…}  ← 创建时
{"id":"67fa…","status":"in_progress", "assignee_id":"agent-1",…}                     ← task running
{"id":"67fa…","status":"in_review",  "assignee_id":"agent-1",…}                     ← task completed
```
重放取最后一条 → issue `in_review`。整条快照,不变字段每行重复(省空间 vs 重放简单的取舍)。

`workspace/tasks.jsonl`(task 状态快照,每行一次变更后的整条 task):
```
{"id":"t1…","issue_id":"67fa…","status":"queued",    "session_id":"",…}             ← 入队
{"id":"t1…","issue_id":"67fa…","status":"dispatched", "claimed_at":"…",…}           ← daemon claim
{"id":"t1…","issue_id":"67fa…","status":"running",    …}                             ← start
{"id":"t1…","issue_id":"67fa…","status":"completed",  "session_id":"ses-1","completed_at":"…",…}  ← 执行完成
```
重放取最后一条 → task `completed`、`session_id=ses-1`。一条 issue 可有多条 task(评论续聊、手动重跑),每条 task 自己的快照序列。

`workspace/sessions/t1….jsonl`(事件流,每行一个事件,带时间戳):
```
{"type":"claim",    "message":"claimed task for '修登录 bug'","ts":"…10.420"}
{"type":"progress", "task_id":"t1…","message":"stub: start '修登录 bug'","ts":"…10.545"}
{"type":"progress", "task_id":"t1…","message":"stub: working...","ts":"…10.599"}
{"type":"done",     "task_id":"t1…","output":"stub completed: 修登录 bug","ts":"…10.647"}
{"type":"result",   "status":"done","output":"stub completed: 修登录 bug","ts":"…10.743"}
```
全保留(只增不删,超长才截断式 summarize)。每条 task 一个 session 文件,key 是 task_id。

| 文件 | 记什么 | 粒度 | 重放 |
|---|---|---|---|
| `issues.jsonl` | issue 状态(看板项当前状态) | 每次变更一条**整条快照** | last-wins |
| `tasks.jsonl` | task 状态(执行尝试) | 每次变更一条**整条快照** | last-wins + orphan 恢复 |
| `sessions/<task_id>.jsonl` | 执行过程(事件流) | 每个事件**一条** | 全保留 + 截断 |

```python
# IssueStore / TaskStore 加 data_dir
class IssueStore:
    def __init__(self, data_dir=None):
        self._issues = {}
        self._lock = threading.Lock()
        self._path = None
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self._path = os.path.join(data_dir, "issues.jsonl")
            self._load()     # 重启重放
```

第二个思想:**快照追加 + last-wins 重放**。每次 issue/task 变更都把整条 issue/task 追加到 JSONL。重放时逐行读,后写的覆盖先写的——`self._issues[rec["id"]] = _issue_from_dict(rec)`,最后一条就是当前状态。一行代码搞定重放,不用 fold、不用按序应用逻辑。代价是文件累积历史快照(不变字段每行重复),在 mica 规模下可接受。

```python
def _persist(self, issue):
    if not self._path:
        return
    with open(self._path, "a") as f:
        f.write(json.dumps(_issue_to_dict(issue), ensure_ascii=False) + "\n")
```

第三个思想:**orphan → failed,不自动重派**。重启重放后,如果有 task 处于 `running` 或 `dispatched`(说明上次崩在执行中途),这是孤儿——没有运行中的 runner。mica 把它标 `failed` + 记 error,不自动重派:

```python
def recover_orphans(self):
    for task in list(self._tasks.values()):
        if task.status in (TaskStatus.RUNNING, TaskStatus.DISPATCHED):
            task.status = TaskStatus.FAILED
            task.error = "interrupted by restart"
            task.completed_at = now_iso()
            self._persist(task)
```

不自动重派是有意的:自动重派可能让一条坏任务无限重跑,且 mica 没有重试/退避策略。标 failed 让人看见、决定新建 task 重来。`_load` 读完所有快照后调 `recover_orphans()`,所以一加载就干净。

第四个思想:**截断式 summarize**。会话事件流太长时,保留最近 N 条原文,旧事件合并成一条 `{type:"summary", dropped_count, range}` 占位,用 tmp + `os.replace` 原子重写:

```python
def summarize_if_needed(self, task_id, threshold=100, keep=20):
    if keep <= 0:
        raise ValueError("keep must be >= 1")
    events = self.load(task_id)
    if len(events) <= threshold:
        return False
    old, recent = events[:-keep], events[-keep:]
    summary = {"type": "summary", "dropped_count": len(old), ...}
    self._rewrite(task_id, [summary] + recent)
    return True
```

留一条 `summary` 占位而非直接删,让截断可审计(读会话的人看得到"这里曾有一批事件被压掉了、压掉多少")。

## 二、为什么这么设计

### 为什么 issue 和 task 用快照追加,不用事件溯源?

事件溯源(记 `created`/`transitioned` 事件再 fold 成状态)更省空间,但重放要按序回放所有事件、维护 fold 逻辑,复杂度高。快照追加把重放降成"取最后一条",一行赋值搞定。mica 规模小,空间浪费无所谓,重放简单最要紧。

### 为什么 orphan 标 failed 不重派?

自动重派可能让一条因崩溃中断的任务无限重跑(mica 没有重试次数/退避)。`failed` 是可观察终态,人能看见"这条崩过",新建 task 重来是显式决定。`running/dispatched→failed` 是终态转移(合法),走的是同一条状态机。

### 为什么三个文件分开?

issue 状态("现在啥状态")、task 行("跑了几次")、session 事件("执行时发生了啥")是三种不同的数据,各自的重放/压缩策略不同。issue/task 用 last-wins 快照;session 用全保留 + 截断。塞一个文件会让重放逻辑混着判"这条是状态快照还是事件"。

### 为什么 `data_dir=None` 时退化为纯内存?

s01–s03 的所有测试都用 `IssueStore()` / `TaskStore()`(无参)在内存里跑,不碰磁盘。s04 给 `__init__` 加 `data_dir=None` 默认值:`None` 时 `_path=None`,`_load`/`_persist` 都是 no-op,行为和 s03 完全一致。已有测试不受影响,持久化是 opt-in。

## 三、状态与数据怎么流转

```
  daemon 变更 issue/task
        │
        ▼
  IssueStore._persist(issue)  ──► issues.jsonl  (追加快照)
  TaskStore._persist(task)   ──► tasks.jsonl   (追加快照)
        │
  daemon 执行 task:
        │  claim → SessionStore.append(claim)
        │  runner 每个事件 → on_event → SessionStore.append(事件)
        │  终态 → SessionStore.append(result)
        ▼
  SessionStore.summarize_if_needed(task_id):
        │  事件数 > threshold → 保留最近 keep 条 + 1 条 summary,原子重写
        ▼
  sessions/<task_id>.jsonl
  ─── 重启 ───
  IssueStore(data_dir)._load() → last-wins 重放 issue 状态
  TaskStore(data_dir)._load() → last-wins 重放 task 状态 + recover_orphans()
```

## 试一试

```bash
python sessions/zh/s04_persistence.py              # demo:StubRunner 跑通持久化 + 重放(不调 claude)
python -m pytest tests/test_persistence.py tests/test_orphan_recovery.py -v
```

demo 用 StubRunner(确定性,无需 claude):执行后 issue 变 in_review 并落盘,会话事件流打印出来(claim/progress/result);再建一个 `IssueStore(data_dir)` 模拟重启,board 仍显示 in_review——状态从 JSONL 重放回来了。orphan 恢复由单测覆盖(写一条 running task、不执行、重放 → failed)。

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s04 |
|---|---|---|
| 存储 | PostgreSQL 17 + pgvector,sqlc 生成 | JSONL 文件,零依赖 |
| issue/task 持久化 | 行级 upsert,状态即列 | 快照追加,last-wins 重放 |
| 会话/transcript | DB 表 / 独立存储 | 每 task 一个 `sessions/<task_id>.jsonl` |
| 崩溃恢复 | `RecoverOrphans`:dispatched/running 孤儿 fail-and-retry | running/dispatched 无 runner → failed + error,不自动重派 |
| 上下文压缩 | LLM 摘要 / 复杂压缩管线 | 截断式:留近 N 条 + 1 条 summary 占位 |
| 原子写 | DB 事务 | tmp + `os.replace` |

一句话:**s04 把 multiCa「落盘 + 重启重放 + 崩溃恢复 + 上下文压缩」做实了,但用 JSONL 而非 PostgreSQL、用截断而非 LLM 摘要、orphan 只标终态不重派**——机制形态对齐,实现选了最简的路径。
