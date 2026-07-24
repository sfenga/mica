# s01 — Issue 看板项、状态机、assignee

mica 的第一章只立一件事:**什么是 issue 看板项**。

后面所有章节都会围绕 issue 往下接:

```text
issue -> task -> daemon -> runner -> realtime/web
```

但在执行之前,必须先回答三个问题:

```text
一张卡有哪些状态?
状态能不能随便跳?
这张卡派给人,还是派给 agent?
```

s01 只处理这三个问题。它不执行任务,不落盘,不调用模型,也不启动后台 daemon。

## 1. Issue 是用户看见的卡

`Issue` 是看板上的任务卡。它描述用户关心的目标,而不是某一次执行尝试:

```python
@dataclass
class Issue:
    id: str
    workspace_id: str
    title: str
    body: str = ""
    status: IssueStatus = IssueStatus.TODO
    assignee_type: str = ""
    assignee_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    error: str = ""
```

这里最重要的字段是:

```text
status          这张卡处在生命周期的哪一步
assignee_type   指派对象类型: member / agent / 空
assignee_id     指派对象的 id
workspace_id    后续多 workspace 隔离的雏形
```

生产 multiCa 的初始迁移里,`issue` 表同样有 7 态 `status`,以及 `assignee_type` + `assignee_id`。s01 把这部分抽成单文件 Python 版本。

## 2. 七个状态表达一条完整流水线

状态不是只有 `todo` 和 `done`。mica 使用七个状态:

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

它们分工如下:

```text
backlog       还没决定要做
todo          已决定要做,但还没开始
in_progress   正在执行
in_review     执行产物等待人评审
done          完成
blocked       卡住,需要人处理
cancelled     放弃
```

为什么需要 `in_review`?因为 agent 跑完不等于任务完成。后面 s02 会让 task `completed` 后把 issue 推到 `in_review`,表示“agent 已提交,等人看”。人通过评审后才进 `done`。

## 3. 状态不能随便跳

合法流转集中在 `VALID_TRANSITIONS`:

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

对应图:

```text
backlog -> todo -> in_progress -> in_review -> done
                         |              |
                         |              `-> in_progress 返工
                         `-> blocked -> todo / cancelled
```

`done` 和 `cancelled` 是终态,转不出去。`todo -> done` 这种跳跃会被拒绝。

`IssueStore.transition()` 只做一件事:查表放行或抛错。

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

这样状态规则不散落在一堆 `start()` / `complete()` / `review()` 方法里。后续章节无论是 daemon 推动状态,还是 Web API 推动状态,都复用同一张表。

## 4. Assignee 是多态的

一张 issue 可以派给人,也可以派给 agent。s01 用两个字段表达:

```text
assignee_type = "member" | "agent" | ""
assignee_id   = 具体 member/agent 的 id
```

例子:

```text
member:alice
agent:agent-1
-
```

为什么不用 `member_id` 和 `agent_id` 两个字段?因为两个可空字段会出现“两个都填”的混乱状态。`assignee_type + assignee_id` 明确告诉系统:当前指派对象是哪一种。

后面 s02 的 daemon 会用它判断:

```text
只有 assignee_type == "agent" 的任务才会被 agent daemon 认领
```

派给 member 的 issue 会留在看板上,不进入 agent 执行队列。

## 5. 本章的内存 Store

s01 的 `IssueStore` 是纯内存版:

```text
create       新建 issue
list         列出全部或某 workspace 的 issue
get          按 id 获取
assign       改 assignee
transition   走状态机
```

它使用进程内 `dict`,所以程序退出后数据就没了。这个限制会在 s04 通过 JSONL 解决。

## 6. 试一试

运行 demo:

```bash
python sessions/zh/s01_issue_board.py
```

无参 demo 会创建两张卡:

```text
修登录 bug   agent:agent-1   todo -> in_progress -> in_review -> done
写文档       member:alice    todo
```

运行测试:

```bash
python -m pytest tests/test_issue_state.py -v
```

测试覆盖:

```text
默认创建状态
合法状态转移
非法状态转移
done/cancelled 终态
in_review 返工
blocked 解除或取消
多态 assignee
workspace_id 过滤
```

## 7. 本章边界

s01 做了:

```text
Issue 数据形状
7 态状态机
多态 assignee
终端看板渲染
```

s01 不做:

```text
Task 执行队列        下一章 s02
daemon 认领          下一章 s02
真实 CLI runner      s03
持久化               s04
评论续聊             s05
```

一句话总结:**s01 把“看板卡片本身”定义清楚。issue 是用户视角的生命周期,不是 agent 的一次执行记录。**
