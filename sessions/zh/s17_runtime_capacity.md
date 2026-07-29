# s17 — Runtime Capacity / Slot:先有执行空位,再 claim task

s15 让 daemon 能在后台自动消费 queued task。s16 让 task 执行前的 agent、issue、project、skill 上下文可以写入 workdir。s17 补的是另一个执行系统必须有的约束:

```text
runtime 的本地执行容量是有限的。
```

如果一个 runtime 同时只能跑 2 个 task,daemon 就不能一次 claim 3 个 task。claim 不是普通读取。claim 成功后,task 会从 `queued` 变成 `dispatched`,其他 worker 通常不能再拿这个 task。

s17 的核心不变量是:

```text
先 acquire 本地 slot
再 claim task
claim 到 task 后绑定 slot
task 结束后 release slot
release 后 wakeup worker
```

这个不变量可以简写为:

```text
slot-before-claim
```

## 1. 为什么需要 Slot

假设现在有 3 个 queued task:

```text
task A
task B
task C
```

runtime `rt-1` 的容量是:

```text
max_slots = 2
```

如果 worker 不检查容量,直接 claim:

```text
claim A -> dispatched
claim B -> dispatched
claim C -> dispatched
```

这会制造一个错误状态:

```text
A 可以执行
B 可以执行
C 已经不在 queued 队列,但本机没有 slot 执行它
```

task C 被提前拿走了。它既不能被其他 worker 处理,本机也不能立刻执行。真实系统里这会增加超时恢复、orphan recovery、用户可见卡住等复杂度。

正确流程是:

```text
slot 0 空闲 -> claim A -> slot 0 running A
slot 1 空闲 -> claim B -> slot 1 running B
没有 slot   -> 不 claim C
```

task C 应该保持:

```text
status = queued
```

等 A 或 B 完成:

```text
A completed
release slot 0
wakeup worker
slot 0 空闲 -> claim C -> slot 0 running C
```

这就是 s17 的学习目标:不是为了做复杂并发,而是为了保证没有本地执行能力时,不要把 task 从队列里拿走。

## 2. Slot 在代码里是什么

s17 用 `RuntimeSlot` 表达一个执行名额:

```python
@dataclass(frozen=True)
class RuntimeSlot:
    runtime_id: str
    id: int
    task_id: str = ""
```

三个字段分别是:

```text
runtime_id
  这个 slot 属于哪个 runtime,例如 rt-1

id
  slot 编号,例如 0、1

task_id
  当前 slot 绑定的 task。刚 acquire 时为空,claim 到 task 后再填入。
```

如果创建:

```python
slots = RuntimeSlotPool(runtime_id="rt-1", max_slots=2)
```

含义是:

```text
rt-1 有两个本地执行名额:
  slot 0
  slot 1
```

slot 不是线程,也不是 task。slot 只是容量记录:

```text
这个 runtime 现在还有没有能力再接一个 task?
```

## 3. RuntimeSlotPool 的状态

`RuntimeSlotPool` 内部有两份状态:

```python
self._available: list[int] = list(range(max_slots))
self._occupied: dict[int, RuntimeSlot] = {}
self._lock = threading.Lock()
```

初始化 `max_slots=2` 时:

```text
_available = [0, 1]
_occupied = {}
```

### 3.1 acquire()

`acquire()` 预留一个 slot:

```python
def acquire(self) -> RuntimeSlot | None:
    with self._lock:
        if not self._available:
            return None
        slot_id = self._available.pop(0)
        slot = RuntimeSlot(self.runtime_id, slot_id)
        self._occupied[slot_id] = slot
        return slot
```

状态变化:

```text
before:
  _available = [0, 1]
  _occupied = {}

after acquire slot 0:
  _available = [1]
  _occupied = {
    0: RuntimeSlot(runtime_id="rt-1", id=0, task_id="")
  }
```

注意:这里已经占用了 slot,但还没有 task_id。原因是 s17 要先确认本机有空位,然后才 claim task。

### 3.2 bind_task()

`bind_task()` 把刚 claim 到的 task 写到 slot 上:

```python
def bind_task(self, slot_id: int, task_id: str) -> RuntimeSlot:
    with self._lock:
        if slot_id not in self._occupied:
            raise KeyError(f"slot not occupied: {slot_id}")
        slot = RuntimeSlot(self.runtime_id, slot_id, task_id)
        self._occupied[slot_id] = slot
        return slot
```

调用顺序是:

```python
slot = slot_pool.acquire()
task = daemon.claim_next()
bound_slot = slot_pool.bind_task(slot.id, task.id)
```

状态变化:

```text
before bind:
  slot 0 -> task_id=""

after bind:
  slot 0 -> task_id="<task-A-id>"
```

`bind_task()` 不执行 task。它只记录:

```text
slot 0 当前被 task A 占用
```

这个记录用于调试、测试、后续前端展示,也让 slot pool 的状态更可读。

### 3.3 release()

`release()` 释放 slot:

```python
def release(self, slot_id: int) -> None:
    with self._lock:
        if slot_id not in self._occupied:
            return
        del self._occupied[slot_id]
        if slot_id not in self._available:
            self._available.append(slot_id)
            self._available.sort()
```

状态变化:

```text
before:
  _available = []
  _occupied = {
    0: RuntimeSlot("rt-1", 0, "task-A"),
    1: RuntimeSlot("rt-1", 1, "task-B")
  }

after release slot 0:
  _available = [0]
  _occupied = {
    1: RuntimeSlot("rt-1", 1, "task-B")
  }
```

释放动作必须发生在两个场景:

```text
claim 不到 task
  acquire 了 slot,但队列为空,要立即 release

task 执行结束
  无论成功还是失败,都要 release
```

否则 runtime 容量会被永久占住。

## 4. SlotDaemonWorker 的主循环

s17 没有改 `Daemon.execute()`。执行 task 的逻辑仍然在 s14 的 `Daemon` 中。s17 只改调度方式。

核心代码是 `SlotDaemonWorker._loop()`:

```python
def _loop(self) -> None:
    while not self._stop.is_set():
        dispatched = False
        while not self._stop.is_set():
            slot = self.slot_pool.acquire()
            if slot is None:
                break

            task = self.daemon.claim_next()
            if task is None:
                self.slot_pool.release(slot.id)
                break

            bound_slot = self.slot_pool.bind_task(slot.id, task.id)
            self.claimed_count += 1
            dispatched = True
            self._dispatch(task, bound_slot)

        if dispatched:
            continue

        self._wakeup.wait(self.poll_interval)
        self._wakeup.clear()
```

逐段看。

### 4.1 先拿 slot

```python
slot = self.slot_pool.acquire()
if slot is None:
    break
```

这里的含义是:

```text
没有本地执行空位
  -> 不调用 daemon.claim_next()
  -> 不改变任何 task 状态
```

这是 s17 最重要的边界。没 slot 时,queued task 必须继续 queued。

### 4.2 有 slot 才 claim

```python
task = self.daemon.claim_next()
if task is None:
    self.slot_pool.release(slot.id)
    break
```

这里已经预留了一个 slot。如果没有 queued task 可 claim,必须马上释放 slot。

否则会出现:

```text
没有 task 在跑
但 slot 被空占
```

### 4.3 claim 后绑定 slot

```python
bound_slot = self.slot_pool.bind_task(slot.id, task.id)
self.claimed_count += 1
dispatched = True
self._dispatch(task, bound_slot)
```

`daemon.claim_next()` 会把 task 从:

```text
queued -> dispatched
```

`bind_task()` 把这个 dispatched task 和 slot 关联起来。随后 `_dispatch()` 开始真正执行。

### 4.4 dispatched 后继续循环

```python
if dispatched:
    continue
```

这和 s15 的 `drained -> continue` 类似。只要这一轮成功派发过 task,worker 不立刻睡眠,而是马上回到循环顶部继续检查:

```text
还有没有空 slot?
还有没有 queued task?
```

所以 `max_slots=2` 且队列有 A/B/C 时,worker 会在一轮中做:

```text
acquire slot 0 -> claim A -> dispatch thread A
acquire slot 1 -> claim B -> dispatch thread B
acquire slot   -> None
break
continue
下一轮发现还是 no slot
wait
```

task C 在没有 slot 时不会被 claim。

## 5. start thread 到底做了什么

`_dispatch()` 会创建一个新线程:

```python
def _dispatch(self, task: Task, slot: RuntimeSlot) -> None:
    thread = threading.Thread(
        target=self._run_in_slot,
        args=(task, slot),
        name=f"mica-slot-{slot.id}",
        daemon=True,
    )
    self._task_threads.append(thread)
    thread.start()
```

这里的 `thread.start()` 是真正启动 Python 线程。新线程执行:

```python
self._run_in_slot(task, slot)
```

也就是:

```python
def _run_in_slot(self, task: Task, slot: RuntimeSlot) -> None:
    try:
        self.daemon._record(task.id, {
            "type": "slot_acquired",
            "task_id": task.id,
            "runtime_id": slot.runtime_id,
            "slot_id": slot.id,
        })
        self.daemon.execute(task)
    finally:
        self.slot_pool.release(slot.id)
        self.completed_count += 1
        self.daemon._record(task.id, {
            "type": "slot_released",
            "task_id": task.id,
            "runtime_id": slot.runtime_id,
            "slot_id": slot.id,
        })
        self.wakeup()
```

新线程负责四件事:

```text
1. 记录 slot_acquired 事件
2. 调用 daemon.execute(task)
3. finally 中释放 slot
4. 唤醒 worker,让 worker 继续检查 queued task
```

真正执行 task 的仍然是:

```python
self.daemon.execute(task)
```

它会进入 s14 的执行链路:

```text
task dispatched -> running
解析 issue / workdir / agent / skills
runner.run(...)
task completed 或 failed
issue 状态更新
session events 记录
```

s17 的线程只负责让多个 slot 可以同时占用。它不重写 runner 逻辑。

## 6. 为什么要用线程

如果不开线程,即使有 2 个 slot,执行仍然会变成串行:

```text
claim A
execute A 等 A 完成
claim B
execute B 等 B 完成
claim C
execute C
```

这样无法验证 runtime capacity。因为任意时刻都只有一个 task running。

开线程后:

```text
slot 0 -> claim A -> thread A 执行
slot 1 -> claim B -> thread B 执行
没有 slot -> C 保持 queued
```

线程 A 或 B 结束后:

```text
release slot
wakeup worker
worker 再 claim C
```

s17 的线程是教学用受控并发。它不启动真实 `codex` / `claude`。真实 CLI 并发会引入取消、超时、文件写入竞争、模型资源消耗等问题,这些应该放到后续章节处理。

## 7. SlowRecordingRunner 如何证明并发上限

s17 用 `SlowRecordingRunner` 模拟长任务:

```python
class SlowRecordingRunner:
    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
```

执行时:

```python
with self._lock:
    self.calls.append(task.id)
    self.active += 1
    self.max_active = max(self.max_active, self.active)

time.sleep(self.delay)

with self._lock:
    self.active -= 1
```

`active` 表示当前同时在 runner 中的 task 数量。`max_active` 表示测试期间观察到的最大并发数。

如果 `max_slots=2`,测试应该满足:

```text
runner.max_active <= 2
```

这不是为了证明 Python 线程性能,而是为了证明:

```text
SlotDaemonWorker 没有派发超过 runtime capacity 的 task。
```

## 8. 测试在验证什么

s17 的测试文件是 [tests/test_runtime_capacity.py](../../tests/test_runtime_capacity.py)。

### 8.1 slot pool 基本状态

`test_slot_pool_acquire_bind_release()` 验证:

```text
max_slots=2
acquire 两次成功
第三次 acquire 返回 None
bind_task 后 occupied 里能看到 task_id
release 后 slot 回到 available
```

这个测试锁定 `RuntimeSlotPool` 的基本状态机。

### 8.2 没有 slot 时不提前 claim

`test_worker_does_not_claim_more_than_available_slots()` 是 s17 最关键的测试。

它创建:

```text
max_slots = 2
task A/B/C
runner delay = 0.2s
```

然后等待系统进入:

```text
2 个 task running
1 个 task queued
```

断言:

```python
assert list(statuses.values()).count(TaskStatus.RUNNING) == 2
assert list(statuses.values()).count(TaskStatus.QUEUED) == 1
assert runner.max_active <= 2
```

这证明第三个 task 没有在无 slot 时被提前 claim。

### 8.3 release 后继续 claim

`test_released_slot_wakes_worker_and_claims_queued_task()` 验证:

```text
A/B 先占满两个 slot
A 或 B 完成
slot 释放
worker 被 wakeup
C 被 claim 并执行
最终三个 task 都 completed
```

关键断言:

```python
assert worker.claimed_count == 3
assert worker.completed_count == 3
assert slots.available() == 2
assert runner.max_active <= 2
```

这证明 slot 会回收,并且回收后 worker 不需要等完整 `poll_interval`。

### 8.4 没 task 时不泄漏 slot

`test_worker_releases_slot_when_no_task_is_claimable()` 验证:

```text
worker 空跑
acquire slot
claim_next() 返回 None
release slot
```

最终:

```python
assert slots.available() == 1
assert slots.occupied() == {}
assert worker.claimed_count == 0
```

这个测试防止空队列场景把 slot 永久占住。

## 9. 与 multiCa 的对应关系

multiCa 的 daemon poller同样遵守 `slot-before-claim`。核心思想是:

```text
先拿本机执行 slot
再向 server claim task
claim 数量不能超过空闲 slot 数
task 结束后释放 slot
释放 slot 后唤醒 poller
```

对应关系:

| 学习点 | mica s17 | multiCa |
|---|---|---|
| runtime 容量 | `RuntimeSlotPool(max_slots)` | `MaxConcurrentTasks` |
| slot 表达 | `RuntimeSlot(id, task_id)` | `newTaskSlotSemaphore(...)` |
| claim 前拿 slot | `slot_pool.acquire()` | `waitForTaskSlot(...)` |
| 一次拿多个空位 | worker 循环 acquire | `drainAvailableSlots(...)` |
| 按容量 claim | 每个 slot claim 一个 task | `ClaimTasksForRuntimes(..., maxTasks=len(slots))` |
| task 结束释放 | `slot_pool.release(slot.id)` | `sem <- slot` |
| 释放后唤醒 | `worker.wakeup()` | `signalPollerWakeup(wakeup)` |
| 本地执行 | `daemon.execute(task)` | `handleTask()` / `runTask()` |

s17 没有实现 multi-runtime batch claim。它现在是:

```text
一个 RuntimeSlotPool
一个 worker 循环
每个 slot claim 一个 task
```

multiCa 生产版会把多个 runtime 的空闲容量合并起来,一次向 server 批量 claim:

```text
runtimeIDs = allRuntimeIDs()
slots = available local slots
ClaimTasksForRuntimes(runtimeIDs, maxTasks=len(slots))
```

但调度不变量是一致的:

```text
没有本地 slot,就不要 claim server task。
```

## 10. 和 s15/s16 的关系

s15:

```text
后台 worker 自动醒来
反复调用 daemon.tick()
```

s16:

```text
task 执行前
把 agent/issue/project/skill 写到 workdir
```

s17:

```text
后台 worker 自动醒来
先检查 runtime slot
有 slot 才 claim task
task 在线程中执行
完成后释放 slot 并唤醒 worker
```

所以 s17 不是替代 s15/s16。它补的是执行调度层:

```text
runtime 同时能接多少 task?
什么时候允许 claim task?
task 结束后如何把容量还回去?
```

## 试一试

```bash
python -m pytest tests/test_runtime_capacity.py -v
python sessions/zh/s17_runtime_capacity.py
```

demo 会创建 3 个 queued task,但 runtime 只有 2 个 slot。你应该看到:

```text
max concurrent runner calls: 2
```

这说明:

```text
前两个 task 同时执行
第三个 task 在没有 slot 时保持 queued
slot 释放后第三个 task 才被 claim
```
