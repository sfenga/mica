# mica

渐进式教学仓库,做一个最小可用的 multiCa —— 把 coding agent 变成数字队友:
issue 板 → 派给 agent → 本地 daemon 认领 → spawn 真实 CLI(`claude`)执行 → 实时回传 → 落盘 → 上看板。

每章一个可直接运行的 Python 文件 + 同名 `.md`,每章只引入一个新概念,前置代码原样保留。这个仓库不是生产系统,而是一个按章节展开的学习项目:用最小代码讲清楚 issue、task、daemon、runtime、workspace、skills、squads、workdir 和控制台如何连接起来。

## 章节索引

| 章 | 主题 |
|---|---|
| s01 | Issue 模型 + 看板 + 7 态状态机 + 多态 assignee |
| s02 | Task 实体 + Daemon 认领 task + StubRunner |
| s03 | `claude` CLI runner,真实模型接入 |
| s04 | JSONL 持久化(issues+tasks+sessions)+ 重放 + orphan |
| s05 | 评论建新 task + `--resume` 上一 task |
| s06 | WebSocket 实时进度 |
| s07 | FastAPI 单页看板 + 创建/转移/评论 |
| s08 | markdown skill 文件加载(`SkillLoader` + `SKILL.md`) |
| s09 | cron autopilot |
| s10 | 多 CLI + PATH 探测(1 daemon : N agent) |
| s11 | 多 workspace 隔离 + 成员资格 |
| s12 | Squads:leader-first 多 agent 协调 |
| s13 | Project + workdir:让 agent 在正确代码目录执行 |
| s14 | Agent Profile:system_prompt + skill 绑定 + daemon 注入 |
| s99 | Mission Control:最终集成控制台,用于观察和操作全系统 |

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# 跑某章
python sessions/zh/s01_issue_board.py
python sessions/zh/s02_task_daemon.py
python sessions/zh/s13_project_workdir.py
python sessions/zh/s14_agents.py

# 打开最终集成控制台
python sessions/zh/s99_mission_control.py
# 浏览器访问 http://localhost:8000/

# 测试
python -m pytest -q
```

> s03 起如果使用真实 `ClaudeCliRunner`,需本机已安装并登录 `claude`(Claude Code)。大多数测试和 demo 默认使用 `StubRunner`,不依赖真实 CLI。

## 推荐学习路线

1. 先读 s01-s02,理解 issue 与 task 为什么分层。
2. 再读 s03-s05,理解真实 CLI、session_id 和评论续跑。
3. 继续读 s06-s07,观察执行事件如何进入实时通道和 Web 看板。
4. 读 s08-s10,学习 skills、autopilot、多 CLI 与 runtime 边界。
5. 读 s11-s14,把系统扩展到 workspace、squad、project workdir 和 agent profile。
6. 最后运行 s99,用 Mission Control 一次性观察全部对象和流程。

## 安全说明

- 本项目是本地教学项目,不要把控制台对公网暴露。
- `s99_mission_control.py` 默认绑定 `127.0.0.1`;如果改成 `0.0.0.0`,目录浏览 API 会暴露服务端目录结构。
- `ClaudeCliRunner` 为了教学自动化默认使用 `--permission-mode bypassPermissions`;只在可信本地目录和可信任务上运行真实 CLI。
- `workspace/` 是运行时数据目录,默认已被 `.gitignore` 忽略。

## 仓库结构

```text
sessions/zh/   每章教学代码与中文文档
tests/         行为测试,覆盖核心教学契约
docs/wiki/     可同步到 wiki 的长文学习文档
openspec/      早期规格与设计记录
```
