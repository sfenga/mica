# s16 — Console Agents Panel(Mission Control + Agent Profile 前端集成)

s15 把 agent 做成可配置执行主体,但只在 CLI demo 里。s16 是最终控制台章,把 s15 的 Agent Profile 集成进 Mission Control:通过 API 和 Agents 面板创建/编辑 agent、绑 skill、设 system_prompt,issue 创建时 assignee 从 agent 列表选。

## 1. s16 在 s14 基础上加了什么

```text
Agent.system_prompt / skill_names        身份提示词 + 绑定技能名
AgentStore(agents.jsonl)                  持久化,create/get/list/update/bind/unbind
SkillLoader 使用 keywords 关键词召回 + get(name) 按名解析
merge_skills                               两层 skill 合并去重
Daemon.execute                             按 assignee_id 注入 system_prompt + skill
API                                        GET/POST/PATCH /api/agents、skill 绑定/解绑
Agents 面板                                创建、编辑 system_prompt、多选绑 skill
issue assignee 下拉                        从 /api/agents 来(替代写死 agent-1)
```

## 2. API

```text
GET    /api/agents                          AgentStore.list();空时回退 runtime.clis 派生
POST   /api/agents                          create(id, name, cli, system_prompt, skill_names)
GET    /api/agents/{id}                     单个 agent;AgentStore 无则查派生
PATCH  /api/agents/{id}                     更新 name/cli/runtime_id/system_prompt/skill_names
POST   /api/agents/{id}/skills?skill_name=  绑定 skill
DELETE /api/agents/{id}/skills/{skill_name} 解绑 skill
```

`GET /api/agents` 合并派生 + 配置:

```python
def _all_agents():
    """派生 agent(始终在,agent-1/agent-2 兜底)+ AgentStore 配置的 agent,
    id 冲突时 AgentStore 覆盖派生。"""
    by_id = {}
    for a in _derived_agents:
        by_id[a.id] = a
    if agent_store is not None:
        for a in agent_store.list():
            by_id[a.id] = a   # AgentStore 覆盖同 id 派生
    return list(by_id.values())
```

所以创建自定义 agent 后,派生 `agent-1`/`agent-2` 不会从下拉里消失(避免已有 task 的 assignee 找不到);用户也可以用 id `agent-1` 在 AgentStore 里重定义它的 system_prompt/skill。

## 3. daemon 注入(沿用 s15)

```python
agent = self.agent_store.get(task.assignee_id) if self.agent_store else None
system_prompt = agent.system_prompt if agent else ""
bound = [self.skill_loader.get(n) for n in agent.skill_names] if agent else []
matched = self.skill_loader.select(issue)
skill_body = merge_skills(bound, matched)
result = self.runner.run(task, issue, [], on_event,
                         resume_session_id=...,
                         append_system_prompt=system_prompt or None,
                         skill_body=skill_body or None,
                         cwd=workdir or None)
```

- 配置过的 agent:注入 `system_prompt`(`--append-system-prompt`)+ 绑定 skill + issue 匹配 skill。
- 未配置 agent(派生 agent,不在 AgentStore):`system_prompt=None`,只 issue 匹配 skill(兼容)。

## 4. 前端 Agents 面板

```text
左侧 nav 加 Agents 项
Agents 页:
  创建表单  id / name / cli / skill_names(逗号)/ system_prompt(textarea)
  agent 卡片  name + id + cli/runtime
              已绑 skill 标签(✕ 解绑)
              绑定 skill 下拉(只列未绑的)
              system_prompt textarea + Save prompt(PATCH)
Issues 创建表单:
  assignee 下拉从 GET /api/agents 来(替代写死 agent-1)
```

复用 s14 已有基础设施:`esc` / `renderMd` / `badge` / `closeWatch` / 目录浏览器 modal / `setStatus`。

## 5. 试一试

```bash
python -m pytest tests/test_console_agents.py -v   # API + 注入 + 兜底
python sessions/zh/s16_console_agents.py            # 起 Mission Control → http://127.0.0.1:8000/
```

浏览器流程:

```text
Agents 页   -> 创建 agent-ui(name + system_prompt + skill_names=code-review)
             -> 卡片里绑/解绑 skill,编辑 system_prompt,Save
Issues 页   -> assignee 下拉选 agent-ui -> 建 issue
底栏        -> ▶ Run daemon once
点开 issue  -> 看 🤖 Agent 评论 + Trace 看 system_prompt/skill 注入的事件
```

## 6. 和 multiCa 的区别

s16 的重点是"把 agent 配置集成进控制台",对照点在 UI/API 形态:

| 维度 | mica s16 | multiCa 生产实现 |
|---|---|---|
| 控制台 | 单页 FastAPI + 内联 vanilla JS | 完整 Next.js web 应用 |
| agent 配置入口 | Agents 面板(创建/编辑/绑 skill) | agent 设置页 + CLI(`multica agent`) |
| agent API | `POST/PATCH /api/agents` + skill 绑定端点 | agent handler 全套 CRUD + 权限 |
| assignee 来源 | `GET /api/agents`(AgentStore 或派生兜底) | agent 表 + workspace 隔离查询 |
| 注入 | s15 同:`--append-system-prompt` + user prompt skill body | CLAUDE.md `## Agent Identity` + skill 文件物化(见 s15 对照) |

agent 配置本身的对照(system prompt / skill 绑定 / runtime)见 s15_agents.md,这里不重复。

一句话:**s16 把 s15 的 agent 配置做成控制台可操作的面板 + API,issue assignee 从真实 agent 列表来;multiCa 是完整 Next.js 应用,agent 配置走专门设置页 + CLI,注入物化到工作目录。**

## 7. 本章边界

s16 做了:

```text
AgentStore + agents.jsonl 持久化
Agent CRUD + skill 绑定/解绑 API
Agents 面板(创建/编辑/绑 skill)
issue assignee 下拉从 /api/agents
daemon 注入 agent system_prompt + 绑定 skill(沿用 s15)
SkillLoader 使用 keywords 关键词召回,并提供 get(name) 供 agent 绑定解析
```

s16 不做:

```text
agent 权限模型(permission_mode / visibility / owner)
agent 级 model / mcp / custom_args / env
CLAUDE.md 注入(用 --append-system-prompt 代替)
skill 物化成文件让 CLI 自发现
agent 归属 workspace 隔离(目前 AgentStore 全局)
```

一句话总结:**s16 是最终控制台章,把 s15 的 Agent Profile 集成进 Mission Control —— Agents 面板可创建/编辑/绑 skill,issue assignee 从真实 agent 来,daemon 按 assignee_id 注入配置。**
