# Design: add-feishu-chat

## 目标与约束

- 对齐 Multica chat 骨架（源码调研结论见 known-issues KI-001），按 Mica 单机单 bot 现实裁剪；
  设计定稿于 2026-10-09，全部决策已与维护者对齐。
- **硬切**：无历史兼容路由、无回退开关、不做数据迁移；存量飞书起源 Issue 的话题内新消息进入 chat。
- **单 API 实例**：batcher 为进程内存态；文档声明不允许多 API 实例。
- **单 Agent 绑定**：会话绑定 Agent 创建后不可变（Multica 同款不变量）；多 Agent 协作属 Issue 侧。
- 出站复用 KI-008 已验证的 reply 机制与降级码表，不新增出站通道。

## 关键决策（对 Multica 的对齐与偏离）

| 决策 | 结论 | 依据 |
|---|---|---|
| 定位 | 飞书 = Chat 完整入口 + `/issue` 建单入口；Issue 推进/验收只在 Web | Multica 同款；产品面对称性 |
| 隔离键 | p2p/普通群=`chat_id`；话题内=`chat_id:thread_id`（话题即会话） | Multica `larkSessionRouting` |
| Agent 绑定 | 首条消息 `agent=` 决定，创建后不可变；后续 `agent=`/`squad=` 剥离+引导 | Multica「一 bot 一 Agent」的适配：单 bot 下绑定时机=会话创建 |
| resume 指针 | `chat_session.session_id`（需 runtime 匹配）→ 本会话最近记录 session_id 的任务行 → 全新 | Multica `GetLastChatTaskSession` 回退；一次失败不断记忆 |
| work_dir | daemon 属主：首轮按 session id 建，终态随任务行 pin 回会话 | Multica `chat_session.work_dir` 写回模式 |
| 出站目标 | flush 时冻结 `reply_to`/`reply_in_thread` 到任务行（delivery 快照的简化） | 单机单渠道不需要独立快照表 |
| 通知 | chat 终态回飞书；**Issue 终态通知保留**（有意偏离 Multica） | 存量用户习惯 |
| 认领 | 共表共 daemon，认领 SQL 三分支互斥（issue/chat 形状），`LEFT JOIN issue` | Multica `ClaimAgentTask`；保留 Mica 的 issue 生命周期守卫 |
| 群 creator | 系统用户（启动时自动创建），p2p=发送者 | Multica 安装者的单机替代 |
| `/issue` 负责人 | 会话绑定 Agent，不支持命令内指定 | 决策定稿；换负责人去 Web |
| `/issue` 重复守卫 | 不做 | Multica 有 active-issue 守卫；Mica 误建成本低 |
| 身份 | `resolve_identity` 自动建用户，不做绑定令牌 | 本机可信环境 |

## 数据模型（migration 012）

```text
chat_session: id, workspace_id, title, status(active|archived),
              agent_id(不可变), creator_user_id,
              session_id / runtime_id / work_dir        ← daemon 属主三元组
channel_chat_binding: id, channel, tenant_key, conversation_id, thread_id,
              UNIQUE(channel, tenant_key, conversation_id, thread_id),
              chat_session_id UNIQUE(1:1), last_message_id, last_thread_id
chat_message: id, chat_session_id, role(user|assistant), body,
              external_message_id, parent_external_message_id,
              task_id(双向: user=被哪轮消费, assistant=哪轮产出),
              message_kind(text|channel_command), channel_ingested, created_at
agent_task:   issue_id DROP NOT NULL; + chat_session_id, chat_input_task_id,
              reply_to_message_id, reply_in_thread
```

索引：`chat_message(chat_session_id, created_at)`、`agent_task(chat_session_id)`。

## 机制要点

### 入站（`feishu.py` `_on_message` 重写）

群聊 @bot 防御检查 → 算隔离键 → `ensure_session`（组合唯一约束冲突时败者重读胜者）→
消息同步落库（去重标记同事务）→ 交给 batcher。`issue_origin` 不参与入站。

### 合并窗口（`application/chat.py` ChatBatcher）

3 秒静默窗口，**只防抖任务创建**；键为 session。flush 同事务：密封窗口内未认领消息到
`chat_input_task_id`、冻结 `reply_to`（批次最后一条）到任务行、解析 runtime。任务在跑时新消息
只落库，终态后由 flush 建 successor。停机 `Drain()`；启动扫描 `task_id IS NULL` 的 user 消息重挂。

### 认领（`repositories.py` `claim_next`）

`JOIN issue` → `LEFT JOIN`；issue 生命周期守卫只挂 issue 分支；chat 分支加
`chat_session.status='active'` 守卫 + `active.chat_session_id` 互斥（dispatched/running）。
NULL ≠ NULL 语义保证两分支互不误伤。chat 任务与 issue 任务按 `created_at` 同队竞争。

### 执行（`daemon.py` `run_once` chat 分支）

取 `chat_session` 替代 issue；resume 三级回退（见决策表）；`work_dir` 缺失则在 daemon 工作区按
session id 建目录；不走 worktree/finalize/分支交付；prompt 负载由 daemon 拼好传 provider（契约
扩展，全部 provider 适配）；**输入读取失败保留任务重投，不按空输入执行**；终态写回三元组并 pin
回会话（带"无更新任务"守卫），输出以 `role='assistant'` 落 `chat_message`。

### 出站（复用 KI-008）

Typing 表情回执替代受理卡片（回复/失败/取消时清除；API 失败降级 no-op + warning）。chat 终态
Outbox 用任务上冻结的 `reply_to`/`reply_in_thread`；仅 6 个不可回复业务码回退群发；空输出静默。
`channel_ingested` 只落库，门控逻辑随 Web Chat 接入启用。

### `/issue` 命令

`/issue <标题>` 仅首个非空行、token 边界、裸命令回 usage；消息以 `channel_command` 落库但**不触发
chat run**、不进 transcript 输入、不参与防抖；走 `TriggerService.create_issue` + 写 `issue_origin`
（external_message_id=命令消息、thread_id=所在话题）→ 保留的 Issue 终态通知按 KI-008 回到同一
话题；同步回执 `已创建 <id> — 标题` + 深链。

## 非目标（本次不做）

- Web Chat UI、共享会话的 Web 呈现、`channel_source` 标记消费（`channel_ingested` 仅落库）。
- `/clear`、`/new` 命令及 context generation / route_revision / retired_at 全套代际机制。
- 会话内切换 Agent（sticky 或逐轮）；Squad 进入 Chat（`squad=` 引导走 `/issue`）。
- 媒体下载管道（对账器、fire_at 延迟、S3 上传）；媒体只落占位符。
- 绑定令牌/用户绑定流程（`resolve_identity` 自动建用户）。
- 出站 message_id 落库（Multica Lark 路径同为 fire-and-forget；KI-008 方案二继续搁置）。
- 认领优先级列；chat 任务按 `created_at` 与 issue 任务公平竞争。
- 引用父消息内容注入 prompt（enricher）、merge_forward 展开。
- `/issue` 活跃 Issue 重复守卫。
- 飞书 → Issue 评论路径（整体移除，含第 0 步小修——路径不存在，修复对象消失）。
- 历史数据迁移与回填；回退开关。
