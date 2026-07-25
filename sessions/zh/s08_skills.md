# s08 — markdown 技能注入(SkillLoader + `match` 正则 + `--append-system-prompt`)

s07 让看板能操作了,但 agent 执行 task 时不知道该用什么"姿势"——修 bug 和做 code review 的方法论不同。s08 加上技能:磁盘上放 markdown 技能文件(`SKILL.md`),`SkillLoader` 扫描目录、用 frontmatter 的 `match` 正则匹配 issue,命中的技能经 `claude --append-system-prompt` 注入 prompt。换技能文件不改代码即生效。

## 一、核心设计思想

第一个思想:**技能是磁盘上的 markdown 文件**。每个技能是一个 `SKILL.md`,带 frontmatter(name/description/match)+ markdown 正文(技能内容):

```markdown
---
name: code-review
description: 审查代码变更,关注安全/正确性/可维护性
match: review|审.*查|PR
---

You are reviewing code. Focus on:
1. Security: injection, auth bypass, data leak
2. Correctness: edge cases, error handling
3. Maintainability: naming, DRY, complexity
```

技能是文件,不是代码——换个文件就换了 agent 的能力,不用改代码、不用重启(重启 SkillLoader 重新扫即可)。技能可以由团队共享(git 管理)。

第二个思想:**`match` 正则匹配 issue**。SkillLoader 扫完所有技能后,`select(issue)` 用每个技能的 `match` 正则测 `issue.title + "\n" + issue.body`,取**第一个命中**的:

```python
class SkillLoader:
    def select(self, issue) -> Skill | None:
        text = issue.title + "\n" + (issue.body or "")
        for skill in self._skills:
            if skill.match and re.search(skill.match, text):
                return skill
        return None
```

`match: "review|审.*查|PR"` 会命中 title 含 "review"/"审X查"/"PR" 的 issue;`match: "fix.*bug"` 命中 "fix login bug"。无 `match` 字段的技能**永不被自动选中**——它是给 agent 常驻的,不按 issue 匹配。第一个命中赢(按 sorted glob 顺序)。

第三个思想:**经 `--append-system-prompt` 注入**。选中技能后,daemon 把技能正文传给 `ClaudeCliRunner.run(append_system_prompt=skill.body)`,run 把它传给 `_cmd` 加 `--append-system-prompt` flag:

```python
def run(self, task, issue, context, on_event,
        resume_session_id=None, append_system_prompt=None) -> RunResult:
    ...
    proc = subprocess.Popen(
        self._cmd(prompt, append_system_prompt=append_system_prompt,
                  resume_session_id=resume_session_id), ...)

def _cmd(self, prompt, append_system_prompt=None, ...):
    cmd = [self.cli, "-p", "--verbose", "--output-format", "stream-json", prompt]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    ...
```

claude 收到 `--append-system-prompt` 后,把技能正文追加到系统提示词里——agent 在执行时就"知道"该用 review 的姿势干活。无匹配技能时不加 `--append-system-prompt`,照常执行。

第四个思想:**Daemon 在 execute 里选技能**。daemon 认领 task 后,先查技能再执行:

```python
# Daemon.execute
skill = None
if self.skill_loader:
    skill = self.skill_loader.select(issue)
result = self.runner.run(task, issue, [], on_event,
                         resume_session_id=prior_sid or None,
                         append_system_prompt=skill.body if skill else None)
```

daemon 是技能选择的入口——它知道 issue(从 task.issue_id 取),调 skill_loader.select,把结果传给 runner。

## 二、为什么这么设计

### 为什么用 `match` 正则,不像 multiCa 那样靠 agent 关联?

生产 multiCa 的技能 frontmatter 只有 `name`/`description`(`skill/frontmatter.go:17,27`),没有 `match`——技能是**显式关联到 agent** 的(agent 有这些技能),不按 issue 内容匹配。mica 加了 `match` 正则,让技能**按 issue 内容自动选中**——issue title 说 "review" 就自动用 review 技能,说 "fix bug" 就自动用 debug 技能。这适合 mica 的单 agent 场景(一个 agent 干多种活,按 issue 内容切姿势)。multiCa 的多 agent 场景里,不同 agent 本身就有不同技能,不需要按 issue 匹配。

### 为什么经 `--append-system-prompt`,不像 multiCa 那样写文件到工作目录?

生产 multiCa 把技能文件写到工作目录(`internal/daemon/execenv/`),agent CLI(claude/codex)自己发现工作目录里的技能文件(CLAUDE.md/AGENTS.md 机制)。mica 用 `--append-system-prompt` 直接把技能正文塞进 claude 的系统提示词——更直接、不需要文件系统操作。代价是技能正文长度受 CLI 参数限制(长技能可能超限);好处是不需要管工作目录、文件权限、清理。这是 mica 的简化:用 CLI flag 替代文件系统注入。

### 为什么用简单 frontmatter 解析,不引 YAML 库?

技能 frontmatter 只有三个字段(name/description/match),用 `key: value` 逐行解析就够,不需要 YAML 库。multiCa 的 `ParseSkillFrontmatter` 也只取 name/description 两个字段(`frontmatter.go:27`),不解析复杂 YAML 结构。保持简单。

### 为什么第一个命中赢,不给所有命中的技能?

多技能同时注入会让系统提示词过长,且技能间可能冲突(一个说"快速修复"、一个说"深度审查")。第一个命中赢(sorted glob 顺序)让技能选择确定、可预测。如果需要多技能,可以写一个"组合技能"包含多个方面的指引。

## 三、技能怎么流转

```
  daemon 认领 task → 取 issue(issue_store.get(task.issue_id))
        │
        ▼
  SkillLoader.select(issue)
        │  遍历 _skills(sorted glob 顺序)
        │  用 skill.match 正则测 issue.title+body
        │  第一个命中 → 返回 Skill;全不命中 → None
        ▼
  ClaudeCliRunner.run(task, issue, ..., append_system_prompt=skill.body or None)
        │  _cmd: 有 skill → 加 --append-system-prompt <skill.body>;无 → 不加
        ▼
  claude 执行时系统提示词含技能正文 → 按"姿势"干活
```

## 试一试

```bash
python -m pytest tests/test_skill_loader.py -v   # SkillLoader 单测(不调 claude)
python sessions/zh/s08_skills.py                  # demo:StubRunner + skill 选中
```

demo 创建示例技能(`workspace/skills/code-review/SKILL.md`,match=`review|审.*查|PR`),创建一条 title="review the auth module" 的 issue,SkillLoader 选中 `code-review` 技能,daemon 执行时(StubRunner)会打印选中信息。

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s08 |
|---|---|---|
| frontmatter 字段 | `name`/`description`(`skill/frontmatter.go:27`) | `name`/`description`/`match` 正则(mica 自加) |
| 技能选择 | 显式关联到 agent(agent 有这些技能) | 按 issue 内容自动匹配(`match` 正则测 title+body) |
| 注入方式 | 写文件到工作目录,agent CLI 自己发现(`internal/daemon/execenv/`) | `--append-system-prompt` 直接塞系统提示词 |
| 技能来源 | DB + 本地目录 + 导入(`local_skills.go:463`) | 磁盘 `workspace/skills/**/SKILL.md` |
| 内置技能 | `builtin_skills/`(autopilots/creating-agents/mentioning/...等 8 个) | 示例 `code-review` |

一句话:**s08 把「技能 = 磁盘 markdown + 自动匹配 + CLI 注入」做出来了——`match` 正则按 issue 内容选技能,`--append-system-prompt` 注入**;与生产的差异是:mica 按 issue 匹配(multiCa 按 agent 关联)、mica 用 CLI flag 注入(multiCa 写文件到工作目录)。`match` 正则是 mica 自己的设计。
