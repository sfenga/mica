# s14 — Agent Profile:把 agent 变成可配置执行主体

s14 解决一个核心问题:**同一个系统里有多个 agent 时,它们为什么不是同一个 CLI 的几个名字?**

在前面的章节里,task 已经可以派给不同 `assignee_id`,daemon 也能认领 task 并调用 runner。但如果所有 agent 都用同一套提示词、同一组技能、同一种行为边界,那么它们只是“任务收件人”,还不是有明确职责的执行主体。

s14 引入 **Agent Profile**。它让每个 agent 拥有自己的身份提示词和默认技能:

```text
Task.assignee_id
  -> AgentStore.get(agent_id)
  -> Agent.system_prompt  -> runner(append_system_prompt=...)
  -> Agent.skill_names    -> SkillLoader.get(name)
  -> Issue title/body     -> SkillLoader.select(issue)
  -> merge_skills(...)    -> runner(skill_body=...)
```

这一章的重点不是前端,也不是完整权限系统,而是补上 agent 配置层:daemon 在执行 task 时,能够根据 assignee 找到正确的 agent profile,再把这个 profile 注入到 runner。

## 1. 本章新增什么

```text
Agent.system_prompt     agent 的身份、职责和工作边界
Agent.skill_names       agent 默认绑定的 skill 名称
AgentStore              保存和读取 agent profile
SkillLoader.get(name)   按 skill name 找到绑定 skill
merge_skills            合并 agent 绑定 skill 和 issue 命中 skill
Daemon.execute          执行前按 assignee_id 注入 agent profile
RecordingRunner         用于观察实际传给 runner 的 prompt/skill 参数
```

可以把 s14 理解成这条链路:

```text
task 派给谁
  -> 这个 agent 是什么角色
  -> 这个 agent 默认会哪些技能
  -> 当前 issue 还临时命中了哪些技能
  -> runner 看到合成后的执行上下文
```

## 2. Agent:配置主体

s14 扩展了 `Agent`:

```python
@dataclass
class Agent:
    id: str
    name: str
    cli: str = "claude"
    runtime_id: str = "local"
    system_prompt: str = ""
    skill_names: list[str] = field(default_factory=list)
```

字段可以分成三组:

```text
身份字段      id / name
执行入口      cli / runtime_id
行为配置      system_prompt / skill_names
```

`system_prompt` 描述这个 agent 是谁、负责什么、不该做什么。执行时它会传给 runner 的 `append_system_prompt`,最终进入 Claude CLI 的 `--append-system-prompt`。

`skill_names` 是这个 agent 默认绑定的技能名。它不是 skill 正文,也不是路径,而是一组稳定引用。daemon 执行时会用 `SkillLoader.get(name)` 把这些名字解析成真正的 `Skill`。

一个具体例子:

```python
agent = Agent(
    id="agent-fe",
    name="Frontend Agent",
    cli="claude",
    runtime_id="rt-1",
    system_prompt=(
        "You are a frontend implementation agent. "
        "Focus on accessibility and UI consistency."
    ),
    skill_names=["react"],
)
```

这个 agent 的含义是:

```text
它用 claude 这个 CLI 执行
它属于 rt-1 这个 runtime
它默认以 frontend 身份工作
它默认带 react skill
```

## 3. AgentStore:保存 profile

`AgentStore` 是 s14 新增的存储层。它负责创建、读取、更新 agent,并维护 agent 和 skill name 的绑定关系。

```python
class AgentStore:
    def create(self, id, name, cli="claude", runtime_id="local",
               system_prompt="", skill_names=None) -> Agent: ...
    def get(self, agent_id) -> Agent | None: ...
    def list(self) -> list[Agent]: ...
    def update(self, agent_id, **fields) -> Agent: ...
    def bind_skill(self, agent_id, skill_name) -> Agent: ...
    def unbind_skill(self, agent_id, skill_name) -> Agent: ...
```

如果传入 `data_dir`,AgentStore 会使用:

```text
workspace/agents.jsonl
```

持久化方式仍然是教学项目一直使用的 snapshot-append:

```text
create/update/bind/unbind
  -> 追加一行 agent 快照
启动时重放 agents.jsonl
  -> 同 id 后写覆盖先写
```

这和数据库里的 update 不一样,但非常适合学习:你可以直接打开 JSONL 文件,看到 agent profile 是如何随操作变化的。

`bind_skill()` 是幂等的。同一个 skill 已经绑定时,再次绑定不会重复追加。`unbind_skill()` 对不存在的 skill 静默处理,这样 UI 或 demo 可以反复点击而不破坏状态。

## 4. SkillLoader:两种读取方式

s14 沿用 `SKILL.md` 文件作为技能来源。一个 skill 文件长这样:

```markdown
---
name: code-review
description: 审查代码变更,关注安全/正确性/可维护性
keywords: review, 审查, PR, code review
---

You are reviewing code. Focus on security, correctness, and maintainability.
```

SkillLoader 有两个重要方法:

```python
def select(self, issue) -> Skill | None:
    # 用 keywords 匹配 issue.title + issue.body

def get(self, name: str) -> Skill | None:
    # 按 name 查找 skill,供 Agent.skill_names 使用
```

这两个方法服务不同场景:

```text
SkillLoader.get(name)
  解决 agent 默认会什么
  来源是 Agent.skill_names
  例如 agent-fe 永远绑定 react

SkillLoader.select(issue)
  解决当前任务临时需要什么
  来源是 issue title/body
  例如标题含 review 时命中 code-review
```

所以 s14 不是“只靠 issue 自动匹配 skill”,也不是“只靠 agent 固定绑定 skill”。它同时保留两层:

```text
agent 绑定 skill   稳定能力
issue 命中 skill   任务级补充
```

## 5. merge_skills:合并并去重

daemon 可能同时拿到两类 skill:

```text
bound skills    agent.skill_names 解析出来的技能
matched skill   issue 内容命中的技能
```

例如:

```text
agent-fe 绑定 react
issue 标题是 "review the auth module"
issue 命中 code-review
```

执行时应该同时注入:

```text
react
code-review
```

如果 agent 已经绑定 `code-review`,而 issue 也命中 `code-review`,就只能注入一次。`merge_skills()` 负责这个合并:

```python
def merge_skills(bound: list[Skill], matched: Skill | None) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for skill in list(bound) + ([matched] if matched else []):
        key = skill.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"## Skill: {skill.name}\n{skill.body}")
    return "\n\n".join(parts)
```

合并后的 `skill_body` 是一段 markdown:

```markdown
## Skill: react
...

## Skill: code-review
...
```

如果没有任何 skill,返回空字符串,调用方再传 `None` 给 runner。

## 6. Daemon.execute:profile 的注入点

s14 的关键变化在 `Daemon.execute()`。daemon 认领 task 后,会按 `task.assignee_id` 查 agent:

```python
agent = self.agent_store.get(task.assignee_id) if self.agent_store else None
system_prompt = agent.system_prompt if agent else ""
```

然后解析 agent 绑定 skill:

```python
bound: list[Skill] = []
if agent and self.skill_loader:
    bound = [self.skill_loader.get(n) for n in agent.skill_names]
    bound = [s for s in bound if s is not None]
```

再解析 issue 命中的 skill:

```python
matched = self.skill_loader.select(issue) if self.skill_loader else None
skill_body = merge_skills(bound, matched)
```

最后传给 runner:

```python
result = self.runner.run(
    task,
    issue,
    [],
    on_event,
    resume_session_id=prior_sid or None,
    append_system_prompt=system_prompt or None,
    skill_body=skill_body or None,
    cwd=workdir or None,
)
```

这里要注意两个注入位置:

```text
system_prompt
  -> append_system_prompt
  -> CLI system prompt
  -> 表示 agent 的长期身份

skill_body
  -> 拼入 user prompt
  -> 表示这次 task 的技能上下文
```

为什么 skill 不放进 system prompt?因为 system prompt 更像 session 级身份,而 skill 是每次执行按 agent 和 issue 重新计算出来的任务上下文。把 skill 放在 user prompt 里,更容易保证每个 task 都拿到当前正确的技能组合。

## 7. 和 workdir、resume 的关系

s14 没有替换前面章节的能力,而是叠加在执行链路上。

daemon 执行一个 task 时,现在会同时处理三类上下文:

```text
resume_session_id   来自同 issue + 同 agent 的上一条 completed task
workdir             来自 issue.project_id -> Project.local_path
agent profile       来自 task.assignee_id -> AgentStore
```

合起来就是:

```text
同一个 agent 可以接上自己的历史 session
同一个 issue 可以进入正确项目目录
不同 agent 可以拥有不同身份和默认技能
```

这也是为什么 `task.assignee_id` 很关键。它不只是“谁来执行”的标签,而是 daemon 查找 session、agent profile、skill 绑定的共同入口之一。

## 8. Demo 怎么读

运行:

```bash
python sessions/zh/s14_agents.py
```

demo 会创建两个 agent:

```text
agent-fe
  system_prompt: frontend 身份
  skill_names: react

agent-be
  system_prompt: backend 身份
  skill_names: code-review
```

然后创建两个 issue:

```text
issue A: "review the auth module"
  assignee: agent-fe
  agent 绑定 react
  issue 命中 code-review

issue B: "refactor utils"
  assignee: agent-be
  agent 绑定 code-review
  issue 不命中 react
```

demo 使用 `RecordingRunner`,不会真的启动 CLI。它只记录 daemon 传给 runner 的参数,所以你能直接看到:

```text
agent-fe 的 append_system_prompt 含 frontend
agent-fe 的 skill_body 含 react + code-review
agent-be 的 append_system_prompt 含 backend
agent-be 的 skill_body 含 code-review,不含 react
```

这个 demo 的学习重点是“隔离”:

```text
react 只属于 agent-fe
code-review 可以属于 agent-be,也可以被 issue 临时命中
两个 agent 的默认 skill 不会互相泄漏
```

## 9. 测试覆盖

运行:

```bash
python -m pytest tests/test_agent_profile.py -v
```

测试覆盖了这些行为:

```text
AgentStore create/get/list/update
AgentStore bind_skill / unbind_skill 幂等行为
agents.jsonl 持久化重放
SkillLoader.get(name) 大小写不敏感
merge_skills 按 skill name 去重
Daemon 注入 agent.system_prompt
Daemon 注入 agent 绑定 skill
Agent 绑定 skill 和 issue 命中 skill 合并去重
不同 agent 的 skill 不互相泄漏
agent_store=None 时保持兼容
```

如果你要判断 s14 是否真的实现了 Agent Profile,最关键的测试不是 CRUD,而是这三个:

```text
daemon 会把 agent.system_prompt 传给 append_system_prompt
daemon 会把 agent.skill_names 解析成 skill_body
agent A 的 skill 不会出现在 agent B 的执行上下文里
```

## 10. 和 multiCa 的对应关系

s14 是教学版 Agent Profile,它用最少代码表达 multiCa 的几个核心思想:

| 维度 | mica s14 | multiCa |
|---|---|---|
| agent 存储 | `agents.jsonl` | 数据库 agent 记录 |
| agent 身份 | `Agent.system_prompt` | agent instructions / identity 配置 |
| skill 绑定 | `Agent.skill_names` | agent 与 skill 的关联关系 |
| skill 注入 | skill body 拼入 user prompt | skill 文件物化到 CLI 可发现目录 |
| runtime 绑定 | `cli` / `runtime_id` 字符串 | runtime 配置、模型、env、args、MCP 等 |
| 任务补充 skill | `keywords` 命中 issue | 生产系统通常更依赖显式绑定和文件系统发现 |

mica 选择“进程内注入”是为了教学清晰:

```text
agent profile 在 AgentStore
skill 内容在 SkillLoader
daemon 明确合成 prompt 参数
RecordingRunner 可以直接断言注入结果
```

multiCa 的生产路径更复杂,因为它需要兼容真实 CLI 的发现规则、工作目录、运行时配置和权限边界。因此它会把 agent identity、skill 文件、runtime 配置等物化到执行环境里,让 CLI 自己在项目目录中读取。

理解 s14 后,再看 multiCa 的 agent/skill/runtime 设计时,重点就不是“字段名字是否一样”,而是这三个问题:

```text
agent 的身份保存在哪里?
agent 绑定了哪些稳定技能?
daemon 在执行前如何把这些配置交给 runner 或 CLI?
```

## 11. 本章边界

s14 做了:

```text
Agent.system_prompt
Agent.skill_names
AgentStore 持久化
SkillLoader.get(name)
agent 绑定 skill + issue 命中 skill 合并
daemon 按 assignee_id 注入 profile
RecordingRunner 可观察注入结果
```

s14 不做:

```text
Agent Profile 的 HTTP API
Mission Control 的 Agents 面板
agent 级 model/env/custom_args/MCP 配置
agent 权限和 workspace 可见性
skill 文件物化到 CLI 原生 skill 目录
CLAUDE.md / AGENTS.md 生成
```

这些边界很重要。s14 的目标是让你学会 agent profile 的最小闭环,不是一次性做完整生产平台。下一章可以在这个基础上把 AgentStore 接进控制台,让用户通过页面创建 agent、编辑 system prompt、绑定 skill,并在创建 issue 时选择真实 agent。

## 12. 一句话总结

s14 把 agent 从“任务的 assignee id”提升为“有身份、有默认技能、可被 daemon 注入执行上下文的配置主体”。

掌握这一章后,你应该能清楚回答:

```text
agent 和 runtime 是什么关系?
agent.skill_names 为什么不是 skill body?
agent 绑定 skill 和 issue 命中 skill 如何合并?
system_prompt 为什么走 append_system_prompt?
daemon 为什么必须按 task.assignee_id 查 AgentStore?
```
