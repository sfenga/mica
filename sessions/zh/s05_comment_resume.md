# s05 — 评论建新 task + `--resume` 上一 task

s04 让 mica 经得起重启了,但还有个问题:用户在 issue 评论区补充信息后想让 agent 接着干,怎么办?如果直接 reopen issue(把 done/in_review 拨回 todo),板子上刚标完成的卡又跳回进行中,用户会困惑。s05 用生产 multiCa 的方式解决:**评论不改 issue 状态,而是入队一条新 queued task**;daemon 认领新 task 时,查同一条 issue 上一条 task 的 `session_id`,用 `claude -p --resume <id>` 让 claude 重新载入上次会话,新评论作为新 user turn 带历史继续。

## 一、核心设计思想

第一个思想:**评论 = 入队新 task,issue 状态不动**。评论只做两件事:追加到 issue 的 `comments` 列表;为 issue 的 assignee 入队一条新 `queued` task(`trigger_comment_id` 指向评论文本)。issue 状态**不变**——`in_review` 的 issue 评论后还是 `in_review`,只是 task 队列里多了一行:

```python
def comment(issue_store, task_store, issue_id, text):
    """评论:追加评论 + 入队新 queued task。issue 状态不变。"""
    issue = issue_store.comment(issue_id, text)     # 只追加 comments
    return task_store.enqueue(issue_id, issue.assignee_type, issue.assignee_id,
                             trigger_comment_id=text)   # 新 task
```

这和 s02 的「为 issue 入队 task」是同一个 TaskStore.enqueue,只是这次由评论触发。issue 不需要 reopen——它的状态是高层板子状态,不该因一句评论跳来跳去。

第二个思想:**resume 从上一 task 的 session_id**。daemon 认领一条 task 时,查同 `(issue, agent)` 的上一条 `completed` task 的 `session_id`:

```python
def _find_prior_session_id(self, task):
    all_tasks = self.task_store.list(task.issue_id)
    prior = [t for t in all_tasks
             if t.id != task.id
             and t.assignee_id == task.assignee_id
             and t.status == TaskStatus.COMPLETED
             and t.session_id]
    return prior[-1].session_id if prior else ""
```

如果有上一条,把它的 `session_id` 作为 `resume_session_id` 传给 `ClaudeCliRunner.run`——命令里会带 `--resume <id>`,claude 从磁盘重新载入上次会话,新评论作为新 user turn 接在历史之后。没有上一条(首次执行)则不传 `--resume`,全新会话。

第三个思想:**resume 时 prompt 始终含原任务 + 评论追加**。`ClaudeCliRunner.run` 始终以 `issue.title + body` 为基础;resume 时追加 `trigger_comment_id`(评论文本)作 `[Follow-up]`:

```python
def run(self, task, issue, context, on_event, resume_session_id=None):
    prompt = issue.title + (f"\n\n{issue.body}" if issue.body else "")
    if resume_session_id and task.trigger_comment_id:
        prompt += f"\n\n[Follow-up] {task.trigger_comment_id}"
```

为什么不是只发评论?因为 `--resume` 可能失效——如果只发评论、resume 没接上,claude 开了全新 session 但只有一句孤立评论,没有原任务上下文,不知道在干什么。始终带 title+body 保证即使 resume 失效,新 session 也有完整任务上下文 + 评论,能从头继续。生产 multiCa 不内联 issue 上下文,而是给 issue ID + 指引 agent 自己跑 `multica issue get` 拉上下文(`prompt.go:215`);mica 的 claude 没有 `multica` CLI,所以必须内联。

第四个思想:**resume 失效用新 id 覆盖**。`--resume` 不是百分百命中——如果 session 在 claude 那边已失效,claude 吐回一个全新的 `session_id`(不是传入的)。mica 的处理:始终用 `RunResult.session_id` 覆盖 task 的 `session_id`——命中时 claude 回吐同一个 id(覆盖是 no-op),失效时给新 id(覆盖成新 id):

```python
# Daemon.execute
result = self.runner.run(task, issue, [], on_event,
                         resume_session_id=prior_sid or None)
if result.get("session_id"):
    self.task_store.set_session_id(task.id, result["session_id"])
```

不沿用失效的旧 id,免得下次又 resume 不到。行为自愈,不卡流程。

## 二、为什么这么设计

### 为什么评论不改 issue 状态,而是入队新 task?

issue 是给用户看的板子项——它的 `in_review` / `done` 表示"整体到哪了"。如果评论就把它拨回 `todo` / `in_progress`,板子上刚标评审通过/完成的卡又跳回进行中,用户困惑(我明明评审完了,怎么又变进行中了?)。入队新 task 是生产 multiCa 的做法:issue 不动,task 队列里多一行,daemon 认领那行去跑。多次评论 = task 队列里多行,每行一次执行,session_id 串起来续会话。

### 为什么 resume 从 task 的 session_id,不是 issue 上的?

session_id 是 claude 一次执行尝试的会话标识,它属于 task(一次执行),不属于 issue(板子项)。一条 issue 可能跑了多次(首次 + 评论续聊 + 手动重跑),每次是不同的 task、可能有不同的 session_id(如果某次 resume 失效换了新 id)。从 task 取 session_id 精确到"上一条执行的会话",而不是 issue 上模糊的"某次"。

### 为什么 resume 失效用"覆盖"而不是报错?

claude 的 `--resume` 失效是常态化的(会话被清、过期)。如果报错,task 就卡住——人得手动清 session_id 才能重跑。直接用 claude 回吐的新 id 覆盖,等于"resume 没接上就当全新会话从头来,顺便记下新 id 供下次"。行为自愈,不卡流程。

### 为什么手动重跑建新 task,不在原 task 上续?

一条 task 行是一次执行尝试的完整记录(状态、session_id、时间、结果)。在原 task 上"续"会篡改历史——你看不出"这条 task 原来跑过一次,后来又重跑了"。建新 task(`rerun_of_task_id` 指向源 task)保留了执行历史:旧 task 行不动(还是 completed/failed),新 task 行是新的执行。这和多评论入队多 task 是同一个模型:每次执行一条 task 行。

## 三、评论 → 续聊怎么流转

```
  首次跑 issue(无 prior task)
        │  ClaudeCliRunner.run(resume_session_id=None) → prompt=title+body
        │  捕获 session_id → task.session_id
        │  task completed → issue in_review
        ▼
  用户评论 comment_and_enqueue(issue, "也处理一下 X")
        │  追加到 issue.comments;入队新 queued task(trigger_comment_id="也处理一下 X")
        │  issue 状态不变(仍 in_review)
        ▼
  daemon 认领新 task(又 queued 了)
        │  _find_prior_session_id → 上一 completed task 的 session_id
        │  ClaudeCliRunner.run(resume_session_id=prior_sid) → prompt=trigger_comment_id(评论文本)
        │  命中:claude 回吐同 id,覆盖 no-op;失效:回吐新 id,覆盖
        ▼
  claude --resume 重新载入上次会话 + 评论文本 → 带历史继续 → task completed
```

`session_id` 是首次跑捕获、之后每次重跑覆盖的——它把多次 `claude -p` 调用串成一条逻辑会话。`trigger_comment_id` 给 resume 提供新 turn,`_find_prior_session_id` 给 resume 提供续会话的 id,三者合起来就是"评论区续聊"。

## 试一试

```bash
python -m pytest tests/test_comment_resume.py -v   # 评论/resume 单测(不调真 claude)
python sessions/zh/s05_comment_resume.py           # demo:2 次真 claude(首次 + resume)
```

demo 第 1 次跑 issue "Reply with exactly: mica s05 first" → done,打印捕获的 `session_id`;然后 `comment_and_enqueue` 入队新 task;第 2 次跑(带 `--resume`)claude 重新载入上次会话 + 评论作新 turn → 回复新内容 → done。两次 `claude -p` 调用靠 `session_id` 串成同一条会话。

> `claude` 未装/未登录时,`ClaudeCliRunner` 返回 `failed`(`CLI not found`),session_id 为空,task 标 failed、issue 标 blocked,不静默跳过。

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s05 |
|---|---|---|
| 评论触发 | `CreateComment` → `triggerTasksForComment` → 三路分流(合并进 queued / active 暂存兜底 / 新建) | 简化:始终新建 queued task |
| issue 状态 | 评论不改 | **完全一致**(评论不改) |
| resume 机制 | claim 时填 `PriorSessionID`(从上一 task 取),daemon 拼 `--resume` | 同(daemon `_find_prior_session_id` → `--resume`) |
| resume 失效 | `resolveSessionID`:resume did not land → 清死 id | 用 RunResult 的新 id 覆盖 task.session_id |
| prompt 选择 | 往 stdin 写新 user turn | `trigger_comment_id` 作 prompt(resume 时) |
| 手动重跑 | `RerunIssue` 建新 task(`rerun_of_task_id`) | 同(`IssueStore.rerun_task`) |

一句话:**s05 用「评论入队新 task + `--resume` 上一 task.session_id」把 multiCa「评论区续聊」做出来了——issue 状态不动,task 队列多一行,session_id 跨执行串会话,resume 失效自愈覆盖**;砍掉的是生产的三路评论分流(合并/暂存),简化为始终新建。
