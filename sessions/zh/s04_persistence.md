# s04 — JSONL 持久化与重启恢复

s01 到 s03 都有一个共同限制:数据只在内存里。

```text
IssueStore      dict
TaskStore       dict
runner events   只打印
```

进程一退出,看板状态、task 历史、执行过程全部消失。s04 解决这个问题:**把 issue、task、session events 都写到磁盘,重启时再重放回来**。

本章新增三类文件:

```text
workspace/issues.jsonl              issue 快照
workspace/tasks.jsonl               task 快照
workspace/sessions/<task_id>.jsonl  执行事件流
```

从这一章开始,mica 经得起重启。

## 1. 三类数据回答三个问题

不要把所有东西塞进一个文件。issue、task、session event 不是同一种数据。

```text
issue     这张看板卡现在是什么状态?
task      这张卡执行过几次,每次是什么结果?
session   某次执行过程中发生了什么?
```

所以 s04 分成三个落盘位置:

```text
issues.jsonl              看板项状态
tasks.jsonl               执行尝试状态
sessions/<task_id>.jsonl  执行过程事件
```

它们的写入粒度也不同:

```text
issue/task   每次状态变化,追加整条快照
session      每个事件追加一行
```

## 2. issue/task 用快照追加

`IssueStore(data_dir=...)` 会把 issue 变更写到 `issues.jsonl`:

```python
def _persist(self, issue: Issue) -> None:
    if not self._path:
        return
    with open(self._path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_issue_to_dict(issue), ensure_ascii=False) + "\n")
```

task 同理写到 `tasks.jsonl`。

一次 task 从 queued 跑到 completed 后,文件大概长这样:

```text
issues.jsonl
{"id":"i1","status":"todo",...}
{"id":"i1","status":"in_progress",...}
{"id":"i1","status":"in_review",...}

tasks.jsonl
{"id":"t1","issue_id":"i1","status":"queued",...}
{"id":"t1","issue_id":"i1","status":"dispatched",...}
{"id":"t1","issue_id":"i1","status":"running",...}
{"id":"t1","issue_id":"i1","status":"completed",...}
```

每一行都是“整条记录当前长什么样”,不是“发生了某个差量事件”。

## 3. 重放用 last-wins

既然每次都写整条快照,重启时就不需要复杂事件溯源。逐行读,同 id 后来的覆盖前面的:

```python
self._issues[rec["id"]] = _issue_from_dict(rec)
```

最后留在 dict 里的就是当前状态。

这叫 last-wins:

```text
同一个 issue/task 出现多行
越靠后的行越新
重放时最后一行胜出
```

优点是简单。缺点是文件会重复存很多不变字段。mica 是教学仓库,选择简单优先。

生产 multiCa 用 PostgreSQL 表直接保存当前状态,不是 JSONL 快照;但核心问题相同:重启后必须能恢复 issue/task 的当前状态。

## 4. session events 是另一条日志

issue/task 快照回答“现在状态是什么”,但不回答“执行时发生了什么”。

执行过程写到:

```text
workspace/sessions/<task_id>.jsonl
```

daemon 执行时会写:

```text
claim 事件
runner 发来的 progress/text/tool_use 等事件
最终 result 事件
```

`SessionStore.append()` 会给事件补 `ts`:

```python
def append(self, task_id: str, event: dict) -> None:
    ev = dict(event)
    ev.setdefault("ts", now_iso())
    with open(self._path(task_id), "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
```

示例:

```text
{"type":"claim","task_id":"t1","issue_id":"i1","agent_id":"agent-1","ts":"..."}
{"type":"progress","task_id":"t1","message":"stub: start '修登录 bug'","ts":"..."}
{"type":"progress","task_id":"t1","message":"stub: working...","ts":"..."}
{"type":"done","task_id":"t1","output":"stub completed: 修登录 bug","ts":"..."}
{"type":"result","task_id":"t1","status":"done","output":"stub completed: 修登录 bug","ts":"..."}
```

后面 s06 的实时推送不会替代这份历史。实时和历史是两条路径:

```text
历史: SessionStore JSONL
实时: 后续 s06 的实时通道
```

## 5. orphan: 重启时正在跑的 task 怎么办

如果进程在 task 执行中崩了,重启后可能看到:

```text
task.status = dispatched
task.status = running
```

但原来的 runner 进程已经不在了。这种 task 叫 orphan。

s04 的处理是:标 failed,不自动重派。

```python
def recover_orphans(self) -> None:
    for task in list(self._tasks.values()):
        if task.status in (TaskStatus.RUNNING, TaskStatus.DISPATCHED):
            task.status = TaskStatus.FAILED
            task.error = "interrupted by restart"
            task.completed_at = now_iso()
            self._persist(task)
```

为什么不自动重派?

```text
自动重派可能重复执行副作用
mica 没有退避/重试次数/幂等保护
failed 是可见终态,人可以决定是否新建 task
```

生产 multiCa 的恢复路径更复杂:它会按 runtime 失败 orphan,并把失败行交给后续 auto-retry 逻辑。mica s04 保留“重启不能假装还在跑”的核心,省掉自动重试。

## 6. session 太长时截断式 summarize

session events 可能越来越长。s04 不调用模型做摘要,只做机械截断:

```text
如果事件数 > threshold
保留最后 keep 条原文
前面的事件合成一条 summary 占位
```

代码形状:

```python
def summarize_if_needed(self, task_id: str, threshold: int = 100,
                        keep: int = 20) -> int:
    events = self.load(task_id)
    if len(events) <= threshold:
        return 0
    dropped = len(events) - keep
    recent = events[-keep:]
    summary = {
        "type": "summary",
        "task_id": task_id,
        "dropped_count": dropped,
        "kept": keep,
        "ts": now_iso(),
        "message": f"truncated {dropped} earlier events (keep last {keep})",
    }
    self._rewrite(task_id, [summary] + recent)
    return dropped
```

重写文件时先写 `.tmp`,再 `os.replace`,避免写到一半留下半个文件。

这不是智能摘要,只是可审计截断。读者至少能看到:

```text
这里曾经有 N 条更早事件被压掉了
最近 keep 条原文仍然保留
```

## 7. data_dir=None 仍是内存模式

为了不破坏前面章节的测试,`IssueStore()` / `TaskStore()` 默认仍然不落盘。

```text
data_dir=None     纯内存,和 s01-s03 一样
data_dir="..."    启用 JSONL 持久化
```

所以 s04 是 opt-in 增强。旧测试继续用内存,新测试用 `tmp_path` 验证持久化。

## 8. 整体链路

```text
创建 issue
   -> IssueStore._persist(issue)      issues.jsonl

入队 task / claim / start / complete / fail
   -> TaskStore._persist(task)        tasks.jsonl

daemon 执行 task
   -> SessionStore.append(claim)
   -> on_event(ev) -> SessionStore.append(ev)
   -> SessionStore.append(result)
   -> summarize_if_needed(task_id)

重启
   -> IssueStore._load() last-wins
   -> TaskStore._load() last-wins
   -> recover_orphans()
```

## 9. 试一试

运行 demo:

```bash
python sessions/zh/s04_persistence.py
```

demo 用 `StubRunner`,不依赖 `claude`。它会:

```text
创建 issue 和 task
daemon 执行
写入 issues/tasks/sessions
重新创建 Store 模拟重启
从 JSONL 恢复 board
```

运行测试:

```bash
python -m pytest tests/test_persistence.py tests/test_orphan_recovery.py -v
```

测试覆盖:

```text
issue 快照写入与重放
task 快照写入与重放
session event 写入与读取
daemon 记录 claim/result
summary 截断
data_dir=None 内存模式
running/dispatched orphan 恢复为 failed
orphan 恢复幂等
```

## 10. 本章边界

s04 做了:

```text
JSONL issue/task 快照
last-wins 重放
SessionStore 事件流
orphan -> failed
截断式 summary
```

s04 不做:

```text
PostgreSQL / 事务
自动重试 orphan
LLM 智能摘要
多进程写入协调
实时推送
```

一句话总结:**s04 把内存系统变成可重启系统。issue/task 用快照恢复当前状态,session events 保留执行历史,崩溃中的 task 明确失败而不是假装继续跑。**
