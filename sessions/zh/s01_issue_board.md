# s01 — Issue 看板项 + 7 态状态机 + 多态 assignee + 看板

mica 的第一章只立住一件事:**一张 issue(看板项)有哪些状态、能怎么流转、能派给谁**。这是整个系统的地基——issue 是用户看到的任务卡,它的状态机定义了任务从计划到评审到完成的完整生命周期。本章不写执行、不写 agent 怎么跑,只把"看板项 + 状态"定义清楚。

## 一、核心设计思想

第一个思想:**issue 是看板项,状态反映整体生命周期**。一张 issue 不只有"做没做完",而是一条完整流水线:`backlog`(计划外)→ `todo`(待开始)→ `in_progress`(进行中)→ `in_review`(待人评审)→ `done`(完成);外加两个侧支:`blocked`(卡住)、`cancelled`(取消)。这比"todo/done"两态更贴近真实看板——`in_review` 是评审环节,`blocked` 是卡点,都是日常发生的。

```python
class IssueStatus(str, Enum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
```

第二个思想:**用状态转移表约束合法性,而不是一堆 if**。issue 的状态不能随便跳——`todo` 不能直接到 `done`(必须经过 `in_progress`),`done` 不能复活。所有合法路径集中在一处:

```python
VALID_TRANSITIONS = {
    IssueStatus.BACKLOG: {IssueStatus.TODO},
    IssueStatus.TODO: {IssueStatus.IN_PROGRESS},
    IssueStatus.IN_PROGRESS: {IssueStatus.IN_REVIEW, IssueStatus.DONE, IssueStatus.BLOCKED},
    IssueStatus.IN_REVIEW: {IssueStatus.DONE, IssueStatus.IN_PROGRESS, IssueStatus.BLOCKED},
    IssueStatus.BLOCKED: {IssueStatus.TODO, IssueStatus.CANCELLED},
    IssueStatus.DONE: set(),
    IssueStatus.CANCELLED: set(),
}
```

`DONE` 和 `CANCELLED` 映射到空集合——**终态**,转不出去。上层拿到的 issue 永远处于合法状态,不必每处判"万一它 done 了怎么办"。

第三个思想:**多态 assignee**。一张 issue 可派给 member(人)或 agent(机器队友),用类型标签 + id 表示:

```python
@dataclass
class Issue:
    id: str
    workspace_id: str
    title: str
    body: str = ""
    status: IssueStatus = IssueStatus.TODO
    assignee_type: str = ""       # "" | "member" | "agent"
    assignee_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    error: str = ""
```

`assignee_type ∈ {"", "member", "agent"}` + `assignee_id`——agent 和人一样是认领者。要判断"这张卡派给了哪个 agent",就是 `assignee_type == "agent" and assignee_id == that_agent`。

## 二、为什么这么设计

### 为什么 7 个状态,不只 todo/done?

`todo/done` 两态把"进行中"和"评审中"都塌缩了,看不出任务到底在哪个环节。真实看板里,`in_review`(等人评审)和 `in_progress`(正在干)是两个不同阶段——评审中的卡不该当成"还在干",也不该当成"完了"。`blocked` 单独一态,让卡点显式可见(卡住的任务能一眼挑出来解除或取消)。`backlog` 是"还没决定要做"的暂存。多态化状态让看板信息量更大、操作更精确。

### 为什么用转移表,而不是 `start()`/`complete()`/`review()` 一堆方法?

一堆方法各自校验容易漏,且新增状态要改多处。一张"当前状态→可转状态集合"的表,把所有合法路径集中,`transition()` 一次集合查 + 一次放行:

```python
def transition(self, issue_id: str, new_status: IssueStatus) -> Issue:
    issue = self._require(issue_id)
    if new_status not in VALID_TRANSITIONS[issue.status]:
        raise IllegalTransition(
            f"非法转移:{issue.status.value} -> {new_status.value}"
        )
    issue.status = new_status
    issue.updated_at = now_iso()
    return issue
```

非法转移统一抛 `IllegalTransition`,易测、易扩展(改表不改逻辑)。

### 为什么 `in_review` 能回到 `in_progress`?

评审不是单向终点——评审者可能"要求返工"(代码没改好、缺测试)。`in_review→in_progress` 允许返工:卡从评审打回进行中,重做后再提交评审。这是真实代码评审流程的常态。`in_review→done` 是评审通过,`in_review→blocked` 是评审中发现卡点(比如缺依赖)。

### 为什么多态 assignee,而不是 `agent_id` + `member_id` 两空字段?

两个可空字段会留出"两个都空""两个都填"的非法状态,校验散落。单字段 `assignee_type` + `assignee_id` 用类型标签明确"这是哪种认领者",合法状态只有三种,约束集中。这对应 multiCa 生产库的约定(多态指派 member 或 agent)。

## 三、状态怎么流转

```
  backlog ──► todo ──► in_progress ──► in_review ──► done
                            │                │
                            │                └──► in_progress(返工)
                            └──► blocked ──► todo(解除) / cancelled(放弃)
```

- **backlog → todo**:决定要做了,排入计划。
- **todo → in_progress**:开始干。
- **in_progress → in_review**:做完,提交评审。
- **in_progress → done**:跳过评审直接完成(小改动可不评审)。
- **in_progress → blocked**:卡住(缺依赖、等外部)。
- **in_review → done**:评审通过。
- **in_review → in_progress**:要求返工。
- **in_review → blocked**:评审中发现卡点。
- **blocked → todo**:解除阻塞,重新入队。
- **blocked → cancelled**:放弃。
- **done / cancelled**:终态,转不出去。

合法转移只有上面这些,其余一律被 `VALID_TRANSITIONS` 拒。`transition()` 每次刷新 `updated_at`。

## 试一试

```bash
python sessions/zh/s01_issue_board.py            # 跑演示看板(一条 done + 一条 todo)
python -m pytest tests/test_issue_state.py -v    # 7 态状态机单测
```

演示看板(无参 demo 跑一条 issue 走 todo→in_progress→in_review→done):

```
ID            STATUS       ASSIGNEE                TITLE
...           done         agent:agent-1           修登录 bug
...           todo         member:alice            写文档
```

看板的 `ASSIGNEE` 列把 `assignee_type:assignee_id` 直接打出来:

```python
def render_board(issues: list[Issue]) -> str:
    if not issues:
        return "(空看板)"
    lines = ["ID            STATUS       ASSIGNEE                TITLE"]
    for i in issues:
        assignee = f"{i.assignee_type}:{i.assignee_id}" if i.assignee_type else "-"
        lines.append(f"{i.id[:12]:<13} {i.status.value:<12} {assignee:<23} {i.title}")
    return "\n".join(lines)
```

## 四、与完整 multiCa 的区别

s01 是生产 multiCa 的「看板项 + 状态机」骨架,纯内存、无执行:

| 维度 | 完整 multiCa(生产) | mica s01 |
|---|---|---|
| 语言/栈 | Go(Chi + sqlc)+ Next.js | Python 单文件 |
| 存储 | PostgreSQL,sqlc 生成 | 进程内 `dict`,无持久化 |
| 状态集 | backlog/todo/in_progress/in_review/done/blocked/cancelled | **完全一致**(7 态) |
| 状态机 | DB 约束 + 应用层 | `VALID_TRANSITIONS` 表 + `transition()` |
| 多态 assignee | `assignee_type` + `assignee_id` 指向 member/agent | **完全一致** |
| 看板 | Next.js 全功能 | 终端 `render_board` 文本表格 |
| workspace 过滤 | 每条查询带 `workspace_id` + 成员资格 | `list(workspace_id)` 过滤(雏形) |

一句话:**s01 保留了 multiCa 的「看板项语义」(7 态状态机 + 多态 assignee + workspace),抽掉了它的运行时(执行、持久化、前端)**。看板项的形状和生产一致;谁来推动这些状态流转,不属于本章范围。
