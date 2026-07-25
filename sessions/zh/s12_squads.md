# s12 — Squads(可选附录)

s01-s11 是 mica 的主线。s12 是可选附录:leader agent 把派给 squad 的 issue 委派给成员 agent,实现团队路由。生产 multiCa 有 squads 功能(README "Squads"),mica 做最简版。

## 一、核心设计思想

**SquadRouter:leader 选成员 → 入队 task**。一条 issue 派给 squad 后,leader agent 选一个成员 agent(deterministic round-robin),为成员入队一条新 task:

```python
class SquadRouter:
    def route(self, issue_id, squad_id) -> str | None:
        squad = self._squads.get(squad_id)
        if not squad or not squad.member_agent_ids:
            return None
        # round-robin(deterministic per issue)
        idx = int(hashlib.md5(issue_id.encode()).hexdigest(), 16) % len(squad.member_agent_ids)
        chosen = squad.member_agent_ids[idx]
        self.task_store.enqueue(issue_id, "agent", chosen)
        return chosen
```

选完后,成员 agent 的 task 进正常 daemon 认领流程。leader 不执行,只路由。

## 二、为什么这么设计

### 为什么 round-robin(deterministic)?

不依赖随机数(测试确定性),每个 issue 的路由结果固定(md5(issue_id) % 成员数)。同一个 issue 重跑不会换 agent。如果需要负载均衡(按成员当前 task 数选),那是更复杂的生产化主题。

### 为什么 leader 不执行,只路由?

leader 的职责是"分配",不是"干"。leader 看到派给 squad 的 issue,选一个成员,入队 task,然后自己继续做别的。成员 agent 的 daemon 认领这条 task 执行。这和 multiCa 不同:multiCa 的工作只给 **leader agent** 自己执行(`EnqueueTaskForSquadLeader`,`task.go:1081`),leader agent 自己决定怎么干,**不 fan-out 到成员**(`multica-squads/SKILL.md:173,240`)。mica 简化为代码 round-robin 选成员——没有 leader agent 的智能判断,但路由确定、可测试。

## 试一试

```bash
python -m pytest tests/test_squads.py -v
python sessions/zh/s12_squads.py   # demo:squad 路由到成员 agent
```

## 三、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s12 |
|---|---|---|
| squad 模型 | Squad{LeaderID, Instructions,...}(`models.go:836`)+ SquadMember(`models.go:851`) | Squad{id, name, leader_agent_id, member_agent_ids} |
| 路由机制 | 工作只给 **leader agent**(`EnqueueTaskForSquadLeader`,`task.go:1081`),leader agent 自己决定怎么干;"is leader routing, **not** member fan-out"(`multica-squads/SKILL.md:173,240`) | 代码 round-robin 选**成员** agent 执行 |
| 持久化 | PostgreSQL(Squad + SquadMember 表) | 内存(dict) |
| 成员负载均衡 | 无(工作不 fan-out 到成员) | 无(deterministic round-robin 选成员) |
