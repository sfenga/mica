# s11 — Workspace 隔离与成员闸门

s10 让一个 daemon 能处理多个 agent 的 task。但所有 issue 仍像在同一个空间里:

```text
alice 的 issue
bob 的 issue
team A 的 issue
team B 的 issue
```

如果没有 workspace 隔离,任何人只要能调用 list 就能看到全部 issue。

s11 补上两层:

```text
workspace_id 过滤       只看某个 workspace 的数据
member 权限校验         只有 workspace 成员才能访问
```

## 1. s11 新增了什么

s11 在 s10 基础上新增:

```text
MemberStore       members.jsonl,记录 workspace -> members
WorkspaceScope    聚合 workspace_id / member_id / member_store
IssueStore        create/list 可选成员校验
```

从 s01 开始,`Issue` 就已经有 `workspace_id`。s11 让这个字段真正承担访问隔离的职责。

## 2. MemberStore: workspace 成员表

成员关系存在:

```text
workspace/members.jsonl
```

数据结构是:

```text
workspace_id -> set(member_id)
```

代码:

```python
class MemberStore:
    def add_member(self, ws_id: str, member_id: str) -> None: ...
    def remove_member(self, ws_id: str, member_id: str) -> None: ...
    def is_member(self, ws_id: str, member_id: str) -> bool: ...
    def list_members(self, ws_id: str) -> list[str]: ...
```

持久化仍然是 JSONL 快照追加:

```json
{"workspace_id": "ws-a", "members": ["alice"]}
{"workspace_id": "ws-a", "members": ["alice", "bob"]}
```

重放时同一个 workspace 后写覆盖前写。

## 3. IssueStore.list 加成员校验

s11 的 `IssueStore.list` 签名:

```python
def list(self, workspace_id: str | None = None,
         member_id: str | None = None,
         member_store: "MemberStore | None" = None) -> list[Issue]:
```

如果传了 `member_id` 和 `member_store`,就先检查:

```python
if member_id and member_store:
    if workspace_id is None:
        raise PermissionError("member_id requires workspace_id for membership check")
    if not member_store.is_member(workspace_id, member_id):
        raise PermissionError(
            f"member {member_id} not in workspace {workspace_id}")
```

然后再按 workspace 过滤:

```python
return [
    i for i in self._issues.values()
    if workspace_id is None or i.workspace_id == workspace_id
]
```

所以有两种用法:

```text
内部逻辑: list("ws-a")                         只过滤,不校验成员
用户请求: list("ws-a", "alice", member_store)   先验成员,再过滤
```

## 4. IssueStore.create 也能校验成员

创建 issue 时也可以传成员信息:

```python
def create(self, workspace_id: str, title: str, body: str = "",
           assignee_type: str = "", assignee_id: str = "",
           member_id: str | None = None,
           member_store: "MemberStore | None" = None) -> Issue:
```

如果 `member_id` 不属于 `workspace_id`,抛 `PermissionError`。

这防止非成员往别人的 workspace 里创建 issue。

## 5. WorkspaceScope 是概念入口

```python
class WorkspaceScope:
    def __init__(self, workspace_id: str, member_id: str,
                 member_store: MemberStore) -> None:
        self.workspace_id = workspace_id
        self.member_id = member_id
        self.member_store = member_store

    def check_access(self) -> None:
        if not self.member_store.is_member(self.workspace_id, self.member_id):
            raise PermissionError(...)
```

当前代码里,实际校验主要由 `IssueStore.create/list` 直接调用 `MemberStore` 完成。`WorkspaceScope` 是把“当前请求在哪个 workspace、当前 member 是谁、用哪个 member store 校验”聚合成一个对象,作为后续 CLI/Web 接入的概念铺垫。

## 6. 为什么 member 参数是可选的

为了兼容前面章节:

```text
s01-s10 的测试和 demo 不知道 member_id
daemon 内部认领 task 也不是用户请求
```

所以 s11 没有强制所有调用都带 member。只有面向用户的路径需要传:

```text
workspace_id + member_id + member_store
```

不传 member 时,仍只按 workspace_id 过滤。

## 7. 整体链路

```text
成员配置:
MemberStore.add_member("ws-a", "alice")
MemberStore.add_member("ws-b", "bob")

alice 访问 ws-a:
IssueStore.list("ws-a", "alice", member_store)
   |
   |-- is_member("ws-a", "alice") -> True
   `-- 返回 ws-a issues

alice 访问 ws-b:
IssueStore.list("ws-b", "alice", member_store)
   |
   |-- is_member("ws-b", "alice") -> False
   `-- PermissionError
```

## 8. 试一试

运行测试:

```bash
python -m pytest tests/test_workspace_isolation.py tests/test_membership_gate.py -v
```

测试覆盖:

```text
workspace A/B issue 不串
切换 workspace 看到不同 issue
task 查询按 issue/workspace 关系隔离
MemberStore 持久化
非成员 list 被拒
成员 list 放行
非成员 create 被拒
```

运行 demo:

```bash
python sessions/zh/s11_workspaces.py
```

demo 会演示:

```text
alice 是 ws-a 成员,能看 ws-a
alice 不是 ws-b 成员,看 ws-b 被拒
bob 不是 ws-a 成员,在 ws-a 创建 issue 被拒
```

## 9. 和 multiCa workspace/membership 的区别

`mica` s11 的目标是把“workspace_id 是隔离边界”讲清楚,所以它只做了 `MemberStore` + `IssueStore.create/list` 的成员校验。`multiCa` 的生产实现把 workspace 隔离贯彻到 SQL、handler、daemon claim、附件、评论、skill、agent、project 等所有资源。

### 9.1 membership 存储

`mica` 使用本地 JSONL:

```text
workspace/members.jsonl
workspace_id -> set(member_id)
```

`multiCa` 使用数据库 `member` 表:

```text
member.workspace_id
member.user_id
member.role
```

用户能看到哪些 workspace,不是从全量 workspace 表里过滤,而是通过 `member JOIN workspace` 得到。也就是说,workspace 列表本身就被 membership 限定。

### 9.2 workspace guard 不只在 handler,还在 SQL

`mica` 的 `IssueStore.list(workspace_id, member_id, member_store)` 是应用层判断:

```text
先 is_member(workspace_id, member_id)
再返回该 workspace 的 issue
```

`multiCa` 会在查询层也带 workspace 条件:

```sql
SELECT * FROM issue
WHERE workspace_id = $1
```

很多读写查询都用 `Get...InWorkspace` 或 `WHERE id = ? AND workspace_id = ?` 这种形态。这样即使 handler 调错了 id,SQL 层也不会把别的 workspace 的资源返回出来。

### 9.3 task/daemon claim 也要 workspace 校验

`mica` s11 只校验 issue create/list;daemon 认领 task 仍是本地内存模型。

`multiCa` 的 daemon claim 会多做一层关键防线:

```text
runtime 属于 workspace A
task 构造出的响应也必须 resolve 到 workspace A
否则取消 task,返回错误
```

原因是 daemon 会把 `MULTICA_WORKSPACE_ID` 放进 agent 环境。如果 task 的 workspace 和 runtime workspace 不一致,agent 可能带着错误凭据操作另一个 workspace。生产系统把这种情况当作硬错误,不是“过滤一下就算了”。

### 9.4 issue 隔离还牵涉 squad/agent 间接关系

`mica` 的查询只问:

```text
这个 member 是否属于 workspace?
```

`multiCa` 的 issue list 还支持更复杂的“involves me”语义:

```text
issue 直接派给我拥有的 agent
issue 派给我所在的 squad
issue 派给我拥有的 agent 领导的 squad
issue 派给包含我拥有 agent 的 squad
```

这些都仍然以 `workspace_id = $1` 为外层边界。也就是说,生产系统既要做 tenant isolation,又要做协作视图里的间接可见性。

### 9.5 对照表

| 维度 | mica s11 | multiCa 生产实现 |
|---|---|---|
| 成员存储 | `members.jsonl` | `member` 表,含 role |
| workspace 列表 | 未实现 | `member JOIN workspace` |
| issue 查询 | 应用层 `workspace_id` 过滤 | SQL `WHERE workspace_id = $1` |
| 成员校验 | create/list 可选传入 | handler + SQL 双层 guard |
| daemon claim | 未接入 workspace 隔离 | runtime workspace 与 task workspace 硬校验 |
| 资源范围 | issue 为主 | issue/comment/task/attachment/skill/agent/project 等 |
| 权限模型 | member / non-member | member role、owner、agent visibility、squad 关系 |
| 间接可见性 | 不做 | owned agent / squad membership / squad leader |

一句话:**mica s11 是 workspace 隔离的最小教学模型;multiCa 则把 workspace_id 当成每个资源和每条 daemon claim 的租户边界,并用 SQL guard 做防御纵深。**

## 10. 本章边界

s11 做了:

```text
members.jsonl
MemberStore
workspace_id 过滤
create/list 成员校验
WorkspaceScope 概念对象
```

s11 不做:

```text
HTTP Header X-Workspace-ID 接入
Web API 全面权限改造
task_store 自身独立 workspace 字段
角色/权限等级
邀请/移除成员流程
```

一句话总结:**s11 让 workspace_id 从普通字段变成隔离边界。查询先确认“你是不是这个 workspace 的成员”,再返回这个 workspace 的 issue。**
