# s12 — Squads:从“分配给人”到“分配给团队”

s12 学的是 multiCa 里 squads 的核心思想:一个 issue 不一定直接分配给某个 agent,也可以分配给一个 **squad**。分配给 squad 之后,系统不是随机挑一个成员去做,而是先唤醒 squad 的 **leader agent**。leader 拿到团队 roster、成员技能、团队 instructions 后,再决定如何委派、拆分、等待回报、推进下一阶段。

这一章的 Python 实现仍然是教学版,但已经覆盖 multiCa squads 最关键的几条机制:

```text
issue assignee_type='squad'
   -> enqueue leader task
   -> leader task carries is_leader_task + squad_id
   -> claim 时可生成 squad briefing
   -> leader delegate worker
   -> worker report / child stage done wakes leader again
```

## 1. 为什么 squad 不是“成员列表”

如果 squad 只是一个成员列表,最容易写成:

```text
issue -> 从 members 里选一个 agent -> enqueue task
```

这只能解决“谁来做”里的最简单情况,但解决不了团队协作里的关键问题:

```text
这个任务要不要拆成多个子任务?
先做前端还是先做测试?
哪个成员更适合处理某一段?
worker 做完后谁来汇总结果?
stage 1 完成后谁来推进 stage 2?
成员回报后如何避免 leader 被重复唤醒?
```

multiCa 的 squad 选择了另一种模型:**leader-first coordination**。squad 本身是一个可分配对象,但实际执行入口是 leader。leader 的职责不是“永远自己做”,而是读取上下文、选择成员、委派任务、记录判断、等待下一次触发。

## 2. 本章新增的对象

### 2.1 SquadMember

```python
@dataclass
class SquadMember:
    member_type: str            # "agent" | "member"
    member_id: str
    role: str = ""
    skills: list[str] = field(default_factory=list)
```

`SquadMember` 表示 squad roster 里的一个成员。成员可以是 agent,也可以是 human member。教学代码里 human member 不会执行 task,但它会出现在 roster/status 中,帮助你理解 multiCa 为什么不是纯 agent list。

`role` 是团队里的角色描述,例如 `frontend`、`tester`、`pm`。`skills` 用来模拟 multiCa briefing 里给 leader 的能力提示:leader 应该根据成员技能委派,而不是按顺序或随机挑人。

### 2.2 Squad

```python
@dataclass
class Squad:
    id: str
    name: str
    leader_agent_id: str
    description: str = ""
    instructions: str = ""
    members: list[SquadMember] = field(default_factory=list)
```

核心字段是:

```text
leader_agent_id   这个 squad 的协调者
members           squad roster
instructions      团队级运行规则,会进入 leader briefing
```

`__post_init__()` 会保证 leader 也在 members 里。这样 briefing 渲染 roster 时,leader 会以 `(leader)` 标记出现。

### 2.3 Task 上的 leader 标记

s12 给 `Task` 增加了三个字段:

```python
is_leader_task: bool = False
squad_id: str = ""
handoff_note: str = ""
```

这三个字段很重要:

```text
is_leader_task=True   说明这个 task 是以 squad leader 身份运行
squad_id              告诉 daemon/claim 这是哪个 squad 的 leader task
handoff_note          记录这次唤醒的原因,例如 worker 回报或 child stage 完成
```

在 multiCa 中,claim handler 不是简单看 issue 当前是不是分配给 squad,而是看 task 是否带 `is_leader_task + squad_id`。原因是 leader task 可能来自很多入口:创建 issue、@squad mention、worker comment、child-done system comment、autopilot。只看 issue assignee 会漏掉一些入口。

## 3. SquadRouter 的职责

读 `SquadRouter` 时,不要先背方法名。先把它放进一次 squad 工作流里看:它负责把“issue 交给一个团队”拆成几次可记录、可继续的系统动作。

```text
登记团队资料
   -> issue 分配给 squad
   -> 创建 leader task
   -> leader claim 时拿到 briefing
   -> leader 委派 worker
   -> worker 回报后唤醒 leader
   -> child stage 完成后唤醒 parent leader
   -> 页面可查看成员当前状态
```

对应到代码,每个方法只负责其中一个动作:

| 方法 | 触发时机 | 主要读写 | 下一步 |
|---|---|---|---|
| `register(squad)` | 系统启动或测试准备 squad 数据时 | 把 `Squad` 放入 `_squads` | 后续可以按 `squad_id` 找到 leader、members、instructions |
| `assign_issue_to_squad(issue_id, squad_id)` | 用户把 issue 分配给 squad 时 | 修改 `Issue.assignee_type/id`,创建 `is_leader_task=True` 的 leader task | daemon 认领 leader task |
| `build_leader_briefing(squad_id)` | leader task 被 claim、准备构造 prompt 时 | 读取 squad leader、members、skills、instructions | leader 知道自己要协调谁、如何委派 |
| `delegate(issue_id, squad_id, member_agent_id, note)` | leader 选定某个 worker 后 | 写一条 mention 风格评论,创建 worker task | worker agent 执行具体工作 |
| `worker_report(issue_id, squad_id, worker_agent_id, text)` | worker 完成或反馈时 | 写 worker 评论,必要时创建新的 leader task | leader 汇总结果或安排下一步 |
| `notify_child_done(child_issue_id)` | child issue 进入 `done/cancelled` 时 | 检查同 stage children 是否都结束,必要时唤醒 parent leader | leader 推进下一 stage 或收尾 parent issue |
| `member_status(squad_id)` | 页面需要展示 squad roster 状态时 | 从 active task 推导 member 状态 | UI 显示 working/idle/human |

这张表里的“下一步”很重要。`SquadRouter` 自己不执行 agent,也不替 leader 做决策;它只把团队协作里的关键节点变成明确的数据变化:issue assignee、task、comment、activity/status。这样每次唤醒都有来源,每个 worker 的动作都能回到 parent issue,leader 也能在下一次运行时继续接上上下文。

## 4. 主流程:分配给 squad 先唤醒 leader

创建普通 issue:

```python
issue = issue_store.create("ws-1", "build landing page")
```

分配给 squad:

```python
leader_task = router.assign_issue_to_squad(issue.id, "sq-1")
```

发生两件事:

```text
Issue.assignee_type = "squad"
Issue.assignee_id   = "sq-1"

Task.assignee_type  = "agent"
Task.assignee_id    = squad.leader_agent_id
Task.is_leader_task = True
Task.squad_id       = "sq-1"
```

注意这个差异:

```text
issue 的 assignee 是 squad
task 的 assignee 是 leader agent
```

这就是 squad 模型的第一层关键:对用户来说,工作属于团队;对执行系统来说,第一步要启动 leader。

## 5. leader briefing:让 leader 知道自己不是普通 worker

`build_leader_briefing()` 生成一个简化版 briefing:

```text
## Squad Operating Protocol
你是 leader,职责是协调,不要默认自己完成所有工作。

## Squad Roster
- lead (leader) — agent, role: "leader" — skills: planning, review — `[@lead](mention://agent/lead)`
- ui — agent, role: "frontend" — skills: react, css — `[@ui](mention://agent/ui)`
- qa — agent, role: "tester" — skills: playwright — `[@qa](mention://agent/qa)`

## Squad Instructions (Frontend Team)
Accessibility fixes go first.
```

这个 briefing 解决一个非常实际的问题:leader 如果只看到普通 issue prompt,它很可能把任务当成自己的个人任务直接做掉。squad briefing 明确告诉它:

```text
你现在是 squad leader
你要根据 roster/skills 选择成员
你要通过 mention-style 指令委派
你要记录 action/no_action/failed
委派后停止,等待下一次触发
```

multiCa 中这部分更严格:briefing 里包含固定 operating protocol、可复制的 mention markdown、成员 roster、成员 skills、squad instructions,并要求 leader 记录 `multica squad activity`。

## 6. leader 委派 worker

leader 选择一个 squad 内 agent:

```python
worker_task = router.delegate(
    issue.id,
    "sq-1",
    "ui",
    "please implement the responsive nav",
)
```

教学代码做三件事:

```text
确认 ui 是 sq-1 的 agent member
在 issue 评论里写入 mention://agent/ui
创建 worker task
```

worker task 的关键字段:

```text
assignee_id     = "ui"
is_leader_task  = False
squad_id        = "sq-1"
handoff_note    = "please implement the responsive nav"
```

它不是 leader task,所以不会拿到 squad leader briefing。它只是作为 worker 执行 leader 委派的那一段工作。

如果要委派给 squad 外的 agent,代码会抛 `PermissionError`。这对应 multiCa 中“不能通过 squad 绕过 agent 调用权限”的设计:能不能把某个 agent 接进 squad,以及能不能让它当 leader/worker,都要经过权限校验。

## 7. worker 回报后唤醒 leader

worker 做完后会评论:

```python
wake = router.worker_report(
    issue.id,
    "sq-1",
    "qa",
    "done, root cause found",
)
```

如果当前 issue 上没有 pending leader task,系统会再创建一个 leader task:

```text
assignee_id     = squad.leader_agent_id
is_leader_task  = True
squad_id        = "sq-1"
handoff_note    = "worker qa reported"
```

这模拟 multiCa 的 worker -> leader 闭环:worker 不负责汇总整个 parent issue,leader 才负责判断下一步:

```text
继续委派?
要求返工?
创建下一批 child issues?
关闭 issue?
向 human 提问?
```

`worker_report()` 还做了 pending 去重。如果 leader 已经有 queued/dispatched/running task,不会再堆一个重复 leader task。这个去重非常重要,否则多个 worker 短时间回报会让 leader 被重复唤醒多次。

## 8. child issue 与 stage barrier

s12 给 `Issue` 增加了:

```python
parent_issue_id: str = ""
stage: int | None = None
```

leader 可以把 parent issue 拆成 child issues:

```python
child_a = router.create_child_issue(parent.id, "build form", "agent", "ui", stage=1)
child_b = router.create_child_issue(parent.id, "test form", "agent", "qa", stage=1)
child_c = router.create_child_issue(parent.id, "polish", "agent", "ui", stage=2)
```

当 child A 完成时:

```python
router.notify_child_done(child_a.id)
```

如果 stage 1 里还有 child B 没完成,不会唤醒 leader。

当 child B 也完成时,stage 1 barrier 关闭:

```text
stage 1: build form done
stage 1: test form done
stage 2: polish still open
```

这时 `notify_child_done()` 会在 parent issue 上写一条 system comment,并创建新的 leader task:

```text
handoff_note = "child stage completed"
```

这个机制表达的是 multiCa 的一个核心协作循环:

```text
leader 拆 stage 1
workers 完成 stage 1 children
系统发现 stage 1 barrier closed
系统唤醒 parent 的 leader
leader 决定是否推进 stage 2
```

服务器不能简单地在每个 child 完成时都唤醒 leader,否则会产生噪音和重复执行。它应该在一个阶段真正关闭时再唤醒。

## 9. 成员状态视图

`member_status()` 是 multiCa squad member status endpoint 的教学版。它从 active task 推导成员状态:

```text
agent 有 queued/dispatched/running task -> working
agent 没有 active task                  -> idle
human member                            -> human
```

生产系统会更复杂:它还会读取 runtime status、last_seen_at、archived_at,并把 agent 区分成 `working/idle/unstable/offline/archived`。s12 只保留最适合学习的部分:status 不是手写字段,而是从 task/runtime 信号推导出来的。

## 10. 和 multiCa 的对应关系

| 概念 | s12 教学代码 | multiCa 生产实现 |
|---|---|---|
| squad 存储 | 内存 `_squads` | `squad` / `squad_member` 表 |
| squad assignee | `Issue.assignee_type="squad"` | `issue.assignee_type='squad'` |
| leader task | `Task.is_leader_task=True` | `agent_task_queue.is_leader_task` |
| squad 归属 | `Task.squad_id` | `agent_task_queue.squad_id` |
| briefing | `build_leader_briefing()` | claim 时注入 operating protocol + roster + instructions |
| 委派 worker | `delegate()` 创建 worker task | leader 通过 mention 或 child issue 触发 worker |
| worker 回报 | `worker_report()` 唤醒 leader | worker comment 触发 squad leader fallback |
| stage barrier | `notify_child_done()` | child-done system comment + parent assignee trigger |
| 成员状态 | `member_status()` | squad members status endpoint |
| 权限 | squad membership 校验 | workspace + creator/admin + agent invocation gate |

一句话:**s12 中的 squad 是一个可分配的团队对象;leader 负责协调 roster 中的人和 agent,并把多步协作推进成可追踪的 task、comment 和 stage。**

## 11. 试一试

运行测试:

```bash
python -m pytest tests/test_squads.py -q
```

测试覆盖:

```text
分配给 squad 会创建 leader task
leader briefing 包含 protocol/roster/skills/instructions
leader 可以委派 squad 内 worker
不能委派 squad 外 agent
worker 回报会唤醒 leader 且去重
child issue 分配给 agent 会创建 worker task
child stage barrier 关闭后唤醒 parent leader
demo 输出完整 leader lifecycle
member_status 从 active task 推导 working/idle/human
```

运行 demo:

```bash
python sessions/zh/s12_squads.py
```

demo 会打印一条完整 squad 生命周期:

```text
parent issue 分配给 squad
leader 被唤醒并看到 roster
leader 创建 Stage 1 child issues 后停止
两个 worker 分别执行 Stage 1
第一个 worker 完成时 stage barrier 仍然 open,不唤醒 leader
第二个 worker 完成时 stage barrier closed,系统唤醒 leader
leader 读取结果并把 Stage 2 从 backlog 推进到 todo
最后打印 member status 和 parent timeline
```

## 12. 本章边界

s12 做了:

```text
SquadMember / Squad / SquadActivity
issue assignee_type='squad'
leader task: is_leader_task + squad_id
squad briefing
leader delegate worker
worker report wakes leader
child issue + stage barrier
member status derivation
```

s12 不做:

```text
数据库持久化 squad/squad_member
HTTP API 和 workspace route middleware
真实 mention parser
真实 daemon claim 注入
agent visibility / private leader 完整权限模型
runtime online/offline/unstable 状态
autopilot 分配给 squad
并发 race 下的 SQL 级去重
```

学习时要抓住主线:**用户把 issue 交给 squad;系统唤醒 leader;leader 根据 roster 协调 worker;worker 或 child stage 完成后再唤醒 leader。squad 的价值不是“自动挑一个人”,而是把多 agent 协作变成可追踪、可恢复、可分阶段推进的工作流。**
