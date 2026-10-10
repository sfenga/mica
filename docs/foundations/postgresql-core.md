# Mica 的 PostgreSQL 核心用法

本文介绍 Mica 后端依赖的 PostgreSQL 核心机制与真实语句，面向两类读者：刚接触代码库、想快速理解"并发正确性是靠什么保证的"的开发者；以及需要讲解这部分实现的面试者。

Mica 没有使用 ORM：表结构定义在版本化迁移目录 `mica/infrastructure/database/sql/`，运行时语句全部内联在两个仓储类中——核心表在 `mica/infrastructure/database/repositories.py`，渠道与 Outbox 在 `mica/infrastructure/database/channel_repository.py`。这样做的好处是每条并发相关语句都能在代码里直接审阅，代价是手写映射。

阅读顺序建议：先理解第 1 节的事务模型（所有正确性论证的前提），再按业务链路读第 2～7 节。每条机制都给出真实 SQL 与代码位置。

## 1. 事务模型：thread-local 可重入连接

所有正确性论证的前提是"哪些写操作在同一个事务里"。`PostgresDatabase`（`mica/infrastructure/database/connection.py`）用线程局部状态实现可重入事务：

```python
@contextmanager
def transaction(self) -> Iterator[None]:
    active = getattr(self._state, "connection", None)
    if active is not None:
        yield            # 外层已有事务：直接加入，不开新连接
        return
    with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
        self._state.connection = connection
        try:
            yield
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._state.connection = None
```

效果：应用服务（如 `IngressService.submit()`）开启事务后，它调用的其他服务和仓储的 `connection()` / `transaction()` 全部汇入同一条连接、同一个事务。这就是"Issue 与首个 Task 同事务落库""评论与任务合并同事务"的实现基础——不需要把连接对象层层传递。

模式迁移（`migrations.py`）同样跑在这条连接上：启动时按文件名版本顺序执行未应用的 `*.up.sql`，并用 `pg_advisory_xact_lock(hashtext('mica-schema-migrations'))` 防止多实例同时迁移。

## 2. 幂等预留：ON CONFLICT DO NOTHING + RETURNING

**场景**：飞书 webhook 至少一次投递、Web 端带 `Idempotency-Key` 重试——同一外部事件可能到达多次，只能处理一次。

`external_event` 表以 `(channel, tenant_key, external_event_id)` 为主键（`sql/003_channels_and_outbox.up.sql`），预留语句在 `channel_repository.py` `reserve_event()`：

```sql
INSERT INTO external_event(channel, tenant_key, external_event_id, event_type, payload_hash, issue_id, created_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (channel, tenant_key, external_event_id) DO NOTHING
RETURNING *
```

`RETURNING *` 配合 `DO NOTHING` 构成一个原子判别：**插入成功会返回新行，冲突则返回 None**——应用层据此区分"我是第一个处理者"和"这是重复事件"，不需要先 SELECT 再 INSERT（那样两个并发请求会同时查到"不存在"）。

重复事件时，Python 侧用 `or` 短路读回已存在的行（`channel_repository.py:110-117`）：

```python
row = (
    inserted                # 插入成功的新行；冲突时为 None
    or connection.execute(  # inserted 为 None 才会执行这条 SELECT
        """SELECT * FROM external_event
        WHERE channel=%s AND tenant_key=%s AND external_event_id=%s FOR UPDATE""",
        (event.channel, event.tenant_key, event.external_event_id),
    ).fetchone()
)
```

`inserted or ...` 是短路求值：插入成功就直接用新行，不执行 SELECT；冲突（`inserted is None`）才按同一个主键把别人插入的行读出来。两条路殊途同归，`row` 总是这个幂等键的官方记录，函数返回 `(event, inserted is not None)`——第二个值告诉调用方"你是不是第一个处理者"。

**为什么冲突时读到的行一定是已提交的**：`INSERT ... ON CONFLICT DO NOTHING` 遇到尚未提交的并发插入时会**阻塞等待**（speculative insertion）——主键被占着，但 PG 不知道对方会提交还是回滚，无法立即判断冲突是否成立，只能等。对方提交，本方走 DO NOTHING；对方回滚，本方插入成功。时间线：

```text
时刻1  A：INSERT 成功（占住主键，尚未 COMMIT）
时刻2  B：INSERT 同一主键 → 卡住，等 A 出结果
时刻3  A COMMIT → B 走 DO NOTHING，inserted = None
       （若 A ROLLBACK → B 插入成功，B 转而成为处理者）
时刻4  B：SELECT ... FOR UPDATE 读回 A 提交的行
```

所以阻塞发生在 INSERT，而不是 SELECT；INSERT 返回 None 时，"赢的事务"已经落定，行必然可见。

**FOR UPDATE 的作用**：INSERT 的等待只解决"谁是处理者"，不保护后续判断。重复请求接下来要读这行的 `payload_hash` 和 `issue_id` 来决定走哪个分支，行锁持有到本事务结束，保证判断期间这行不会被其他并发的重复请求改写，多个重复请求也因此串行处理。之后 `submit()` 分三种情况：`payload_hash` 不同报幂等冲突；hash 相同但 `issue_id` 为空（防御分支——正常流程里事件行与 `attach_issue` 同事务提交，已提交的行必然带 `issue_id`）报"处理中"；hash 相同且有 `issue_id` 则直接返回已有 Issue。

要点：**唯一性约束放在数据库写入时成立，而不是应用层先查后写**。这是全项目并发设计的第一原则。

## 3. 咨询锁：pg_advisory_xact_lock

本节涉及用户相关的两张表（均建于 `sql/003_channels_and_outbox.up.sql`）：

```sql
CREATE TABLE platform_user (              -- 平台内部用户
    id TEXT PRIMARY KEY,                  -- 应用层生成，如 user_xxx
    workspace_id TEXT NOT NULL,           -- 所属工作区
    display_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE external_identity (          -- 外部渠道身份 → 平台用户的映射
    channel TEXT NOT NULL,                -- web / feishu
    tenant_key TEXT NOT NULL,             -- 渠道内的租户命名空间
    external_user_id TEXT NOT NULL,       -- 渠道侧的用户标识（如飞书 open_id）
    user_id TEXT NOT NULL REFERENCES platform_user(id),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (channel, tenant_key, external_user_id)
);
```

分工是"内部统一、外部多变"：`platform_user` 是系统内部的用户实体，Issue 通过 `created_by_user_id` 引用它（003 迁移同时给 issue 加了这一列）；`external_identity` 记录"某个渠道、某个租户里的某个外部账号"对应哪个内部用户。主键 `(channel, tenant_key, external_user_id)` 保证一个外部账号只映射到一个内部用户；反过来一个内部用户可以有**多条** identity（同一个人在 Web 和飞书是两个外部身份，归入同一内部用户）。这样下游的 Issue、Comment 只引用稳定的内部 `user_id`，不感知渠道差异。

**场景**：`resolve_identity()` 要把外部渠道用户映射成平台用户——"查身份 → 不存在则建新用户 → 建身份绑定"三步（`channel_repository.py`）。

**并发从哪来**：竞态发生在**同一用户最早的几次请求恰好并发**时。最典型的是飞书新用户在群里连发两条消息，平台产生两个 `event_id` 不同的 webhook 事件——`reserve_event` 拦不住它们（两个都是合法的新事件，不是重发），两个事务同时走到建档这一步，同时查到"不存在"。Web 侧同理：同一用户开两个标签页、用不同 `Idempotency-Key` 同时提交。FastAPI 的同步端点跑在线程池里，单进程内就有真并发。窗口虽然窄（身份建好之后都走查询路径），撞上一次的代价却不小。

**为什么不能像第 2 节那样"插入即仲裁"**：这里要写**两张表**——先建 `platform_user`（id 由应用层随机生成），再建 `external_identity` 指向它。不用锁的话，两个并发事务各建一个**不同 id** 的 user，然后在 `external_identity` 主键上撞车：输家整个事务回滚报错，还多出一个没人引用的孤儿 user。行锁也帮不上忙——行锁锁的是已存在的行，而这里的行还不存在。

所以 `resolve_identity()` 在事务内先拿一把**咨询锁**：PG 提供的按整数编号互斥的锁，不绑定任何表或行，编号的含义由应用层自己约定，PG 只保证同一编号同时只有一个事务持有：

```sql
SELECT pg_advisory_xact_lock(hashtext(%s))   -- 键形如 identity:feishu:tenant:user
```

- `hashtext()` 把字符串折叠成固定整数：同一用户的建档请求抢的是同一个编号。
- `pg_advisory_xact_lock()` 申请该编号，被占就排队；`xact` 后缀表示锁随事务结束自动释放，不存在忘记解锁的问题。
- 锁在数据库里，跨线程、跨进程、跨实例都有效（进程内互斥锁做不到这一点）。

时间线：

```text
时刻1  A：拿锁成功 → 查身份 → 不存在 → 建 user + 建绑定 → COMMIT（锁自动释放）
时刻2  B：申请同编号锁 → 排队等 A
时刻3  B 拿到锁 → 重查 → 读到 A 已提交的行 → 直接返回，零插入
```

注意 B 醒来后**必须重查**：排队之前查到的"不存在"已经过时。咨询锁把"查 + 插"三步整体串行化，输家根本不走插入路径。迁移过程用同一机制的另一个编号（`mica-schema-migrations`）防止多实例并发跑 DDL。

**不用锁会怎样**：数据不会坏（`external_identity` 主键是最后防线），坏的是请求会报错失败。无锁时两个并发事务的时间线：

```text
时刻1  A、B 同时 SELECT：身份不存在（都看到"没有"）
时刻2  A、B 各建一个 platform_user：都成功
       ——user id 应用层随机生成、天生不撞，仲裁点不在这张表
时刻3  A、B 各建 external_identity：主键相同
       → A 先提交；B 的 INSERT 直接报 unique_violation
时刻4  B 整个事务回滚（含 reserve_event 插入的事件行）→ 请求返回 500
```

后果分渠道：飞书侧 webhook 返回非 2xx 会触发平台重试，第二次能成功——**能自愈**，但受理消息晚到几十秒，服务端留下一条容易误判的 unique_violation error 日志；Web 侧没有重试，用户提交直接失败，只能手动重发。

改用 `ON CONFLICT` 也不能等价替代：identity 插入静默不冲突后事务正常提交，投机创建的 `platform_user` 就成为**已提交的孤儿行**，每撞一次车多一条垃圾数据。这正是"插入即仲裁"模式不适用的原因——仲裁点（identity 表）引用的行（user 表）是投机创建的；第 2 节的 `reserve_event` 之所以干净，是因为事件行自己就是仲裁点。

一句话定位：主键约束保证"撞了不会错"，咨询锁保证"根本不会撞"——前者是数据底线，后者把并发首次建档从"报错回滚 + 渠道重试 + 孤儿行"变成安静的排队办理。

## 4. 部分唯一索引 + 条件 UPSERT：评论合并

**场景**：同一 Issue、同一 Agent 最多保留一个"待执行"任务；新评论到达时若已有 queued 任务就合并进去，否则新建。这是面试点"减少重复启动"的核心。

先建**部分唯一索引**（`sql/009_comment_routing_and_merge.up.sql`）——只约束 queued 状态的行，历史任务不受影响：

```sql
CREATE UNIQUE INDEX agent_task_one_queued_per_issue_agent_idx
    ON agent_task(issue_id, agent_id)
    WHERE status = 'queued';
```

然后 `enqueue_or_merge()`（`repositories.py:212`）用带索引谓词的 UPSERT：

```sql
INSERT INTO agent_task (...)
VALUES (...)
ON CONFLICT (issue_id, agent_id) WHERE status='queued'
DO UPDATE SET
    merged_comment_ids = array_remove(
        CASE WHEN agent_task.trigger_comment_id IS NULL
            THEN agent_task.merged_comment_ids
            ELSE array_append(agent_task.merged_comment_ids, agent_task.trigger_comment_id)
        END,
        EXCLUDED.trigger_comment_id
    ),
    trigger_comment_id = EXCLUDED.trigger_comment_id,
    ...
RETURNING ..., (xmax = 0) AS inserted
```

三个要点：

1. `ON CONFLICT ... WHERE status='queued'` 的谓词必须与部分索引的谓词一致，冲突检测才会落到这个索引上。
2. 冲突分支在任务行上维护两个评论字段的轮转：`trigger_comment_id` 是**当前最新**的触发评论（只有一个），`merged_comment_ids` 是**之前**合并进来的评论列表（原 Comment 行不动，合并关系记在任务上）。新评论合并进来时按三步轮转：

   ```text
   ① 老 trigger 降级入列表：CASE WHEN trigger_comment_id IS NULL THEN 列表原样
      （任务由"建 Issue"事件直接创建、没有评论 trigger）ELSE array_append(列表, 老 trigger)
   ② 新评论从列表中摘除：array_remove(①的结果, EXCLUDED.trigger_comment_id)
      ——EXCLUDED 指本次试图插入的新行；新评论即将上位为 trigger，
      不能同时留在列表里（也防御重复合并导致同一评论出现两次）
   ③ 新 trigger 上位：trigger_comment_id = EXCLUDED.trigger_comment_id
   ```

   顺序有语义：merged 列表保持先来后到，老 trigger 追加在末尾，claim 时新 trigger 拼在最后（见第 5 节），正好拼出时间序。示例：

   ```text
   C1 到达：建任务    trigger=C1, merged=[]
   C2 到达：合并      trigger=C2, merged=[C1]       （C1 降级入列表，C2 上位）
   C3 到达：合并      trigger=C3, merged=[C1, C2]   （C2 降级入列表，C3 上位）
   领取时：delivered = merged + [trigger] = [C1, C2, C3]，按序消费
   ```
3. `(xmax = 0) AS inserted` 是判断"本次是插入还是更新"的技巧：新插入的行 xmax 为 0，被更新的行非 0。应用层用它区分"新建了任务"和"合并进了已有任务"。

## 5. 任务领取：CTE + FOR UPDATE SKIP LOCKED + 输入快照

**场景**：多个 Daemon 并发轮询任务队列，同一任务只能被一个领取；且同一 Issue/Agent 已有活跃任务时不允许并发领取。这是全项目最复杂的一条语句（`repositories.py:302` `claim_next()`）：

```sql
WITH candidate AS (
    SELECT queued.id FROM agent_task AS queued
    JOIN issue AS owner_issue ON owner_issue.id=queued.issue_id
    JOIN agent AS assigned_agent ON assigned_agent.id=queued.agent_id
        AND assigned_agent.runtime_id=queued.runtime_id
    WHERE queued.runtime_id=%s AND queued.status='queued'
    AND owner_issue.status NOT IN ('done', 'cancelled')
    AND NOT EXISTS (                          -- 同一 Issue/Agent 已有活跃任务则跳过
        SELECT 1 FROM agent_task AS active
        WHERE active.issue_id=queued.issue_id AND active.agent_id=queued.agent_id
        AND active.status IN ('dispatched', 'running')
    )
    AND ( ... )                               -- Squad：有活跃成员时跳过 Leader 汇总任务
    ORDER BY queued.created_at
    FOR UPDATE OF queued, owner_issue SKIP LOCKED
    LIMIT 1
)
UPDATE agent_task AS task
SET status='dispatched', claimed_by=%s,
    delivered_comment_ids=(                  -- 领取时固化本轮输入快照
        SELECT COALESCE(array_agg(comment_id ORDER BY first_position), '{}'::text[])
        FROM (
            SELECT comment_id, min(position) AS first_position
            FROM unnest(
                task.merged_comment_ids ||
                CASE WHEN task.trigger_comment_id IS NULL THEN '{}'::text[]
                    ELSE ARRAY[task.trigger_comment_id] END
            ) WITH ORDINALITY AS comments(comment_id, position)
            GROUP BY comment_id
        ) AS ordered_comments
    ),
    lease_expires_at=now() + make_interval(secs => %s),
    attempt=task.attempt + 1, updated_at=now()
FROM candidate
WHERE task.id=candidate.id
RETURNING ...
```

五个机制各解决一个问题：

- **`FOR UPDATE ... SKIP LOCKED`**：多 worker 扫队列时，已被别人锁住的候选行直接跳过而不是等待，消除队头阻塞和重复领取。注意 `FOR UPDATE OF queued, owner_issue` 把锁限定在指定的两张表上（JOIN 还带了 agent 表，不锁它）。
- **`NOT EXISTS` 子查询**：领取层面强制"同一 Issue/Agent 串行"——前一个任务没结束，后续 queued 任务不会被领走。合并（第 4 节）管"数量"，这里管"执行先后"。
- **CTE + UPDATE ... FROM**：候选选择与状态更新合成一条语句原子完成，`RETURNING` 直接返回更新后的行。
- **`delivered_comment_ids` 快照**：领取的同一语句里把"本轮消费的评论集合"固化下来——`unnest(...) WITH ORDINALITY` 展开数组并保留顺序，按首次出现位置排序去重后重新聚合。此后生成 Prompt 只读这个集合，领取后才到达的评论不会混入本轮。
- **`make_interval(secs => %s)`**：租约到期时间在数据库侧用服务器时钟计算，避免应用服务器时钟偏差。

配套的两个部分索引（`sql/001_core.up.sql`）让候选扫描只看相关行：

```sql
CREATE INDEX agent_task_claim_idx ON agent_task(runtime_id, created_at) WHERE status = 'queued';
CREATE INDEX agent_task_expired_lease_idx ON agent_task(runtime_id, lease_expires_at)
    WHERE status IN ('dispatched', 'running');
```

## 6. 租约与恢复：条件更新三件套

任务被领取后靠"租约 + 代次"管理所有权，三条语句各司其职（都在 `repositories.py`）：

**续租**（`renew_lease()`）——WHERE 里做所有权校验，续不上返回空：

```sql
UPDATE agent_task SET lease_expires_at=now() + make_interval(secs => %s), updated_at=now()
WHERE id=%s AND claimed_by=%s AND status IN ('dispatched', 'running')
RETURNING id
```

`claimed_by=%s` 保证只有持有者优先续租；任务已被恢复或完成时影响行数为 0，Daemon 据此判断失租。

**崩溃恢复**（`recover_expired()`）——把租约过期的任务放回队列：

```sql
UPDATE agent_task SET status='queued', claimed_by=NULL, lease_expires_at=NULL, updated_at=now()
WHERE runtime_id=%s AND status IN ('dispatched', 'running') AND lease_expires_at <= now()
RETURNING ...
```

重新领取时 `attempt` 递增；旧 worker 的终态写回因 `worker_id + attempt` 校验失败而被拒绝（KI-003：这只能保护数据库状态，撤销不了已发生的副作用——at-least-once，不是 exactly-once）。

**取消**（`request_cancel()`）——CASE 表达式把状态机写进一条语句：

```sql
UPDATE agent_task SET
    status=CASE WHEN status IN ('queued', 'waiting_input') THEN 'cancelled' ELSE status END,
    cancel_requested=CASE WHEN status IN ('dispatched', 'running') THEN TRUE ELSE cancel_requested END,
    ...
WHERE id=%s RETURNING ...
```

未开始的直接取消；执行中的只置 `cancel_requested` 标记，由 Provider 轮询后终止进程组。

### 执行模型：执行线程与心跳线程

租约字段的生产者和消费者是两个线程（`mica/runtime/daemon.py`）。Daemon 没有使用协程——整条依赖链（psycopg、subprocess 管道、httpx 同步客户端、lark SDK）都是同步阻塞的，而心跳必须与"阻塞在执行中几分钟的线程"真并发，线程是与该阻塞模型匹配的最简工具：

```text
DaemonWorker 主循环（Thread，2s 轮询）
  └─ 执行线程（每 Slot 一个 Thread）→ Daemon.run_once()
       ├─ 领任务时写入初始租约（claim_next：claimed_by + attempt+1 + lease）
       ├─ 任务转 running 后 spawn 心跳线程（每任务一个）
       ├─ provider.run() 执行 Agent（可能数分钟）
       └─ finally：heartbeat_stop.set() → join(1s) → 释放 Slot
            └─ 心跳线程：每 heartbeat_seconds(30s) 醒一次续租
```

心跳循环（`_heartbeat()`，daemon.py:294）的核心语义：

- `stop.wait(heartbeat_seconds)` 一身二任：既是周期定时器，又能被 `set()` 立即唤醒，保证关停快；
- 死线语义：**租约有效期 = 上次成功续约 + `lease_seconds`**。续租抛异常（DB 抖动等，结果不确定）时按周期重试，超过死线仍失败才判定失租；返回 False（WHERE 未命中，任务确定已被回收或终态）则立即判定失租；
- 失租后 `lease_lost` 置位，与 `_shutdown`、库里的 `cancel_requested` 一起汇入 `cancellation_check()`，由 Provider 轮询后终止执行。

**线程配对的三层保证**（防止"执行线程死了、心跳还活着"）：

1. `run_once()` 的所有出口（成功、取消、异常）都经过同一个 `finally`，置 `stop` 并 join 心跳线程；
2. 兜底：`renew_lease` 的 WHERE 条件兼任心跳的"自杀开关"——任务离开 `dispatched/running` 后下一次续租必返回 False，孤儿心跳最多多活一个心跳周期即自行退出；它也无法给被回收的任务续租（`claimed_by` 已易主）；
3. 心跳是 daemon 线程，进程崩溃时随进程一起死，任务交给租约过期 + `recover_expired` 兜底。

**软终止模型**：Python 杀不了线程，双向都不做强杀——心跳发现失租只能置信号，靠 Provider 配合轮询；Provider 若不配合，执行线程跑完后终态写回仍会被 `worker_id + attempt` 校验拒绝（attempt 已被新领取者递增）。

已知边界见 [KI-003](../known-issues.md)：初始租约从领取起算，而心跳在准备工作（Worktree/Session）完成后才启动，准备阶段存在租约空窗；心跳线程若卡在挂起的数据库调用里会泄漏一个线程（资源问题而非正确性问题）。

## 7. Outbox 投递：同一套原语的组合

可靠回传飞书消息复用上面所有原语（`channel_repository.py`）。表上有 `UNIQUE (channel, source_event_id, message_type)` 去重，外加只扫 pending 行的部分索引 `outbox_claim_idx`。

**领取**（`claim_outbox()`）——先回收崩溃现场，再用 CTE + SKIP LOCKED 认领：

```sql
UPDATE outbox_message SET status='pending', claimed_by=NULL, lease_expires_at=NULL
WHERE status='sending' AND lease_expires_at <= now();      -- 回收：worker 发到一半挂了

WITH candidate AS (
    SELECT id FROM outbox_message
    WHERE status='pending' AND available_at <= now()
    ORDER BY available_at, created_at
    FOR UPDATE SKIP LOCKED LIMIT 1
)
UPDATE outbox_message AS message SET status='sending', claimed_by=%s,
    attempt=message.attempt + 1, lease_expires_at=now() + make_interval(secs => %s)
FROM candidate WHERE message.id=candidate.id RETURNING message.*;
```

**结果写回**（`mark_outbox_sent()` / `retry_outbox()`）——WHERE 带 `claimed_by + status='sending'` 所有权校验；失败重试用 `available_at = now() + make_interval(...)` 实现指数退避（退避秒数在应用层算 `min(300, 2^attempt)`）。

`created_at` 与 `available_at` 的分工：前者是审计字段（通知何时产生，不可变），后者是调度字段（何时可投递；初始等于创建时间，每次失败被推到未来）。认领语句里 `available_at <= now()` 是闸门（退避期内的消息不碰），`ORDER BY available_at, created_at` 保证最早该处理的先发、同刻按出生先后决胜。退避状态随行持久化，任何 worker 捡起来都知道何时该再试。

### 写入点与职责边界

Outbox 在项目里只服务一个方向：**飞书出站通知**。三个写入点都在 `application/channels.py`：

1. **新 Issue 受理**（`submit()` 新建分支，channels.py:153）：`issue.accepted`——"任务已受理"，与 Issue、首个 Task 同事务；
2. **补充受理**（`submit()` 的 `related_issue_id` 分支，channels.py:117）：`comment.accepted`——"补充要求已加入"，与评论合并同事务；
3. **任务终态通知**（`TaskNotificationProjector`，channels.py:209）：间接写入——Daemon 执行时落 `task_event`，Projector 每轮把终态事件投影成 Outbox。只投影五种：`task.completed`、`task.failed`、`task.input_required`、`task.cancelled`、`squad.completed`。Squad 场景有两道过滤：成员的 `task.completed` 跳过（不能把成员完成误报为整体完成），成员的 failed/cancelled 也跳过，只有 Leader 任务终态才通知——这就是"飞书只回传关键消息、成员完成被折叠"的实现处。

消费端只有一个：`ChannelDeliveryWorker`（`integrations/delivery.py`）每轮先跑 Projector 再认领投递，sender 按 channel 分发（当前只有 `FeishuMessageSender`）。去重分两层：`UNIQUE (channel, source_event_id, message_type)` 保证同一事实只留一条待发送记录（Projector 游标在内存，重启重放事件流时靠它兜底）；飞书 API 的 `uuid=message.id` 保证重试发送时不重复展示。

**不用 Outbox 的地方**同样是边界：Web 渠道走同步 HTTP 响应 + WebSocket 实时推送，不需要异步可靠投递；Agent 执行过程的流式事件（`agent.text`、`agent.tool`）只落 `task_event` 供 Web 时间线展示，不进 Outbox——飞书不逐条同步进度。

## 8. 行锁作为应用层临界区

除了上面的专用语句，还有两种"裸"行锁用法：

- `SELECT ... FOR UPDATE` 读 Issue 行（`repositories.py:109` `get_for_update()`）：Squad 的批次屏障在 Issue 行锁内检查"同批成员是否全部落定、是否已有待处理 Leader"，保证多个成员同时完成时**只唤醒一次** Leader——临界区用行锁而不是应用层互斥锁，多实例部署也成立。
- 第 2 节 `reserve_event` 的重复事件重读同理。

## 9. 类型与约束的取舍

- **`TEXT` 主键而非 UUID 类型**：ID 由应用层生成（如 `task_xxx`），便于日志辨认和跨表肉眼关联。
- **`JSONB`**：`agent.provider_config`（各 Provider 的异构配置）、`outbox_message.payload`（渠道消息体）——结构随 Provider/渠道变化，不值得建列。
- **`TEXT[]` 数组**：`merged_comment_ids`、`delivered_comment_ids`——只在任务行内消费的有序 ID 集合，不独立建关联表；配合 `array_append` / `array_remove` / `unnest WITH ORDINALITY` 操作。
- **CHECK 约束当枚举**：`status IN ('queued', 'dispatched', ...)` 等，状态域在库层兜底（005 迁移还给 outbox 状态加过 `failed` 值）。
- **`TIMESTAMPTZ`**：全部时间戳带时区；租约比较统一用数据库的 `now()`。
- **外键 + `ON DELETE CASCADE`**：评论、任务、事件随 Issue 级联删除；`ON DELETE SET NULL` 用于回复引用（如 `parent_comment_id`），删评论不炸回复链。

## 10. 没有用什么

明确不用的机制同样值得一提：

- **不用 ORM / 触发器 / 存储过程**：并发逻辑显式写在仓储方法里，可审阅、可测试（内存适配器实现同一契约，行为测试不依赖数据库）。
- **不用 LISTEN/NOTIFY**：任务唤醒是进程内 `Event`（daemon 轮询间隔 2 秒），跨进程通知尚未需要。
- **不依赖 READ COMMITTED 以上的隔离级别**：所有并发正确性来自唯一约束、行锁和条件更新，而不是 Serializable。


## 11. Chat 会话：全键组合唯一 + 任务形状分叉（migration 012）

Chat 模型引入了三个此前没用过的组合姿势。

**非哨兵不可的组合唯一键**：会话隔离键是 `(channel, tenant_key, conversation_id, thread_id)`，但 PostgreSQL 的 UNIQUE 把 NULL 视为彼此不同——两个"无话题"的普通群消息会各自建 binding，create/re-read 协议直接失效。解法是 `thread_id` 用 `''`（非 NULL）表示无话题，让组合唯一约束全键生效；领域对象上的 `Optional[str]` 由仓储翻译。这个取舍写在 `012_chat.up.sql` 头部注释里。

**不用异常做并发收敛**：`ensure_session()` 先 SELECT 再 INSERT session，binding 用 `ON CONFLICT ... DO NOTHING RETURNING id`——RETURNING 为空说明并发对手赢了 binding，此时同事务 `DELETE` 掉刚插的败者 session、重读胜者。整条路径没有异常控制流，也不需要嵌套事务回滚（对比 `reserve_event` 的异常转译风格：那里冲突本身是业务信号，这里冲突只是重试一次的读路径）。

**消息级去重下沉到表约束**：`chat_message` 上 `UNIQUE (chat_session_id, external_message_id)`，append 用 `ON CONFLICT DO NOTHING RETURNING` 判断是否新消息。飞书事件重推的全部幂等性由这一个约束承担——不需要独立去重表，因为"同一条外部消息属于同一个会话"是模型事实。

**任务形状分叉认领**：`claim_next()` 从 `JOIN issue` 改为 `LEFT JOIN issue` + `LEFT JOIN chat_session`，互斥子查询按形状分叉：

```sql
AND (
    (queued.issue_id IS NOT NULL
        AND owner_issue.status NOT IN ('done', 'cancelled'))
    OR (queued.chat_session_id IS NOT NULL
        AND owner_chat.status='active')
)
AND NOT EXISTS (
    SELECT 1 FROM agent_task AS active
    WHERE active.agent_id=queued.agent_id
    AND active.status IN ('dispatched', 'running')
    AND (
        (queued.issue_id IS NOT NULL AND active.issue_id=queued.issue_id)
        OR (queued.chat_session_id IS NOT NULL
            AND active.chat_session_id=queued.chat_session_id)
    )
)
```

NULL ≠ NULL 语义保证两个分支互不误伤——这是把"任务来源判别"显式写进 SQL 的代价，也是收益。教训一条：`FOR UPDATE` 不能加在外连接的可空侧（`FOR UPDATE OF owner_issue` 直接报 FeatureNotSupported），且这里也不需要——done 守卫没有竞态窗口（`transition_issue(DONE)` 要求无 active 任务，queued 计入 active；CANCELLED 转移自己取消排队任务），锁任务行就够了。

## 速查表

| 机制 | 语句位置 | 迁移 |
|---|---|---|
| 事件幂等预留 | `channel_repository.py` `reserve_event()` | 003 |
| 身份建档咨询锁 | `channel_repository.py` `resolve_identity()` | 003 |
| 评论合并 UPSERT | `repositories.py:212` `enqueue_or_merge()` | 009（部分唯一索引） |
| 任务领取 + 输入快照 | `repositories.py:302` `claim_next()` | 001（索引）、009（快照字段） |
| 租约续期 / 恢复 / 取消 | `repositories.py` `renew_lease()` / `recover_expired()` / `request_cancel()` | 001、002 |
| Outbox 领取 / 重试 | `channel_repository.py` `claim_outbox()` 等 | 003、005 |
| Squad 批次临界区 | `repositories.py:109` `get_for_update()` + `application/squads.py` | 004 |
| 迁移版本锁 | `migrations.py` | — |
| Chat binding 竞态收敛 / 消息去重 | `chat_repository.py` `ensure_session()` / `append_message()` | 012 |
| Chat 会话认领互斥 | `repositories.py` `claim_next()`（形状分叉） | 012 |

相关文档：[架构基线](../architecture.md)、[已知问题](../known-issues.md)、[面试手册 01 多渠道触发与合并](../interview/01-multi-channel-trigger-and-merge.md)。
