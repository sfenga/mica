# s05 — 评论触发新 task 与 `--resume`

s04 让系统能重启恢复。现在还有一个协作问题:

```text
agent 跑完后,issue 进入 in_review
用户在评论里补一句:“再处理一下 X”
系统应该怎么让 agent 接着干?
```

一个坏方案是把 issue 从 `in_review` 拨回 `todo`。这会让看板状态乱跳:用户只是补充了一条评论,不代表整张卡要被重新打开。

s05 的方案是:

```text
评论不改变 issue 状态
评论会入队一条新的 task
daemon 认领新 task 时,用上一条 task.session_id 调 claude --resume
```

也就是说,评论触发的是“新的执行尝试”,不是“重写 issue 状态”。

## 1. 评论只追加,不改状态

s05 给 `Issue` 增加 `comments`:

```python
comments: list = field(default_factory=list)
```

`IssueStore.comment()` 只做追加:

```python
def comment(self, issue_id: str, text: str) -> Issue:
    issue = self._require(issue_id)
    issue.comments.append(text)
    issue.updated_at = now_iso()
    self._persist(issue)
    return issue
```

注意:这里没有 `transition()`。

所以:

```text
in_review issue 收到评论后,仍然是 in_review
blocked issue 收到评论后,仍然是 blocked
done issue 收到评论后,仍然是 done
```

评论本身不是状态转移。状态仍由人或 task 生命周期推动。

## 2. 评论会入队新 task

评论后想让 agent 行动,需要显式入队 task:

```python
def comment_and_enqueue(issue_store: IssueStore, task_store: "TaskStore",
                        issue_id: str, text: str) -> Task:
    issue = issue_store.comment(issue_id, text)
    return task_store.enqueue(issue.id, issue.assignee_type, issue.assignee_id,
                              trigger_comment_id=text)
```

这条 task 和首次执行的 task 一样,都是 `queued`。差别是它记录了:

```text
trigger_comment_id = 评论文本
```

在生产 multiCa 里,`trigger_comment_id` 是 comment 表里的 UUID。mica 为了单文件教学,直接把评论文本放进去。语义是一样的:这次执行是由哪条评论触发的。

## 3. 为什么是新 task,不是改旧 task

旧 task 是历史:

```text
第一次执行 task    completed, session_id=s1
评论触发 task      queued
再次执行后         completed, session_id=s1 或新 session
```

如果在旧 task 上“继续跑”,就看不出第一次执行什么时候结束、第二次执行什么时候开始。新 task 保留了执行历史,也给每次执行自己的状态、时间、错误和 session_id。

这就是 issue/task 分层的价值:

```text
issue 仍是一张卡
task 记录每一次 agent 尝试
```

## 4. daemon 认领时查上一条 session

daemon 认领一条 task 后,会查同一条 issue、同一个 agent 的上一条已完成 task:

```python
def _find_prior_session_id(self, task: Task) -> str:
    all_tasks = self.task_store.list(task.issue_id)
    prior = [t for t in all_tasks
             if t.id != task.id
             and t.assignee_type == task.assignee_type
             and t.assignee_id == task.assignee_id
             and t.status == TaskStatus.COMPLETED
             and t.session_id]
    if prior:
        return prior[-1].session_id
    return ""
```

如果找到了,就传给 runner:

```python
result = self.runner.run(task, issue, [], on_event,
                         resume_session_id=prior_sid or None)
```

`ClaudeCliRunner._cmd()` 会加:

```text
--resume <prior_session_id>
```

这让 Claude CLI 载入上一轮会话,把新评论作为新的 user turn 接着处理。

生产 multiCa 的 claim handler 也会解析 prior session/workdir。真实系统更复杂:manual rerun 会优先从指定源 task 恢复,普通 follow-up 才查最近的 `(agent, issue)` session,并且会过滤“有毒”的失败会话。mica s05 保留最小规则:找上一条 completed task 的 session_id。

## 5. resume prompt 必须带原任务和评论

`ClaudeCliRunner.run()` 的 prompt 始终从 issue 本体开始:

```python
prompt = issue.title + (f"\n\n{issue.body}" if issue.body else "")
if resume_session_id and task.trigger_comment_id:
    prompt += f"\n\n[Follow-up] {task.trigger_comment_id}"
```

也就是说,评论续聊时的 prompt 是:

```text
issue.title

issue.body

[Follow-up] 评论内容
```

不是只有评论内容。

为什么?因为 `--resume` 可能没接上。如果只发评论,一旦 resume 失效,Claude 会开一个全新会话,只看到一句“再处理一下 X”,没有原任务上下文。带上 title/body 后,即使降级成新会话,它仍然知道原任务是什么。

生产 multiCa 的 prompt 构造更依赖系统内工具和上下文读取;mica 没有完整 multica CLI,所以把必要上下文直接放进 prompt。

## 6. resume 没接上怎么办

`--resume <old>` 不保证一定成功。Claude 可能返回一个新的 `session_id`。

mica 的规则是:

```text
RunResult.session_id 有值 -> 覆盖当前 task.session_id
```

代码:

```python
if result.get("session_id"):
    self.task_store.set_session_id(task.id, result["session_id"])
```

两种情况:

```text
resume 成功: 返回还是 old id,覆盖是 no-op
resume 失败: 返回 new id,当前 task 记录 new id
```

这样下次再续聊时,会从最新可用 session 继续,不会一直拿失效 id 重试。

## 7. 手动重跑也是新 task

手动 rerun 不会修改旧 task,而是新建 task:

```python
def rerun_task(self, source_task_id: str) -> Task:
    source = self._require(source_task_id)
    return self.enqueue(source.issue_id, source.assignee_type, source.assignee_id,
                        rerun_of_task_id=source.id)
```

`rerun_of_task_id` 保存来源:

```text
旧 task     failed/completed,不动
新 task     queued,rerun_of_task_id=旧 task id
```

这和评论触发一样:每次执行都是新 task,历史不被覆盖。

## 8. 整体链路

```text
首次执行
   |
   |-- task t1 queued -> running -> completed
   |-- Claude 返回 session_id=s1
   |-- issue -> in_review
   |
用户评论 "再处理一下 X"
   |
   |-- issue.comments 追加文本
   |-- issue.status 不变
   |-- 新 task t2 queued, trigger_comment_id="再处理一下 X"
   |
daemon 认领 t2
   |
   |-- _find_prior_session_id(t2) -> s1
   |-- ClaudeCliRunner.run(resume_session_id=s1)
   |-- prompt = title + body + [Follow-up] 评论
   |
Claude 执行
   |
   |-- resume 成功: session_id 仍是 s1
   |-- resume 失败: session_id 变成 s2
   |
task t2 completed
issue -> in_review
```

这条链路有一个核心约束:

```text
评论不直接改 issue 状态
```

issue 状态只会在新 task 真正 running/completed/failed 时被 daemon 推动。

## 9. 和生产 multiCa 的关系

从生产代码看,真实评论分流比 mica 更复杂:

```text
如果已有 queued/dispatched task,会尝试把评论合并进未开始的 task
如果已有 active task,会 defer,让完成后的 reconcile 覆盖评论
如果没有可覆盖的 task,才入队新的 task
manual rerun 会精确从源 task 恢复,避免并行 task 抢错 session
失败会话会按 failure_reason 过滤,避免 resume 到有毒上下文
```

mica s05 把这些生产分支折叠成一条教学规则:

```text
每条评论 -> 新建 queued task
新 task -> resume 上一条 completed task.session_id
```

这样能先看懂最重要的模型:评论不是 reopen issue,而是触发一次新的 agent 执行。

## 10. 试一试

单测不调真 `claude`:

```bash
python -m pytest tests/test_comment_resume.py -v
```

测试覆盖:

```text
评论追加且不改变 issue 状态
comment_and_enqueue 创建 queued task
daemon 找上一条 session_id 并传 resume
resume 返回新 session 时覆盖当前 task
无 prior task 时不传 resume
rerun 创建新 task,保留 rerun_of_task_id
resume prompt 包含 title 和 follow-up 评论
```

demo 会调真实 CLI:

```bash
python sessions/zh/s05_comment_resume.py
```

需要本机已安装并登录 `claude`。如果没装,runner 会返回 failed,task 标 failed,issue 标 blocked。

## 11. 本章边界

s05 做了:

```text
issue comments
评论入队新 task
查上一条 task.session_id
claude --resume
resume 失效时记录新 session_id
手动 rerun 新建 task
```

s05 不做:

```text
评论合并进 pending task
active task 完成后 reconcile 评论
有毒 session 过滤
workdir 复用
多 agent/squad 评论路由
```

一句话总结:**s05 把“用户追评后让 agent 接着干”接起来。评论不改 issue 状态,而是制造下一次 task;session_id 把多次执行串成一条会话。**
