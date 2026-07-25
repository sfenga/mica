# s10 — 多 CLI + PATH 探测(1 daemon : N agent)

s02-s09 的 daemon 是 1:1(认领一个 agent 的 task)。s10 泛化为 1:N:一个 daemon 认领本 runtime 上**所有** agent 的 queued task,不按 agent_id 过滤。还加了 `PathDetector` 探测 PATH 上的 CLI,和 `MultiCliRunner` runner 注册表(为多 CLI 分派留接口)。

## 一、核心设计思想

第一个思想:**PathDetector 探测可用 CLI**。`shutil.which` 检查 PATH 上每个已知 CLI(claude/codex/copilot/opencode/cursor-agent),返回可用列表。daemon 启动时探测,知道本机能跑哪些 agent:

```python
class PathDetector:
    KNOWN_CLIS = ["claude", "codex", "copilot", "opencode", "cursor-agent"]

    @staticmethod
    def detect() -> list[str]:
        return [cli for cli in PathDetector.KNOWN_CLIS if shutil.which(cli)]
```

第二个思想:**Daemon 1:N 认领**。s02 的 `claim_next` 过滤 `assignee_id == self.agent.id`(1:1)。s10 改为只过滤 `assignee_type == "agent" && status == QUEUED`——不按 agent_id,认领本 daemon 上所有 agent 的 task:

```python
def claim_next(self) -> Task | None:
    candidates = [
        t for t in self.task_store.list()
        if t.assignee_type == "agent"
        and t.status == TaskStatus.QUEUED
    ]
    for task in candidates:
        if self.task_store.claim(task.id):   # CAS
            return task
    return None
```

一个 daemon 托管多个 agent(不同 assignee_id 的 task 都认领)。CAS 保证多 daemon 不会重复认领。

第三个思想:**MultiCliRunner 是 runner 注册表**。`register(cli, runner)` 注册,`has(cli)` / `available()` 查询。s10 里 daemon 仍用单一 runner(StubRunner 或 ClaudeCliRunner);MultiCliRunner 留接口,可扩展为按 `agent.cli` 字段分派到不同 CLI 的 runner。

第四个思想:**Runtime 标识 daemon 所在机器**。`Runtime{id, name, clis[]}`——记录 daemon 的机器名和可用 CLI 列表。s10 的 daemon 持 `runtime_id`(标识自己是哪个 runtime),但不过滤(claim_next 不按 runtime_id 过滤——简化,mica 单 daemon 单 runtime)。

## 二、为什么这么设计

### 为什么 1:N 不按 agent_id 过滤?

s02 的 1:1 是简化(一个 daemon = 一个 agent)。生产 multiCa 是一个 daemon 服务多个 agent(一台机器上装了 claude + codex,两个 agent 各有 task)。s10 去掉 agent_id 过滤,让一个 daemon 认领所有 agent 的 task——这就是 1:N。CAS 保证多 daemon 不会重复认领同一 task。

### 为什么 MultiCliRunner 不实际分派?

mica 只用 claude——不需要真正的"按 agent.cli 选不同 runner"。MultiCliRunner 是接口留位:`register("claude", ClaudeCliRunner())` + `register("codex", CodexRunner())` 后,dashboard 能看到本机支持哪些 CLI。真正分派(dashboard 按 agent.cli 选 runner)留给扩展。

### 为什么 Runtime 不用于过滤?

mica 是单 daemon 单 runtime——所有 task 都在本 daemon 上。生产 multiCa 有多 runtime(多台机器),task 有 `runtime_id` 标识该跑在哪台;daemon 按 runtime_id 过滤认领自己的 task。mica 简化:不按 runtime_id 过滤,所有 agent task 都认领。

## 三、1:N 认领怎么流转

```
  多个 agent 各有 queued task(agent-1, agent-2, ...)
        │
  Daemon 1:N claim_next:
        │  过滤: assignee_type=="agent" && status==queued (不按 agent_id)
        │  CAS: queued → dispatched (只一个 daemon 赢)
        ▼
  逐个执行 → task completed → issue in_review
```

## 试一试

```bash
python -m pytest tests/test_multicli.py -v
python sessions/zh/s10_multicli.py   # demo:PathDetector + 1:N 两个 agent task 都认领
```

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(CLAUDE.md/README) | mica s10 |
|---|---|---|
| daemon:agent | 1:N(一个 daemon 服务多 agent/runtime) | 1:N(同) |
| CLI 探测 | daemon 启动时自动探测 PATH | PathDetector.detect()(shutil.which) |
| 多 CLI 分派 | 按 agent.cli 字段选不同 backend | MultiCliRunner 注册表(接口留位,实际用单一 runner) |
| runtime 过滤 | task 有 runtime_id,daemon 按 runtime_id 认领 | 无过滤(单 daemon 单 runtime) |
| CAS | server 端 ClaimAgentTask(DB 行级) | TaskStore.claim(内存锁) |
