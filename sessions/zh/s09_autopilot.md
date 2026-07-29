# s09 — Cron Autopilot

s08 之前,所有 task 都来自人工操作:

```text
用户创建 issue
用户评论 issue
用户手动重跑 task
```

s09 加一类新的任务来源:**定时任务**。

Autopilot 的职责很简单:

```text
cron 到期
   -> 自动创建 issue
   -> 自动入队 task
   -> 后续仍走 daemon 认领执行链路
```

它不替代 daemon,也不直接执行 runner。它只负责按时间制造新的 issue/task。

## 1. s09 新增了什么

s09 在 s08 基础上新增:

```text
Autopilot           一条定时规则
AutopilotStore      autopilots.jsonl 持久化
Scheduler           tick(now) 检查到期规则
croniter            根据 cron 算 next_run
```

数据文件:

```text
workspace/autopilots.jsonl
```

持久化方式延续 s04:JSONL 快照追加,last-wins 重放。

## 2. Autopilot 数据结构

```python
@dataclass
class Autopilot:
    id: str
    cron: str
    agent_id: str
    title: str
    template: str = ""
    next_run: str = ""
    fail_count: int = 0
    enabled: bool = True
    timezone: str = "UTC"
```

字段含义:

```text
cron        标准 5 段 cron 表达式
agent_id    到期后 issue 派给哪个 agent
title       自动创建的 issue title
template    自动创建的 issue body
next_run    下次触发时间,ISO 字符串
fail_count  连续失败次数
enabled     是否启用
timezone    cron 计算使用的时区,默认 UTC
```

如果 `next_run` 为空,第一次 `tick()` 只会初始化它,不会立刻触发。

## 3. Scheduler.tick 做什么

核心入口:

```python
def tick(self, now=None) -> int:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    triggered = 0
    for ap in self.ap_store.list():
        if not ap.enabled:
            continue
        if not ap.next_run:
            self._compute_next_run(ap, now)
            continue
        next_run = self._parse_iso(ap.next_run)
        if next_run <= now:
            self._fire(ap, now)
            triggered += 1
    return triggered
```

它只看三件事:

```text
规则是否 enabled
是否已有 next_run
next_run 是否已经到期
```

返回值是这次 tick 触发了几条 autopilot。

## 4. 到期后创建 issue + task

到期触发在 `_fire()`:

```python
def _fire(self, ap: Autopilot, now) -> None:
    issue = self.issue_store.create(
        "ws-1", ap.title, body=ap.template,
        assignee_type="agent", assignee_id=ap.agent_id,
    )
    self.task_store.enqueue(issue.id, "agent", ap.agent_id)
    self._compute_next_run(ap, now)
    ap.fail_count = 0
    self.ap_store.save(ap)
```

触发产生的是普通 issue 和普通 queued task。daemon 不需要知道它来自 autopilot:

```text
autopilot task
manual task
comment follow-up task
```

对 daemon 来说都是 queued task。

## 5. next_run 为什么触发时就推进

`_fire()` 创建 issue/task 后立刻计算下一次:

```python
cron = croniter(ap.cron, local_now)
next_dt = cron.get_next(datetime.datetime)
ap.next_run = next_dt.isoformat()
```

这件事不等 runner 执行结果。

原因是执行是异步的:

```text
Scheduler 只负责触发
Daemon 之后才认领并执行 task
```

如果等执行结果再推进 `next_run`,中间进程崩溃就可能导致同一个时间槽位重复触发。触发即推进,能保证重启后以落盘的 `next_run` 为准。

## 6. fail_count 和禁用

s09 还提供两个方法:

```python
def mark_fail(self, ap_id: str) -> None:
    ap.fail_count += 1
    if ap.fail_count >= 5:
        ap.enabled = False
    self.ap_store.save(ap)

def mark_success(self, ap_id: str) -> None:
    ap.fail_count = 0
    self.ap_store.save(ap)
```

规则:

```text
执行成功       fail_count = 0
执行失败       fail_count += 1
连续失败 5 次  enabled = False
```

禁用后不会自动恢复。需要人修改配置或代码重新启用。

注意当前 mica 的 `_fire()` 在“成功创建 issue/task”后会清零 `fail_count`;实际 runner 执行结果需要外部调用 `mark_success/mark_fail` 来记录。也就是说,s09 把调度和执行结果记录拆开了。

## 7. 时区处理

`timezone` 默认 `"UTC"`。

```python
def _get_tz(self, tz_name: str):
    if tz_name == "UTC" or not tz_name:
        return datetime.timezone.utc
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        return datetime.timezone.utc
```

未知时区会退回 UTC。`_compute_next_run()` 会先把 `now` 转到指定时区,再用 croniter 计算下一次。

## 8. 整体链路

```text
AutopilotStore 加载 autopilots.jsonl
   |
   v
Scheduler.tick(now)
   |
   |-- disabled       -> 跳过
   |-- next_run 空    -> 计算并保存 next_run
   |-- next_run 未到  -> 跳过
   `-- next_run 已到  -> _fire()
                           |
                           |-- IssueStore.create(...)
                           |-- TaskStore.enqueue(...)
                           |-- compute next_run
                           `-- save autopilot snapshot

后续:
daemon 认领 queued task -> runner 执行 -> 外部 mark_success/mark_fail
```

## 9. 试一试

运行测试:

```bash
python -m pytest tests/test_autopilot.py -v
```

测试覆盖:

```text
cron 计算 next_run
到期创建 issue 和 task
触发后推进 next_run
disabled 规则不触发
连续失败 5 次禁用
成功清零 fail_count
重启后不重复触发已经推进过的槽位
```

运行 demo:

```bash
python sessions/zh/s09_autopilot.py
```

demo 会创建一条到期 autopilot,触发后生成 issue + task,再打印看板和 task 数量。

## 10. 和 multiCa 的区别（进阶对照）

这一节是进阶对照。第一次读可以先抓四件事:mica 只做本地 cron;multiCa 要处理多触发来源;生产系统必须记录每次 run;分布式调度需要额外的幂等和失败治理。下面的生产名词不用一次背完,先用对照表理解边界即可。

`mica` s09 是一个教学版 cron scheduler:一条 `Autopilot` 规则直接带 `cron`、`agent_id`、`title`、`template` 和 `next_run`,到期后创建 issue 和 queued task。`multiCa` 的生产实现拆得更细,因为它要处理多 workspace、多触发器、多副本调度、审计、幂等和失败治理。

### 10.1 数据模型:一张 JSONL vs 多张表

`mica` 只有一个本地文件:

```text
workspace/autopilots.jsonl
```

每条 autopilot 自己保存 `cron`、`next_run`、`fail_count`。这适合单进程 demo。

`multiCa` 把概念拆成几层:

```text
autopilot              自动化规则本体:标题、目标、状态、execution_mode
autopilot_trigger      触发器:schedule / webhook / api,cron、timezone、next_run_at、token
autopilot_run          每次触发生成的运行记录:source、status、issue_id、task_id、planned_at
autopilot_rule_version 审计快照:谁发布了当前规则,未来 run 归责给谁
```

所以 `multiCa` 不只是问“下一次什么时候跑”,还会回答:

```text
这次 run 是 schedule/webhook/manual 哪来的?
这次 run 属于哪个 trigger?
这次 run 有没有创建 issue 或 task?
这次 run 应该归责给哪个人或规则版本?
这次 run 是失败、完成、跳过,还是还在进行中?
```

### 10.2 触发来源:mica 只有 cron,multiCa 有 schedule/webhook/manual/api

`mica` s09 只实现 cron 到期:

```text
Scheduler.tick(now) -> _fire(ap, now)
```

`multiCa` 的 `AutopilotService` 则把触发来源显式写入 `autopilot_run.source`:

```text
schedule  定时触发
webhook   外部事件触发
manual    用户点击 run now
api       API 触发
```

这会影响归责和幂等。比如 manual run 是明确的人类动作;schedule/webhook/api 没有现场人工点击,通常归责到规则 owner 或 trigger publisher。

### 10.3 执行模式:mica 总是创建 issue,multiCa 有 create_issue 和 run_only

`mica` 到期后固定做两件事:

```text
IssueStore.create(...)
TaskStore.enqueue(issue.id, agent_id)
```

`multiCa` 的 `execution_mode` 至少分两类:

```text
create_issue  先创建一个可见 issue,再让 issue/task 链路接管
run_only      不创建 issue,直接创建 agent_task_queue 任务,但仍记录 autopilot_run
```

这就是为什么 `multiCa` 的 `autopilot_run` 同时有 `issue_id` 和 `task_id`:不同模式下,下游可观察对象不同。`mica` 没有这层分叉,所以不需要 run 表。

### 10.4 幂等:mica 靠 next_run 推进,multiCa 双层防重复

`mica` 的防重复主要靠“触发后立刻推进并保存 `next_run`”。这能防单进程重启后重复触发同一个槽位。

`multiCa` 面对的是多进程/多副本调度,所以有两层保护:

```text
sys_cron_executions         调度层:同一个 job/scope/plan_time 只允许一个 runner claim
autopilot_run.planned_at    dispatch 层:同一个 trigger_id + planned_at 只允许一个有效 run
```

`DispatchAutopilotForPlan` 必须拿到明确的 `trigger_id` 和 `planned_at`;如果同一个计划时间已经有完整 run,它复用旧 run,不再创建第二个 issue/task。如果发现旧 run 是“只写了 run 行,还没创建下游 issue/task”的 partial 状态,会先把旧 run 标成 failed 并清掉 `planned_at`,再创建新的 run。这是生产系统为了处理崩溃窗口做的恢复逻辑。

### 10.5 失败治理:mica 是 fail_count,multiCa 是基于 run 历史的自动暂停

`mica` 有简单计数:

```text
mark_fail -> fail_count += 1
fail_count >= 5 -> enabled = False
mark_success -> fail_count = 0
```

`multiCa` 不靠单字段连续计数,而是周期性扫描 `autopilot_run` 历史。failure monitor 默认看最近 7 天、至少 50 次 run、失败率达到 90% 的 active autopilot,然后把它系统暂停为 `paused`,记录 rule version,并发 inbox/WS 通知。

这解决的是生产问题:一个每 5 分钟跑一次、长期 100% 失败的 autopilot,如果没人处理,会持续烧任务和 token。`mica` 的 5 次禁用是教学版保护;`multiCa` 的 failure monitor 是运营级保护。

### 10.6 离线 runtime:mica 不管,multiCa 会 admission skip

`mica` 的 Scheduler 到期就 enqueue task,不检查 agent/runtime 是否在线。

`multiCa` 在 `run_only` 模式下会做触发时准入:如果目标 agent 的 runtime 离线,它记录一个 `skipped` 的 `autopilot_run`,不把 doomed task 堆进队列。`create_issue` 模式例外:为了保留可见审计 trail,即使 runtime 暂时离线也可以创建 issue,等 runtime 回来后继续处理。

### 10.7 对照表

| 维度 | mica s09 | multiCa 生产实现 |
|---|---|---|
| 存储 | `autopilots.jsonl` | Postgres:autopilot / trigger / run / rule_version |
| 触发来源 | cron only | schedule / webhook / manual / api |
| 调度入口 | `Scheduler.tick(now)` | scheduler manager + `autopilot_schedule_dispatch` job |
| 到期副作用 | 创建 issue + queued task | create_issue 或 run_only |
| 幂等 | 推进 `next_run` | `sys_cron_executions` + `(trigger_id, planned_at)` |
| 运行记录 | 无独立 run 表 | `autopilot_run` 全量记录 source/status/payload/issue/task |
| 失败处理 | `fail_count >= 5` 禁用 | failure monitor 按窗口失败率 auto-pause + 通知 |
| runtime 离线 | 仍入队 | run_only 可 skipped,create_issue 保留审计 |
| webhook | 不做 | token / signing secret / durable delivery / idempotent run |
| 归责审计 | 不做 | rule_version、originator/accountable、trigger evidence |

一句话:**mica s09 是“cron 到期制造 issue/task”的最小模型;multiCa 是“多触发器 + 可审计 run + 分布式幂等 + 失败治理”的生产模型。**

## 11. 本章边界

s09 做了:

```text
cron autopilot 数据结构
autopilots.jsonl 持久化
tick 检查到期
到期创建 issue + queued task
next_run 推进
fail_count 和 5 次禁用
```

s09 不做:

```text
后台常驻 scheduler loop
把 task 结果自动回写到 autopilot
webhook 触发
复杂审计表
多 workspace autopilot
生产级重试策略
```

一句话总结:**s09 让 task 不只来自人,也可以来自时间。cron 到期只负责制造 issue/task;执行仍交给已有 daemon 链路。**
