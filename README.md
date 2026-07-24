# mica

渐进式教学仓库,做一个最小可用的 multiCa —— 把 coding agent 变成数字队友:
issue 板 → 派给 agent → 本地 daemon 认领 → spawn 真实 CLI(`claude`)执行 → 实时回传 → 落盘 → 上看板。

每章一个可直接运行的 Python 文件 + 同名 `.md`,每章只引入一个新概念,前置代码原样保留。

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
| s08 | markdown 技能注入(`match` 正则) |
| s09 | cron autopilot |
| s10 | 多 CLI + PATH 探测(1 daemon : N agent) |
| s11 | 多 workspace 隔离 + 成员资格 |
| s12 | (可选附录)squads |

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# 跑某章(Phase 1 已就绪)
python sessions/zh/s01_issue_board.py
python sessions/zh/s02_daemon_claim.py

# 测试
python -m pytest tests/ -v
```

> s03 起需本机已安装并登录 `claude`(Claude Code)。Phase 1 不依赖任何 CLI。
