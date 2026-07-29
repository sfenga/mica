# s14 — Agent Profile:agent 绑定 system_prompt 和 skill

s14 解决一个核心问题:**同一个系统里有多个 agent 时,每个 agent 的身份和技能从哪里来?**

前面章节已经能把 task 派给不同 `assignee_id`,daemon 也能调用 runner。但如果所有 agent 都共享同一套提示词和技能,它们只是不同名字的 CLI 入口。s14 引入 Agent Profile,让 agent 成为可配置的执行主体。

## 1. 本章心智模型

本章采用这条职责链:

```text
SkillLoader
  负责从 workspace/skills 读取 skill

Agent.skill_names
  负责声明 agent 绑定哪些 skill

Daemon.execute
  按 task.assignee_id 查 Agent
  再按 Agent.skill_names 加载 skills
  注入 runner

Issue
  不直接绑定 skill
```

也就是:

```text
Task.assignee_id
  -> AgentStore.get(agent_id)
  -> Agent.system_prompt
  -> Agent.skill_names
  -> SkillLoader.get(name)
  -> merge_skills(bound_skills)
  -> runner.run(append_system_prompt=..., skill_body=...)
```

Issue 只提供“要做什么”,Agent Profile 决定“由谁以什么身份、带哪些技能去做”。

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

字段分成三组:

```text
身份字段      id / name
执行入口      cli / runtime_id
行为配置      system_prompt / skill_names
```

`system_prompt` 描述 agent 的身份、边界和工作方式。执行时它传给 runner 的 `append_system_prompt`,最终进入 CLI 的系统提示词扩展。

`skill_names` 是 agent 绑定的技能名列表。它保存的是稳定名字,不是 skill 正文,也不是路径。执行前 daemon 会用 `SkillLoader.get(name)` 把名字解析成当前 `SKILL.md` 的正文。

例如:

```python
agent = Agent(
    id="agent-fe",
    name="Frontend Agent",
    cli="claude",
    runtime_id="rt-1",
    system_prompt="You are a frontend implementation agent.",
    skill_names=["react"],
)
```

这表示 `agent-fe` 默认以 frontend 身份工作,并拥有 `react` 这个 skill。即使某个 issue 标题里写了 “review”,它也不会自动获得 `code-review` skill;除非 `code-review` 出现在它的 `skill_names` 里。

## 3. AgentStore:保存 profile

`AgentStore` 负责创建、读取、更新 agent,并维护 skill name 绑定:

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

如果传入 `data_dir`,它会把 profile 写到:

```text
workspace/agents.jsonl
```

持久化方式是教学项目一直使用的 snapshot-append:

```text
create/update/bind/unbind
  -> 追加一行 agent 快照
启动时重放 agents.jsonl
  -> 同 id 后写覆盖先写
```

`bind_skill()` 是幂等的。同一个 skill 已经绑定时,再次绑定不会重复追加。`unbind_skill()` 对不存在的 skill 静默处理,方便 UI 反复操作。

## 4. SkillLoader:get(name) 按绑定名读取

s14 沿用 s08 的 `SKILL.md` 文件格式:

```markdown
---
name: code-review
description: 审查代码变更,关注安全/正确性/可维护性
---

You are reviewing code. Focus on security, correctness, and maintainability.
```

`SkillLoader` 的关键方法是:

```python
def get(self, name: str) -> Skill | None:
    target = name.strip().lower()
    for skill in self._skills:
        if skill.name.strip().lower() == target:
            return skill
    return None
```

这个方法只接受 skill name。调用者必须明确说“我要哪个 skill”。在 s14 里,调用者就是 daemon,来源就是 `agent.skill_names`。

这样设计的好处是:

```text
可解释       agent 有哪些能力,看 agent.skill_names 就知道
可隔离       agent A 绑定的 skill 不会泄漏给 agent B
可审计       执行记录能解释 skill_body 来自哪个 agent profile
可维护       改 SKILL.md 内容不需要改 daemon,改绑定不需要改 Issue
```

## 5. merge_skills:渲染并去重

`agent.skill_names` 可能包含重复项,或者大小写不同但指向同一个 skill。`merge_skills()` 把 loader 解析出的 `Skill` 列表渲染成 runner 能注入的 markdown:

```python
def merge_skills(skills: list[Skill]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for skill in list(skills):
        key = skill.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"## Skill: {skill.name}\n{skill.body}")
    return "\n\n".join(parts)
```

输出示例:

```markdown
## Skill: react
Use functional components, hooks, and proper key props.

## Skill: code-review
You are reviewing code. Focus on security, correctness, and maintainability.
```

它只渲染 agent 绑定的 skill,因此输出能直接追溯到当前 agent profile。

## 6. Daemon.execute 的注入点

执行 task 时,daemon 做的是按 assignee 找 agent:

```python
agent = self.agent_store.get(task.assignee_id) if self.agent_store else None
system_prompt = agent.system_prompt if agent else ""

bound: list[Skill] = []
if agent and self.skill_loader:
    bound = [self.skill_loader.get(n) for n in agent.skill_names]
    bound = [s for s in bound if s is not None]

skill_body = merge_skills(bound)
result = self.runner.run(
    task, issue, [], on_event,
    resume_session_id=prior_sid or None,
    append_system_prompt=system_prompt or None,
    skill_body=skill_body or None,
    cwd=workdir or None,
)
```

这段代码里有两个重要边界:

```text
agent 找不到      不注入 system_prompt,也不注入 skill_body
skill name 找不到 忽略这个绑定,不会让执行失败
```

教学项目选择“忽略缺失 skill”是为了让 profile 可以先写出来,skill 文件稍后补上。生产系统可以在 UI/API 层增加校验,但 daemon 的执行链路仍然应该保持清晰:按 agent profile 加载,不要从 issue 文本猜测。

## 7. 与 multiCa 的对应关系

| 维度 | mica s14 | multiCa |
|---|---|---|
| agent profile | `Agent` dataclass | agent 表与 handler/service |
| system prompt | `Agent.system_prompt` -> `append_system_prompt` | agent instructions / identity 写入 runtime 上下文 |
| skill 绑定 | `Agent.skill_names` | `agent_skill` 关联表 |
| skill 加载 | `SkillLoader.get(name)` | `LoadAgentSkills(ctx, agentID)` / skill bundle |
| Issue 关系 | Issue 不绑定 skill | task 的 agent 决定可见 skill |
| 注入方式 | `runner.run(skill_body=...)` | execenv/runtime 配置和 skill 文件上下文 |

s14 的目标不是复刻 multiCa 的数据库、权限和 runtime 文件物化,而是学习它最关键的结构关系:**skill 属于 agent profile 的能力集合,不是 issue 的临时标签。**

## 试一试

```bash
python -m pytest tests/test_agent_profile.py -v
python sessions/zh/s14_agents.py
```

demo 会创建不同 agent,为它们绑定不同 skill,再执行 task。你应该重点观察:

```text
agent-fe 只拿到自己的 skill_body
agent-be 只拿到自己的 skill_body
issue title/body 不会让未绑定的 skill 被注入
未配置 profile 的 assignee 不会得到 system_prompt / skill_body
```
