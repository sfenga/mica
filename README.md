# mica

渐进式教学仓库,做一个最小可用的 multiCa —— 把 coding agent 变成数字队友:
issue 板 → 派给 agent → 本地 daemon 认领 → spawn 真实 CLI(`claude`)执行 → 实时回传 → 落盘 → 上看板。

每章一个可直接运行的 Python 文件 + 同名 `.md`,每章只引入一个新概念,前置代码原样保留。

## 章节索引

| 章 | 主题 |
|---|---|
| s01 | Issue 模型 + 看板 + 多态 assignee + 状态机 |
| s02 | Daemon 认领执行循环(stub runner) |
| s03 | `claude` CLI runner,真实模型接入 |
| s04 | JSONL 持久化 + 会话重放 |
| s05 | WebSocket 实时进度 |
| s06 | FastAPI 单页看板 |
| s07 | markdown 技能注入 |
| s08 | cron autopilot |
| s09 | 多 CLI + PATH 探测 |
| s10 | 多 workspace 隔离 |
| s11 | (可选附录)squads |

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
