# s16 — Execution Context:把执行上下文物化到 workdir

s15 让 daemon 可以常驻后台自动消费 queued task。s16 继续补上 daemon 执行前的 prepare 阶段:**把本次 task 需要的 agent、issue、project、skill 信息写成 runtime 可见文件**。

这章不改 claim 逻辑,也不直接启动 CLI。它只回答一个问题:

```text
runtime/CLI 启动前,应该能在工作目录里看到哪些上下文?
```

## 1. 为什么需要 Execution Context

前面章节里,daemon 直接把信息作为参数传给 runner:

```text
append_system_prompt=agent.system_prompt
skill_body=merge_skills(bound)
cwd=project.local_path
```

这对教学最小闭环足够,但不利于理解 multiCa 的 daemon/execenv 思想。生产系统通常会先准备执行环境:

```text
task
  -> resolve issue / agent / project / skills
  -> write runtime-visible context files
  -> launch runtime in prepared workdir
```

这样 CLI 不只收到一段 prompt,还可以通过文件系统看到稳定、可审计、可清理的上下文。

## 2. s16 写出的目录

教学版把上下文写到 project workdir 下:

```text
project workdir/
  .mica/
    tasks/
      <task_id>/
        agent-identity.md
        task-context.md
        skills/
          code-review/
            SKILL.md
        manifest.json
```

这些文件分别承担不同职责:

```text
agent-identity.md   当前 agent 是谁、绑定哪些 skill、system_prompt 是什么
task-context.md     当前 issue/task/project 的可读上下文
skills/*/SKILL.md   Agent.skill_names 解析出的 skill 文件
manifest.json       本次 prepare 写了哪些文件,用于审计和清理
```

## 3. ExecutionContextBuilder

核心对象是 `ExecutionContextBuilder`:

```python
builder = ExecutionContextBuilder(
    issue_store,
    task_store,
    agent_store=agent_store,
    skill_loader=skill_loader,
    project_store=project_store,
)
ctx = builder.build(task)
```

它做五步:

```text
1. task.issue_id -> Issue
2. issue.project_id -> Project -> workdir
3. task.assignee_id -> Agent
4. Agent.skill_names -> SkillLoader.get(name)
5. 写 agent/task/skills/manifest 文件
```

它不做三件事:

```text
不 claim task
不调用 runner
不改变 task 状态
```

这让 prepare 阶段可以单独测试,也能在失败时明确知道问题发生在“准备上下文”而不是“runtime 执行”。

## 4. Agent Identity

`agent-identity.md` 由 `Agent` 渲染:

```markdown
# Agent Identity

- Agent ID: agent-review
- Name: Review Agent
- CLI: claude
- Runtime: local
- Bound Skills: code-review

## Instructions

Review carefully before proposing changes.
```

这对应 s14 的 Agent Profile。区别是:s14 把它作为 runner 参数传入,s16 把它写成 runtime 可见文件。

## 5. Task Context

`task-context.md` 由 `Issue`、`Task`、`Project` 共同渲染:

```markdown
# Task Context

## Issue

- Issue ID: ...
- Workspace ID: ws-1
- Title: review auth changes
- Status: todo
- Assignee: agent:agent-review

### Body

Check the login redirect and session handling.

### Comments

- Focus on auth bypass edge cases.

## Task

- Task ID: ...
- Status: queued
- Trigger Comment: (none)

## Project

- Project ID: ...
- Project Name: Demo Project
- Local Path: /tmp/.../demo-project
```

这一步把“要做什么”和“在哪里做”变成可审计文件。

## 6. Skills Materialization

s16 沿用 s14 的 agent-bound skill 模型:

```text
Agent.skill_names
  -> SkillLoader.get(name)
  -> .mica/tasks/<task_id>/skills/<skill-name>/SKILL.md
```

Issue 不参与 skill 选择。写出的 `SKILL.md` 保留 `name`、`description` 和正文:

```markdown
---
name: code-review
description: 审查代码变更,关注安全/正确性/可维护性
---

You are reviewing code...
```

这样 runtime 可以通过文件系统读取技能,而不是只依赖 daemon 拼接 prompt。

## 7. Manifest

`manifest.json` 记录本次 prepare 的结果:

```json
{
  "task_id": "...",
  "issue_id": "...",
  "workspace_id": "ws-1",
  "agent_id": "agent-review",
  "project_id": "...",
  "workdir": "...",
  "files": [
    "agent-identity.md",
    "task-context.md",
    "skills/code-review/SKILL.md",
    "manifest.json"
  ]
}
```

manifest 的作用是:

```text
审计       知道某次执行准备了什么
调试       runner 出错时能回看上下文
清理       后续可以按 manifest 删除 sidecar 文件
```

## 8. 与 multiCa 的对应关系

| 维度 | mica s16 | multiCa |
|---|---|---|
| prepare 对象 | `ExecutionContextBuilder` | daemon `runTask()` + execenv |
| agent identity | `agent-identity.md` | runtime config / agent identity section |
| task context | `task-context.md` | task brief / issue context / comments |
| skill materialize | `.mica/tasks/<id>/skills/*/SKILL.md` | agent skill bundle / runtime skill context |
| manifest | `manifest.json` | sidecar/marker/cache/cleanup metadata |
| 启动 runtime | 本章不做 | prepare 后启动 provider CLI |

一句话:**s16 把“执行需要知道的信息”从 Python 参数变成 workdir 里的上下文文件。**

## 试一试

```bash
python -m pytest tests/test_execution_context.py -v
python sessions/zh/s16_execution_context.py
```

demo 会创建临时 project、agent、skill、issue 和 task,然后打印 `.mica/tasks/<task_id>/` 下写出的文件。
