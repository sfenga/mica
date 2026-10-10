# Proposal: add-feishu-chat

## Why

KI-001（docs/known-issues.md）：Mica 没有独立的 Chat 模型，飞书文本消息一律建 Issue 或 Issue
Comment——普通咨询污染看板；连续消息只有 parent_id/root_id 命中 IssueOrigin 才能接回，同群直发、
回复 bot 消息都会误开新 Issue；评论树丢失飞书回复关系；已有 Issue 的 `agent=` 指定失效；评论合并
语义不适合即时对话。Multica 用 ChatSession/ChatMessage + 防抖批次 + `/issue` 命令完整解决了这五点
（调研结论与本变更设计均出自对 multica 仓库 `server/internal/integrations/channel/engine/` 的源码
分析）。本变更按 KI-001 定稿方案（2026-10-09，见 known-issues KI-001「定稿方案」节）落地。

## What Changes

- **迁移 012**：新建 `chat_session`（daemon 属主三元组 session_id/runtime_id/work_dir）、
  `channel_chat_binding`（`(channel, tenant_key, conversation_id, thread_id)` 组合唯一，1:1 会话）、
  `chat_message`（task_id 双向、message_kind、channel_ingested）；`agent_task` 的 `issue_id` 放开
  NOT NULL，新增 `chat_session_id`/`chat_input_task_id`/`reply_to_message_id`/`reply_in_thread`。
- **入站路由唯一规则**：消息按 `(chat_id, thread_id)` 查 binding，命中进该会话，未命中新建会话；
  `issue_origin` 退出入站（保留纯出站锚点）。群聊仅 @bot；text/post 拍平，媒体占位符。
- **Agent 绑定不可变**：首条消息 `agent=` 决定会话绑定（缺省 `FEISHU_DEFAULT_AGENT_ID`），之后
  `agent=`/`squad=` 只剥离并回引导；Squad 不进 Chat。
- **合并窗口**：3 秒静默窗口只防抖任务创建；flush 时密封 `chat_input_task_id` + 冻结 `reply_to`；
  停机 Drain、启动扫描恢复；在跑时新消息只落库。
- **认领分叉**：`claim_next` 改 `LEFT JOIN issue`，issue 守卫只挂 issue 分支，chat 分支挂
  `chat_session.status='active'` 守卫 + per-session 互斥；daemon `run_once` 按
  `task.chat_session_id` 分叉（resume 三级回退、work_dir 属主 pin 回、不走 worktree、
  输入读取失败保留重投、assistant 回复落 `chat_message`）。
- **出站**：Typing 表情回执替代受理卡片；chat 终态复用 KI-008 reply 机制（6 个不可回复业务码才
  回退群发）；Issue 终态通知保留（有意偏离 Multica）。
- **`/issue` 命令**：`channel_command` 落库不触发 chat run；建 Issue + 写 `issue_origin`；同步回执
  + 深链；负责人取会话绑定 Agent。
- **文档**：known-issues/interview/postgresql-core 按追加式更新（保留原文），README/architecture/
  test-plan 小改保持现状描述准确。

## Capabilities

### New Capabilities
- `mica-chat`: 飞书 Chat 模型——会话隔离与绑定路由、Agent 绑定、消息落库与去重、合并窗口与输入
  密封、chat 任务认领串行化、执行与续接（resume/work_dir）、出站（Typing 回执 + 冻结 reply_to）、
  `/issue` 命令。

### Modified Capabilities
- `mica-core`: 「Task 实体与执行队列」——`issue_id` 放开 NOT NULL，task 可挂 `chat_session_id`，
  新增 chat 任务字段；「Daemon 认领执行循环」——认领 SQL 按 issue/chat 两种任务形状分叉互斥与
  守卫，chat 分支不驱动 issue 状态。

## Impact

- **代码**：`mica/domain/models.py`、`mica/application/`（新增 `chat.py`，改 `channels.py`、
  `services.py`）、`mica/infrastructure/database/`（新增 `chat_repository.py`，改
  `repositories.py`）、`mica/runtime/`（`daemon.py`、`workspace.py`、`providers/*`）、
  `mica/integrations/feishu.py`、`mica/bootstrap.py`、`mica/web` 查询核对。
- **配置**：无新增必填环境项；飞书应用需人工开通表情回复权限（缺失时回执降级为 no-op）。
- **文档**：见 tasks §9；known-issues KI-001 状态与 KI-008 交叉注记。
- **行为变更（硬切，无历史兼容）**：上线后飞书普通消息不再建 Issue；存量飞书起源 Issue 的话题内
  新消息进入 chat 而非评论。README 与 known-issues 声明。
- **约束**：batcher 为 API 进程内存态，单 API 实例部署；飞书表情回复权限为回执功能前置。
- **交付**：单提交（代码 + 迁移 + 测试 + 文档 + 本变更归档前的勾选）。
