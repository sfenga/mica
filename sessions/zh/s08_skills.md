# s08 — SkillLoader:从 workspace/skills 读取技能

s08 只建立一个地基:**技能是工作区里的 markdown 文件,SkillLoader 负责把它们读出来**。这一章不让 Issue 选择 skill,也不让 daemon 根据 issue title/body 猜测 skill。真正的绑定关系会在 s14 由 `Agent.skill_names` 引入。

## 1. 为什么先做 SkillLoader

multiCa 里的 skill 不是写死在代码里的函数,而是可管理的能力说明。它通常有三层含义:

```text
skill 文件       SKILL.md,描述一种工作方法
skill 记录       平台知道有哪些 skill
agent 绑定       某个 agent 拥有哪些 skill
```

s08 先实现第一层:从 `workspace/skills/**/SKILL.md` 扫描技能文件,解析 `name`、`description` 和正文。这样后面的章节可以直接复用这个 loader,不用把 skill 内容散落在 daemon 或 runner 里。

## 2. SKILL.md 格式

一个技能目录长这样:

```text
workspace/
  skills/
    code-review/
      SKILL.md
```

`SKILL.md` 使用很小的 frontmatter:

```markdown
---
name: code-review
description: 审查代码变更,关注安全/正确性/可维护性
---

You are reviewing code. Focus on:
1. Security: injection, auth bypass, data leak
2. Correctness: edge cases, error handling
3. Maintainability: naming, DRY, complexity
```

s08 只解析两个字段:

```text
name           skill 的稳定名字,后续 Agent.skill_names 会引用它
description    给人看的说明,用于列表和控制台展示
body           frontmatter 后面的 markdown 正文
```

当前格式只保留 skill 的稳定名字和说明。skill 是否进入执行上下文,由后续 Agent Profile 决定:agent 先声明自己绑定哪些 skill,daemon 执行时再按这些名字加载技能。

## 3. SkillLoader 的职责

代码里的 `SkillLoader` 很克制:

```python
@dataclass
class Skill:
    name: str
    description: str
    body: str = ""
    path: str = ""

class SkillLoader:
    def list(self) -> list[Skill]: ...
    def get(self, name: str) -> Skill | None: ...
```

它做三件事:

```text
扫描目录     sorted glob(workspace/skills/**/SKILL.md)
解析文件     --- frontmatter --- + markdown body
按名读取     get(name),大小写不敏感
```

它不做三件事:

```text
不读取 Issue
不从任务文本推断 skill
不决定 daemon 执行时应该注入哪些 skill
```

这让边界很清楚:SkillLoader 是“技能仓库读取器”,不是“任务路由器”。

## 4. 数据流

s08 的数据流是:

```text
workspace/skills/code-review/SKILL.md
        │
        ▼
SkillLoader._load()
        │  扫描所有 SKILL.md
        ▼
Skill(name="code-review", description="...", body="...", path="...")
        │
        ├─ list()      给 UI / 测试 / 调试展示所有 skill
        └─ get(name)   给后续 Agent.skill_names 解析绑定 skill
```

这章的 demo 会创建示例 `code-review` skill,然后用 `skill_loader.get("code-review")` 证明它被加载。daemon 仍然可以执行 issue,但 skill 是否注入不由 Issue 决定。

## 5. 与 multiCa 的对应关系

| 维度 | mica s08 | multiCa |
|---|---|---|
| skill 文件 | `workspace/skills/**/SKILL.md` | workspace skill / builtin skill 都以 `SKILL.md` 为核心 |
| frontmatter | `name` / `description` | `name` / `description` |
| 读取职责 | `SkillLoader.list()` / `get(name)` | service/handler 加载 workspace skill 与 agent skill bundle |
| 绑定关系 | s08 不处理,s14 引入 `Agent.skill_names` | `agent_skill` 关联表把 skill 绑定到 agent |
| Issue 关系 | Issue 不绑定 skill | Issue/task 不直接决定 skill 集合 |

一句话:**s08 只回答“有哪些 skill,如何按名字取到它们”;不回答“这个 task 应该用哪些 skill”。** 后一个问题属于 Agent Profile,在 s14 解决。

## 试一试

```bash
python -m pytest tests/test_skill_loader.py -v
python sessions/zh/s08_skills.py
```

你应该看到 `SkillLoader` 能加载 `code-review`,并且测试覆盖空目录、无 frontmatter、嵌套目录扫描和大小写不敏感的 `get(name)`。
