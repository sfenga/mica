# s99 — Mission Control:最终集成控制台

s99 不是新的核心机制章节,而是把 s01-s14 的对象集中到一个本地页面里观察和操作。它让你能在浏览器里创建 issue、运行 daemon、查看 trace、配置 agent profile、绑定 skill,从而验证整条学习系统是否接起来了。

## 1. s99 集成了什么

```text
Issues / Tasks / Daemon / Runner      前面章节的执行主线
Runtime / MultiCliRunner              多 CLI 与 runtime 观测
Workspace / Project workdir           工作区和项目目录
SquadRouter                           leader-first 多 agent 协调
AgentStore                            创建/编辑 agent profile
SkillLoader                           读取 workspace/skills 下的 SKILL.md
Agent.skill_names                     声明 agent 绑定哪些 skill
Daemon.execute                        按 task.assignee_id 查 Agent,加载绑定 skill 后注入 runner
```

最重要的是最后四行。s99 的控制台只是入口,真正的模型仍然是:

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

## 2. Agent 与 skill 的 API

```text
GET    /api/agents                          列出配置 agent;空时回退 runtime 派生 agent
POST   /api/agents                          创建 agent profile
GET    /api/agents/{id}                     读取单个 agent
PATCH  /api/agents/{id}                     更新 name/cli/runtime_id/system_prompt/skill_names
POST   /api/agents/{id}/skills?skill_name=  绑定 skill name
DELETE /api/agents/{id}/skills/{skill_name} 解绑 skill name
GET    /api/skills                          列出 SkillLoader 读到的 skill
GET    /api/skills/{name}                   按 name 读取单个 skill
```

控制台里的 Agents 面板会调用这些 API。绑定 skill 时保存的是 skill name,不是 skill 正文;执行时 daemon 再用 `SkillLoader.get(name)` 解析成当前文件内容。

## 3. daemon 注入链路

执行 task 时,s99 沿用 s14 的 Agent Profile 逻辑:

```python
agent = self.agent_store.get(task.assignee_id) if self.agent_store else None
system_prompt = agent.system_prompt if agent else ""

bound = []
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

这段代码表达了三个边界:

```text
assignee_id 决定查哪个 Agent
Agent.skill_names 决定加载哪些 skill
Issue 只提供任务内容,不参与 skill 选择
```

如果 assignee 是派生 agent,但没有在 AgentStore 里配置 profile,那么 daemon 不会注入 system_prompt,也不会注入 skill_body。这样可以避免“只因为 issue 标题包含某个词,agent 就获得额外技能”的隐式行为。

## 4. 控制台怎么使用

```text
Agents 页
  创建 agent-ui
  填 system_prompt
  选择或输入 skill_names,例如 code-review
  保存后 agent profile 写入 agents.jsonl

Issues 页
  assignee 下拉选择 agent-ui
  创建 issue

底栏
  Run daemon once

Issue 详情 / Trace
  查看 agent 评论、task 事件、runner 收到的 system_prompt 和 skill_body
```

Skills 页只展示 `SkillLoader` 读到的技能文件。它不提供“输入 issue title 预览匹配 skill”的功能,因为当前模型里 Issue 不负责 skill 选择。

## 5. 与 multiCa 的对应关系

| 维度 | mica s99 | multiCa |
|---|---|---|
| 控制台 | FastAPI + 内联 vanilla JS | 完整 web 应用 + CLI |
| agent 配置 | AgentStore + agents.jsonl | agent 表、handler、权限与 workspace 隔离 |
| skill 列表 | `workspace/skills/**/SKILL.md` | workspace skills + builtin skills + imported skills |
| skill 绑定 | `Agent.skill_names` | `agent_skill` 关联表 |
| 执行注入 | user prompt 中的 `skill_body` + `--append-system-prompt` | daemon/execenv 构造 runtime 可见的 agent identity 和 skill 上下文 |
| Issue 关系 | Issue 不绑定 skill | task 的 agent 决定可见 skill bundle |

一句话:**s99 的价值是把完整系统跑起来、看得见、能操作;它不是学习重点本身,而是验证 s01-s14 主线的一块仪表盘。**

## 试一试

```bash
python -m pytest tests/test_mission_control_agents.py -v
python sessions/zh/s99_mission_control.py
```

启动后打开 `http://127.0.0.1:8000/`,按“创建 agent -> 绑定 skill -> 创建 issue -> run daemon once -> 看 trace”的顺序走一遍,就能观察 agent-bound skill 的完整链路。
