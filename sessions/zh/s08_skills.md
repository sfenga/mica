# s08 — markdown 技能注入(SkillLoader + `keywords` 关键词召回 + user prompt 注入)

s07 让看板能操作了,但 agent 执行 task 时不知道该用什么"姿势"——修 bug 和做 code review 的方法论不同。s08 加上技能:磁盘上放 markdown 技能文件(`SKILL.md`),`SkillLoader` 扫描目录、用 frontmatter 的 `keywords` 关键词匹配 issue,命中的技能正文拼入 **user prompt**。换技能文件不改代码即生效。

## 一、核心设计思想

第一个思想:**技能是磁盘上的 markdown 文件**。每个技能是一个 `SKILL.md`,带 frontmatter(name/description/keywords)+ markdown 正文:

```markdown
---
name: code-review
description: 审查代码变更,关注安全/正确性/可维护性
keywords: review, 审查, PR, code review
---

You are reviewing code. Focus on:
1. Security: injection, auth bypass, data leak
2. Correctness: edge cases, error handling
3. Maintainability: naming, DRY, complexity
```

技能是文件,不是代码——换个文件就换了 agent 的能力,不用改代码。技能可以由团队共享(git 管理)。

第二个思想:**`keywords` 关键词召回**。SkillLoader 扫完所有技能后,`select(issue)` 用每个技能的 `keywords`(逗号分隔)做**子串匹配** `issue.title + "\n" + issue.body`,取**第一个命中**的:

```python
class SkillLoader:
    def select(self, issue) -> Skill | None:
        text = issue.title + "\n" + (issue.body or "")
        for skill in self._skills:
            if skill.keywords:
                for kw in skill.keywords.split(","):
                    if kw.strip().lower() in text:
                        return skill
        return None
```

`keywords: "review, 审查, PR, code review"` 会命中 title 含 "review"/"审查"/"PR"/"code review" 的 issue。无 `keywords` 字段的技能**永不被自动选中**。第一个命中赢(按 sorted glob 顺序)。

这是在 multiCa 的 `name`/`description` frontmatter(`skill/frontmatter.go:17,27`)基础上**加了一个简单字段**做召回,不是另起炉灶的正则系统。

第三个思想:**技能正文拼入 user prompt,不进 `--append-system-prompt`**。选中技能后,daemon 把技能正文传给 `ClaudeCliRunner.run(skill_body=skill.body)`,run 把它拼进 user prompt:

```python
def run(self, task, issue, context, on_event,
        resume_session_id=None, append_system_prompt=None,
        skill_body=None) -> RunResult:
    prompt = issue.title + (f"\n\n{issue.body}" if issue.body else "")
    if skill_body:
        prompt += f"\n\n[Skill]\n{skill_body}"
    if resume_session_id and task.trigger_comment_id:
        prompt += f"\n\n[Follow-up] {task.trigger_comment_id}"
    ...
```

技能正文和 issue 内容在同一个 user prompt 里——技能跟 issue 内容绑定,issue 变了就匹配不同技能。不用 `--append-system-prompt`,因为系统提示词是 session 级的,会跨轮残留:如果首次跑匹配了 review 技能,resume 后新评论是修 bug 的,系统提示词里还卡着 review 技能——语义错乱。

第四个思想:**Daemon 在 execute 里选技能**。daemon 认领 task 后,先查技能再执行:

```python
# Daemon.execute
skill = None
if self.skill_loader:
    skill = self.skill_loader.select(issue)
result = self.runner.run(task, issue, [], on_event,
                         resume_session_id=prior_sid or None,
                         skill_body=skill.body if skill else None)
```

## 二、为什么这么设计

### 为什么用 `keywords` 关键词,不用正则?

正则太复杂、容易写错,和 multiCa 的 frontmatter(`name`/`description`,无 match)完全不同。关键词子串匹配简单直观:`keywords: "review, 审查, PR"` 就是三个关键词,逗号分隔,任一出现在 issue title+body 即命中。multiCa 的 frontmatter 只有 `name`/`description`(`skill/frontmatter.go:17,27`),没有匹配字段——技能是显式关联到 agent 的。mica 加 `keywords` 是在 multiCa 的基础上加了一个简单字段做召回,不是另起炉灶。

### 为什么技能进 user prompt,不进 `--append-system-prompt`?

`--append-system-prompt` 注入的是**系统提示词**——session 级的,一旦注入,这个 session 的**每一轮**都带着它。但技能是按 issue 内容匹配的,是**特定 task/issue 级别**的。如果首次跑匹配了 review 技能 → 注入 `--append-system-prompt` → resume 后新评论是修 bug → 系统提示词里还卡着 review 技能,不匹配当前 issue 内容。

技能正文拼入 user prompt:每次执行时 prompt 重新构建(issue title+body + 技能 + 评论),issue 变了→匹配不同技能→新技能进新 prompt。不残留。

### 为什么用简单 frontmatter 解析,不引 YAML 库?

技能 frontmatter 只有三个字段(name/description/keywords),用 `key: value` 逐行解析就够。multiCa 的 `ParseSkillFrontmatter` 也只取 name/description 两个字段(`frontmatter.go:27`),不解析复杂 YAML 结构。

### 为什么第一个命中赢,不给所有命中的技能?

多技能同时拼入会让 user prompt 过长,且技能间可能冲突。第一个命中赢(sorted glob 顺序)让技能选择确定、可预测。

## 三、技能怎么流转

```
  daemon 认领 task → 取 issue(issue_store.get(task.issue_id))
        │
        ▼
  SkillLoader.select(issue)
        │  遍历 _skills(sorted glob 顺序)
        │  用 skill.keywords 逗号分隔,逐个子串匹配 issue.title+body
        │  第一个命中 → 返回 Skill;全不命中 → None
        ▼
  ClaudeCliRunner.run(task, issue, ..., skill_body=skill.body or None)
        │  prompt = issue.title+body
        │  有 skill → prompt += "[Skill]\n" + skill.body
        │  _cmd: 不加 --append-system-prompt(技能已进 user prompt)
        ▼
  claude 执行时 user prompt 含技能正文 → 按"姿势"干活
```

## 试一试

```bash
python -m pytest tests/test_skill_loader.py -v   # SkillLoader 单测(不调 claude)
python sessions/zh/s08_skills.py                  # demo:StubRunner + skill 选中
```

demo 创建示例技能(`workspace/skills/code-review/SKILL.md`,keywords=`review, 审查, PR, code review`),创建一条 title="review the auth module" 的 issue,SkillLoader 选中 `code-review` 技能,daemon 执行时(StubRunner)会打印选中信息。

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s08 |
|---|---|---|
| frontmatter 字段 | `name`/`description`(`skill/frontmatter.go:27`) | `name`/`description`/`keywords` 关键词(mica 在 multiCa 基础上加了 keywords) |
| 技能选择 | 显式关联到 agent(agent 有这些技能) | 按 issue 内容关键词召回(keywords 子串匹配 title+body) |
| 注入方式 | 写文件到工作目录,agent CLI 自己发现(`internal/daemon/execenv/`) | 技能正文拼入 user prompt(不进 `--append-system-prompt`) |
| 技能来源 | DB + 本地目录 + 导入(`local_skills.go:463`) | 磁盘 `workspace/skills/**/SKILL.md` |
| 内置技能 | `builtin_skills/`(autopilots/creating-agents/mentioning/...等 8 个) | 示例 `code-review` |

一句话:**s08 把「技能 = 磁盘 markdown + 关键词召回 + user prompt 注入」做出来了**——`keywords` 关键词子串匹配(在 multiCa 的 name/description 基础上加了一个简单字段),技能正文拼入 user prompt(不跨轮残留)。与生产的差异是:mica 按 issue 内容召回(multiCa 按 agent 关联)、mica 用 user prompt 注入(multiCa 写文件到工作目录)。
