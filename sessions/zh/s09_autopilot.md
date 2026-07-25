# s09 — Cron Autopilot(croniter + 建 issue+task + fail_count 禁用)

s08 让 agent 有了技能,但任务来源还是手动创建。s09 加上定时调度:cron 表达式到期时自动建 issue + 入队 task,进正常认领。失败推进 next_run、成功清零 fail_count、连续 5 次禁用、重启不重触发。

## 一、核心设计思想

第一个思想:**cron 到期 → 建 issue+task**。`Scheduler.tick` 检查所有 `enabled` 的 autopilot,`next_run` 已到的就触发——创建一条 issue(指派给 autopilot 的 agent)+ 入队一条 queued task,进入正常 daemon 认领流程:

```python
def _fire(self, ap, now):
    issue = self.issue_store.create("ws-1", ap.title, body=ap.template,
                                    assignee_type="agent", assignee_id=ap.agent_id)
    self.task_store.enqueue(issue.id, "agent", ap.agent_id)
    self._compute_next_run(ap, now)
    ap.fail_count = 0
    self.ap_store.save(ap)
```

autopilot 触发的 task 和手动创建的 task 走同一条认领→执行→标终态链路——daemon 不区分 task 来源。

第二个思想:**next_run 用 croniter 算,无论成败推进**。到期触发后,用 `croniter.croniter(ap.cron, now).get_next()` 算下一次运行时间,落盘到 `autopilots.jsonl` 的 `next_run` 字段。无论执行成功还是失败,next_run 都推进——否则失败的 autopilot 会在同一槽位反复触发,无限刷 issue。

第三个思想:**连续 5 次失败禁用**。`mark_fail` 把 `fail_count+1`,到 5 置 `enabled=False`。禁用后**不自动恢复**,需手动改 `autopilots.jsonl` 的 `enabled=true`。这防止一条坏任务无限重跑。`mark_success` 清零 `fail_count`。

第四个思想:**重启不重触发**。daemon 重启后加载 `autopilots.jsonl`,以落盘的 `next_run` 为准。如果 `next_run` 已在过去但该槽位已触发过(上次触发时推进了 next_run 到未来),重启不会重复建 issue。

## 二、为什么这么设计

### 为什么到期触发即推进 next_run,不等执行结果?

执行是异步的(daemon 认领 task 后才跑),如果等执行结果再推进 next_run,触发和推进之间有间隔——如果 daemon 崩了,next_run 没推进,重启后会重触发同一槽位。触发即推进(不管执行结果)保证 next_run 始终是"下一次运行时间",不会重触发。执行失败靠 `mark_fail` 单独计数。

### 为什么连续 5 次失败才禁用,不第一次失败就禁?

单次失败可能是暂时的(网络、模型过载、CLI bug)。给 5 次机会容错。5 次连续失败说明这条 autopilot 本身有问题(配置错误、模板错、agent 不可用),禁用它避免无限刷 issue。`mark_success` 清零计数——如果中间成功一次,连续计数重置。

### 为什么禁用后不自动恢复?

自动恢复可能让一条已知的坏 autopilot 又开始触发。禁用是显式信号"这条有问题,人来看"。恢复需要人手动改 `enabled=true`,确保人确认了问题已修。

## 三、怎么流转

```
  autopilots.jsonl 加载(enabled + cron + next_run)
        │
  Scheduler.tick(now)
        │  遍历 enabled autopilot
        │  next_run <= now? → _fire
        ▼
  _fire: create issue + enqueue task + _compute_next_run(croniter) + save
        │
  daemon 认领 task → 执行 → 成功 mark_success / 失败 mark_fail
        │  mark_fail: fail_count+1; >=5 → enabled=False
        │  mark_success: fail_count=0
        ▼
  autopilots.jsonl 持久化(next_run + fail_count + enabled)
```

## 试一试

```bash
python -m pytest tests/test_autopilot.py -v
python sessions/zh/s09_autopilot.py   # demo:autopilot 触发,issue+task 创建
```

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s09 |
|---|---|---|
| 调度 | autopilots(cron + webhook + 手动)(`autopilot.go:114,136,246,356`) | croniter tick |
| 执行模式 | create_issue(`dispatchCreateIssue`,`:573`)+ run_only(`dispatchRunOnly`,`:860`)两种 | 仅 create_issue(建 issue + enqueue task) |
| 失败处理 | 调度器 `sys_cron_executions` 有执行审计(`db_ops.go:277`);AutopilotTrigger 有 Enabled 字段(`models.go`),但未读到「连续 N 次失败自动禁用」逻辑 | 连续 5 次失败 → enabled=False(mica 自加,multiCa 未核实有此机制) |
| 触发器字段 | CronExpression/NextRunAt/Enabled/Timezone/WebhookToken(`models.go:AutopilotTrigger`) | cron/next_run/enabled/timezone(简化,无 webhook) |
| 持久化 | PostgreSQL | autopilots.jsonl |
| UTC 默认 | `DefaultAutopilotTriggerTimezone = "UTC"`(`autopilot.go:44`) | timezone 默认 "UTC"(同) |
