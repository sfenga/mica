# mica-chat Specification

> 飞书 Chat 模型契约。Chat 与 Issue 共用 Task 调度/Provider/事件/取消机制，但不共享上层业务语义：
> 普通飞书消息进入 ChatSession（不出现在 Issue 看板），仅显式 `/issue` 建 Issue。

## ADDED Requirements

### Requirement: 会话隔离与绑定路由

系统 SHALL 以 `(channel, tenant_key, conversation_id, thread_id)` 为会话隔离键维护
`channel_chat_binding`（与 `chat_session` 1:1）：私聊与普通群消息的键不含 `thread_id`
（即 `chat_id` 维度），话题内消息（携带 `thread_id`）的键含之——同一群内两个话题 SHALL 隔离为
两个会话。入站消息 SHALL 仅按该键路由：命中 binding 进入对应会话，未命中创建新会话（并发首条消息
SHALL 由组合唯一约束兜底，败者重读胜者会话）。`issue_origin` SHALL NOT 参与入站路由（仅作出站
锚点）。

#### Scenario: 私聊进入同一会话
- **WHEN** 同一用户在私聊中先后发送两条消息（非回复关系）
- **THEN** 两条消息进入同一 `chat_session`（键为 chat_id）

#### Scenario: 同群两个话题隔离
- **WHEN** 用户在群 `oc_A` 的话题 `omt_B` 与话题 `omt_C` 各 @bot 发消息
- **THEN** 创建两个不同 `chat_session`，互不共享上下文

#### Scenario: 并发首条消息只建一个会话
- **WHEN** 两个用户几乎同时在同一话题首条 @bot
- **THEN** 组合唯一约束保证仅一个会话创建成功，另一消息落入该会话

#### Scenario: 旧 Issue 话题的追加进入 chat
- **WHEN** 用户回复 KI-001 之前由飞书创建的存量 Issue 的来源消息
- **THEN** 该消息按绑定路由进入（新建的）chat 会话，SHALL NOT 成为该 Issue 的评论

### Requirement: Agent 绑定不可变

`chat_session.agent_id` SHALL 在会话创建时由首条消息的显式 `agent=` 前缀决定（缺省取
`FEISHU_DEFAULT_AGENT_ID`），创建后 SHALL NOT 变更。已绑定会话中后续消息的 `agent=`/`squad=`
前缀 SHALL 从正文剥离（不污染 transcript）且 SHALL NOT 改变路由，系统 SHALL 回引导说明（换话题
或 `/issue`）。`agent=` 指向不存在的 Agent 时 SHALL fail-closed：本轮不执行、不绑定，回引导说明，
SHALL NOT 以默认 Agent 冒充执行。Squad SHALL NOT 进入 Chat。

#### Scenario: 首条消息决定绑定
- **WHEN** 会话首条消息为 `agent=geometry-agent 画个布局`
- **THEN** 会话 `agent_id=geometry-agent`，本轮任务由其执行

#### Scenario: 已绑定会话的 agent= 被剥离并引导
- **WHEN** 绑定为 X 的会话中收到 `agent=y 帮我看看`
- **THEN** 正文剥离前缀后进入 transcript，本轮由 X 执行，并收到引导回复；SHALL NOT 改绑

#### Scenario: 无效 agent fail-closed
- **WHEN** 首条消息 `agent=not-exist 任务`
- **THEN** 回"未找到 Agent"引导，本轮不执行、会话绑定延迟到首个有效执行

### Requirement: 消息落库与去重

每条入站消息 SHALL 同步持久化为 `chat_message`（role=user），与去重标记在同一事务提交；重复事件
SHALL 零副作用。群聊 SHALL 仅处理 @bot 消息。`text` 与 `post`（富文本）SHALL 拍平为纯文本；
`image`/`file`/`audio`/`media`/`video` SHALL 以占位符（如 `[图片]`）进 transcript。命令消息
（`channel_command`）SHALL 落库但对 Agent 输入不可见。

#### Scenario: 重复事件零副作用
- **WHEN** 飞书重推同一 `event_id` 的消息事件
- **THEN** 不产生新的 `chat_message`、任务或出站消息

#### Scenario: 富文本拍平
- **WHEN** 收到 post 类型消息（含加粗与链接段落）
- **THEN** 以拍平后的纯文本落库并进 transcript

### Requirement: 合并窗口与输入密封

系统 SHALL 以 3 秒静默窗口合并会话内连续消息，且 **SHALL 只防抖"创建任务"**：消息本身同步落库，
每窗口 flush 时恰好创建一个 chat 任务，同事务将窗口内未认领消息密封到该任务的
`chat_input_task_id`，并把批次最后一条消息 id 冻结为任务的 `reply_to`（含 `reply_in_thread`）。
任务在跑时到达的消息 SHALL 只落库，由终态后的 flush 建立 successor。进程停止时 SHALL Drain 全部
未触发窗口；启动时 SHALL 扫描存在未认领 user 消息的会话并重挂 flush。

#### Scenario: 连续多条消息一次触发
- **WHEN** 用户 1 秒内连发"看上海天气"和"还有青岛"
- **THEN** 两条消息分别落库，3 秒后仅创建一个任务，两 sealed 消息同轮交付

#### Scenario: 在跑时不建并发任务
- **WHEN** 会话有 dispatched/running 任务期间窗口 flush 到期
- **THEN** 不创建并行任务；消息落库等待终态后 successor

#### Scenario: 崩溃后恢复
- **WHEN** API 进程在窗口内崩溃重启
- **THEN** 启动扫描发现未认领消息并补建任务，消息不丢失

### Requirement: Chat 任务认领与串行化

chat 任务与 issue 任务 SHALL 共用 `agent_task` 队列并由同一 daemon 认领。认领互斥 SHALL 按任务
形状分叉：issue 任务按 `(issue_id, agent_id)`，chat 任务按 `chat_session_id`——同一会话同时至多
一个 dispatched/running 任务。`chat_session.status='archived'` 的会话 SHALL NOT 认领新任务（归档
 SHALL 取消其排队与在跑任务）。chat 任务 SHALL NOT 驱动任何 issue 状态。

#### Scenario: 双任务同队竞争
- **WHEN** 队列中同时存在 issue 任务与 chat 任务（同 runtime）
- **THEN** 两者按 created_at 顺序都被认领执行，互不阻塞

#### Scenario: 同会话任务串行
- **WHEN** 会话任务在跑期间后继任务入队
- **THEN** 后继任务保持 queued，直至前序任务到达终态

#### Scenario: chat 任务不影响看板
- **WHEN** 一批 chat 任务完成
- **THEN** 无任何 issue 状态变化

### Requirement: Chat 任务执行与续接

daemon 认领 chat 任务后 SHALL 取 `chat_session` 构建执行输入（密封批次 + 近期 transcript），
SHALL NOT 构造 worktree、不产交付分支。resume SHALL 三级回退：`chat_session.session_id`（须与
任务 runtime 匹配）→ 本会话最近记录了 `session_id` 的任务行 → 全新会话。会话 `work_dir` 为
daemon 属主字段：缺失时 SHALL 按会话 id 在 daemon 工作区创建，终态 SHALL 将
`session_id`/`runtime_id`/`work_dir` pin 回会话（存在更新的任务时 SHALL NOT 回拖）。任务输入
读取失败 SHALL 保留任务等待重投，SHALL NOT 按空输入执行。执行输出 SHALL 以 `role='assistant'`
落 `chat_message` 并关联 `task_id`。

#### Scenario: 失败轮次不断记忆
- **WHEN** 某轮任务失败未记录 session_id，下一条消息触发新任务
- **THEN** resume 回退命中最近成功任务行的 session_id，对话记忆延续

#### Scenario: 输入读取失败不伪装空输入
- **WHEN** 认领后读取密封批次时数据库暂时不可用
- **THEN** 任务保留等待重投，SHALL NOT 以空输入执行后取消

#### Scenario: work_dir 跨轮稳定
- **WHEN** 会话第二轮任务认领
- **THEN** 复用首轮 pin 回的 work_dir；目录被删则重建

### Requirement: Chat 出站

chat 任务终态 SHALL 经 Outbox 回到触发会话：使用任务上冻结的 `reply_to`/`reply_in_thread` 调
reply 端点，仅限"目标不可回复且未发送"的明确业务码（撤回/话题消失等 6 个）时回退群级发送；
限流/5xx/超时 SHALL 保持失败走退避重试。空输出 SHALL 静默不回。消息受理 SHALL 以 Typing 表情
回执替代文本受理卡（回复/失败/取消时清除；表情 API 失败降级为 no-op）。

#### Scenario: 话题内终态回复
- **WHEN** 话题内触发的 chat 任务完成
- **THEN** 结果 reply 到批次末条消息并 `reply_in_thread=true`

#### Scenario: 触发消息被撤回
- **WHEN** 终态回复时目标消息已撤回（230011）
- **THEN** 回退群级发送，用户仍收到结果

#### Scenario: 限流不回退
- **WHEN** reply 端点返回限流错误
- **THEN** Outbox 按退避重试，SHALL NOT 回退群发造成重复

### Requirement: /issue 命令

会话中 `/issue <标题>`（仅首个非空行、token 边界；裸命令 SHALL 回 usage 且 SHALL NOT 从历史推断
标题）SHALL 创建 Issue 并写 `issue_origin`（external_message_id=命令消息，thread_id=所在话题），
负责人取会话绑定 Agent；命令消息以 `message_kind='channel_command'` 落库但 SHALL NOT 触发
chat run、SHALL NOT 进入后续 Agent 输入。系统 SHALL 同步回执"已创建 `<issue_id> — 标题`"+ 深链。
该 Issue 的终态通知 SHALL 沿 `issue_origin` reply 回原话题。

#### Scenario: 建单不触发 chat run
- **WHEN** 用户发送 `/issue 修复导出异常`
- **THEN** Issue 与 issue 任务创建，无 chat 任务产生；收到回执与深链

#### Scenario: 命令不污染 transcript
- **WHEN** `/issue` 之后的下一条普通消息触发 chat 任务
- **THEN** 该任务的输入批次与 transcript 均不含命令消息

#### Scenario: chat 建单的通知回程
- **WHEN** 由 `/issue` 创建的 Issue 执行完成
- **THEN** 终态通知 reply 到命令所在话题（经 issue_origin）
