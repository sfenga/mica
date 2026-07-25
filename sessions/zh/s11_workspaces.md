# s11 — 多 workspace 隔离 + 成员资格闸门

s10 让一个 daemon 能认领多个 agent 的 task 了,但所有 issue 都在同一个 workspace——没有"团队 A 看不到团队 B 的 issue"这层隔离。s11 加上 workspace 过滤 + 成员资格校验:每条 issue/task 查询带 `workspace_id` 过滤,非成员访问被拒。

## 一、核心设计思想

第一个思想:**workspace_id 过滤**。IssueStore.list(workspace_id) 只返回该 workspace 的 issue——跨 workspace 不串:

```python
def list(self, workspace_id=None, member_id=None, member_store=None):
    if member_id and member_store:
        if not member_store.is_member(workspace_id, member_id):
            raise PermissionError(...)
    return [i for i in self._issues.values()
            if workspace_id is None or i.workspace_id == workspace_id]
```

这个过滤从 s01 就有(`list(workspace_id)`),s11 只是加了成员校验层。s01-s10 的所有测试不传 member_id → 不校验 → 不受影响。

第二个思想:**成员资格闸门**。`MemberStore` 存 `workspace → member_id 列表`(`members.jsonl`)。IssueStore.list/create 带 `member_id` + `member_store` 时,先校验 member 是否属于该 workspace,非成员抛 `PermissionError`:

```python
class MemberStore:
    def is_member(self, ws_id, member_id) -> bool:
        return member_id in self._members.get(ws_id, set())

    def add_member(self, ws_id, member_id):
        self._members.setdefault(ws_id, set()).add(member_id)
        self._persist(...)
```

第三个思想:**member_id/member_store 可选,向后兼容**。`list(workspace_id, member_id=None, member_store=None)`——不传就不校验(纯过滤),传了才校验。s01-s10 的测试不传 → 不校验 → 96 个测试不受影响。

## 二、为什么这么设计

### 为什么不只过滤 workspace_id,还要成员校验?

只过滤 workspace_id 意味着任何知道 workspace_id 的人都能查——没有访问控制。成员校验加了一层"你属于这个 workspace 吗":非成员即使知道 workspace_id 也被拒。这对应生产 multiCa 的"membership gates access"(CLAUDE.md 明确)。

### 为什么 member_id/member_store 可选?

s01-s10 的测试和 daemon 都不带成员概念——它们是内部逻辑(认领 task、执行),不需要人身份。只有 web 看板/CLI 面向用户时才需要知道"你是谁、你能看哪个 workspace"。可选参数让内部逻辑不强制传 member,只在面向用户时加校验。

### 为什么 MemberStore 用 JSONL,不嵌进 IssueStore?

成员关系是独立维度(issue 有 workspace_id,member 有 workspace 归属)——用独立文件(`members.jsonl`)存。如果嵌进 IssueStore,每条 issue 快照都要带成员列表,冗余且更新困难。

## 三、隔离怎么流转

```
  用户 alice 访问 workspace A
        │  IssueStore.list("ws-a", "alice", member_store)
        │  member_store.is_member("ws-a", "alice")? → True → 放行
        │  返回 ws-a 的 issue
        ▼
  用户 alice 访问 workspace B(alice 非 B 成员)
        │  IssueStore.list("ws-b", "alice", member_store)
        │  member_store.is_member("ws-b", "alice")? → False → PermissionError
        ▼
  非成员被拒
```

## 试一试

```bash
python -m pytest tests/test_workspace_isolation.py tests/test_membership_gate.py -v
python sessions/zh/s11_workspaces.py   # demo:成员访问 + 非成员被拒
```

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(CLAUDE.md) | mica s11 |
|---|---|---|
| workspace 隔离 | "All queries filter by workspace_id" | IssueStore.list(workspace_id) 过滤 |
| 成员资格 | "membership gates access" | MemberStore.is_member → PermissionError |
| 存储 | PostgreSQL(workspace + member 表) | members.jsonl |
| X-Workspace-ID | 请求头选 workspace | member_id + member_store 参数(简化) |
