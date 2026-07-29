# s15 — Daemon Loop:让任务入队后自动执行

读到 s14 为止,mica 已经能执行 task,但执行方式仍然偏“手动”:你创建 issue 后会得到 queued task,然后必须再调用一次 `daemon.run_once()`。

```text
create issue
  -> TaskStore.enqueue(...)
  -> task.status = queued
  -> 用户/测试调用 daemon.run_once()
  -> daemon claim task
  -> runner 执行
```

s15 解决的是一个更接近真实系统的问题:

```text
任务已经入队了,为什么还要人手动点一下 run_once?
```

答案是:系统里应该有一个常驻后台 worker。它平时睡眠,有任务时醒来,不断调用 daemon 的单步执行能力,直到暂时没有任务可跑。

## 1. 先用一个场景理解

假设你在 Mission Control 里创建了一个 issue:

```text
Title: review login redirect
Assignee: agent-review
```

没有 s15 时,流程是:

```text
1. API 创建 Issue
2. API 创建 Task(status=queued)
3. 页面显示 queued
4. 你必须点 Run daemon once
5. daemon 才 claim 并执行这个 task
```

有 s15 后,流程变成:

```text
1. API 创建 Issue
2. API 创建 Task(status=queued)
3. TaskStore 发出“有新任务”的 wakeup
4. 后台 DaemonWorker 被唤醒
5. DaemonWorker 调用 daemon.tick()
6. daemon claim 并执行 task
7. task 变成 completed,issue 进入 in_review
```

这章的重点不是改变 task 怎么执行,而是改变“谁来触发执行”。

```text
s14: 人或测试触发 daemon.run_once()
s15: 后台 worker 自动触发 daemon.tick()
```

## 2. multiCa 里的真实问题是什么

multiCa 是一个 server + 本地 daemon 的系统。server 可以创建 task,但 server 不能直接在你的电脑上启动 `claude`、`codex` 这类 CLI。真正能启动本地 CLI 的,是你机器上运行的 daemon。

所以 multiCa 的基本分工是:

```text
server
  保存 issue/task
  把 task 放进队列
  通知相关 runtime:可能有活了

local daemon
  常驻在用户机器上
  注册自己有哪些 runtime/agent CLI
  向 server claim task
  在本机 workdir 里启动 CLI
  把结果回报给 server
```

也就是说,server 创建 queued task 后不会直接执行。它只能做两件事:

```text
1. 把 task 写入 agent_task_queue
2. 给 daemon 一个 wakeup hint:你可以来 claim 了
```

真正的执行链路是:

```text
server inserted queued task
  -> NotifyTaskEnqueued
  -> daemon 收到 task_available / 或下一次 poll 醒来
  -> daemon claim task
  -> daemon 在本机运行 provider CLI
  -> daemon report completed/failed
```

这就是 s15 要学的核心:队列和执行者是分离的。入队不是执行,入队只是让后台执行者有活可领。

## 3. multiCa 的几个名字应该怎么理解

如果不读代码,这些名字很容易显得抽象。可以先把它们翻译成普通动作:

```text
Daemon.Run()
  daemon 进程启动后的主入口。
  它不是只跑一个 task,而是启动一组后台循环,然后进入 claim/poll 主循环。

pollLoop()
  daemon 的主等待室。
  它接收 wakeup 信号,也处理定时 poll;一旦需要检查任务,就唤醒 claim loop。

runBatchPoller()
  真正反复 claim task 的循环。
  它先看本机有没有空闲执行名额,再向 server claim 一批 task。

ClaimTasksForRuntimes()
  server 侧的批量 claim。
  daemon 告诉 server:这些 runtime 是我本机能跑的,我还有 N 个空位;server 返回最多 N 个可执行 task。

handleTask()/runTask()
  daemon 本机执行 task。
  这里才会准备 workdir、启动 provider CLI、监听取消、上报结果。

NotifyTaskEnqueued()
  server 创建 task 后发出的提示。
  它不是执行 task,只是让 daemon 不必等到下一次 poll 才发现新任务。
```

所以 multiCa 的流程可以读成一句人话:

```text
Daemon.Run 启动常驻进程;
server 有新 task 时 NotifyTaskEnqueued;
pollLoop 收到提示或定时醒来;
runBatchPoller 按本机空闲容量 claim task;
handleTask/runTask 在本机执行并回报。
```

## 4. 为什么既要 wakeup,又要 poll

只靠 poll 可以工作,但体验差。

```text
poll interval = 3s
用户刚创建 task
最坏要等接近 3s daemon 才发现
```

所以需要 wakeup:

```text
task enqueue
  -> wakeup daemon
  -> daemon 立刻 claim
```

但只靠 wakeup 也不安全。网络断开、websocket 重连、进程刚启动时错过通知,都有可能让 wakeup 丢失。因此还要保留 poll fallback:

```text
wakeup 成功
  task 几乎立刻执行

wakeup 丢失
  下一次 poll 仍然会发现 queued task
```

multiCa 里 `NotifyTaskEnqueued()` 会先让“空队列缓存”失效,再发 wakeup。这个顺序是为了避免一种竞态:

```text
daemon 刚刚查过:没有 task
server 把“该 runtime 暂时没任务”缓存起来
新 task 入队
如果先 wakeup,再清缓存
daemon 可能被唤醒后仍读到旧的“没任务”判断
```

所以生产版会特别强调:

```text
先 bump / invalidate empty-claim cache
再 wakeup daemon
```

mica s15 不实现这个缓存,但保留同一个思想:先创建 queued task,再通知 worker。

## 5. mica s15 如何压缩这个模型

s15 这个压缩模型不实现生产级远程 server、WebSocket wakeup、数据库 claim、runtime 批量调度。它只保留后台轮询和 wakeup 标记两个核心概念。

```text
WakeupTaskStore
  仍然是 TaskStore
  但 enqueue 成功后会通知订阅者

DaemonWorker
  后台线程
  被 wakeup 或 poll_interval 唤醒
  反复调用 daemon.tick()
```

对应关系是:

```text
multiCa server NotifyTaskEnqueued
  -> mica WakeupTaskStore._notify_task_available()

multiCa daemon pollLoop/runBatchPoller
  -> mica DaemonWorker._loop()

multiCa daemon claim + handleTask/runTask
  -> mica Daemon.tick() + Daemon.execute()
```

s15 的数据流是:

```text
IssueStore.create(...)
        │
        ▼
WakeupTaskStore.enqueue(...)
        │  创建 queued task
        │  通知 callback
        ▼
DaemonWorker.wakeup()
        │
        ▼
DaemonWorker._loop()
        │
        ▼
daemon.tick()
        │
        ├─ task_store.claim_next(...)
        ├─ daemon.execute(task)
        └─ runner.run(...)
```

## 6. DaemonWorker 的 loop 逐行理解

s15 的核心循环是:

```python
while not self._stop.is_set():
    drained = False
    while not self._stop.is_set() and self.daemon.tick():
        self.claimed_count += 1
        drained = True

    if drained:
        continue

    self._wakeup.wait(self.poll_interval)
    self._wakeup.clear()
```

可以翻译成:

```text
只要 worker 没停:
  先尝试执行 task
  如果能执行,就一直执行
  直到 daemon.tick() 说“没有 queued task 了”

  如果刚才执行过 task:
    马上回到循环顶部,再检查一次

  如果完全没有 task:
    睡眠,等待 wakeup 或 poll_interval 到期
```

这里的 `daemon.tick()` 是单次执行原语:

```text
tick()
  claim 一个 queued task
  如果 claim 到:
    execute(task)
    return True
  如果没有 claim 到:
    return False
```

`drained` 的含义是“这一轮有没有跑过至少一个 task”。如果跑过,worker 不立刻睡眠,而是马上继续检查。原因是 task 执行过程中可能产生新 task:

```text
leader task 执行完成
  -> 创建 child issue
  -> enqueue worker task

worker report 到达
  -> 重新 enqueue leader task
```

如果刚执行完一个 task 就睡眠,这些连锁任务可能要等下一次 wakeup 或 poll。`if drained: continue` 让 worker 更积极地把队列推进到真正空闲。

## 7. 为什么不把执行逻辑写进 Worker

`DaemonWorker` 只负责“何时调用 daemon.tick()”。它不应该知道如何执行 task。

执行 task 的知识已经在 `Daemon.execute()` 里:

```text
查 issue
解析 project workdir
查 Agent Profile
加载 Agent.skill_names 对应的 skill
调用 runner.run(...)
写 task / issue 终态
记录 session events
```

如果把这些逻辑搬进 worker,边界会混乱:

```text
Daemon
  负责 claim + execute

DaemonWorker
  负责后台生命周期:start / stop / wakeup / poll fallback

TaskStore
  负责创建和保存 task

WakeupTaskStore
  负责 enqueue 后通知 worker
```

这个边界也对应 multiCa:生产版 daemon 的 poller 负责“claim 和调度”,真正的本地执行仍然落到 `handleTask/runTask`。

## 8. 和 run_once 的关系

`s15` 没有删除 `daemon.run_once()`。它仍然适合:

```text
单元测试
调试按钮
同步执行当前队列
```

`DaemonWorker` 适合:

```text
服务启动后常驻
任务入队后自动执行
接近 multiCa daemon 常驻执行模型
```

所以二者关系是:

```text
run_once
  人工/测试驱动的一次 drain

DaemonWorker
  后台驱动的持续 drain
```

Mission Control 里的 `Run once` 是调试按钮;`Start daemon` 才是 s15 后更接近真实系统的主路径。

## 9. 与 multiCa 的对应关系

| 学习点 | mica s15 | multiCa |
|---|---|---|
| daemon 常驻 | `DaemonWorker.start()` 启动后台线程 | `Daemon.Run()` 启动 daemon 进程和后台 loops |
| 新任务提示 | `WakeupTaskStore.enqueue()` 后调用 callback | `NotifyTaskEnqueued()` bump cache 后发 daemon wakeup |
| 定时兜底 | `poll_interval` 到期后再 tick | `pollLoop()` 在 wakeup 丢失时仍按 poll interval 检查 |
| claim 单位 | `daemon.tick()` 一次 claim 一个 task | `ClaimTasksForRuntimes()` 按 runtime 和空闲 slot 批量 claim |
| 本地容量 | 教学版单 worker,不做并发 slot | `MaxConcurrentTasks` 控制本机并发执行名额 |
| 执行入口 | `Daemon.execute()` 调 runner | `handleTask()` / `runTask()` 准备环境并启动 provider CLI |
| 任务完成后继续推进 | `drained -> continue` 立即再检查 | task goroutine 释放 slot 后 `signalPollerWakeup()` |

一句话总结:

```text
s15 让 mica 从“有人手动调用 daemon.run_once()”变成“后台 worker 在任务入队后自动醒来、自动 claim、自动执行”。
```

## 试一试

```bash
python -m pytest tests/test_daemon_loop.py -v
python sessions/zh/s15_daemon_loop.py
```

demo 里你会看到:代码只创建 issue 并 enqueue task,没有调用 `daemon.run_once()`。task 仍然会被后台 worker 执行到 completed,issue 进入 `in_review`。

你可以重点观察三个位置:

```text
WakeupTaskStore.enqueue()
  新 task 创建后通知 worker

DaemonWorker._loop()
  收到 wakeup 后持续 tick

wait_until(...)
  demo 等待后台线程把 task 跑完
```
