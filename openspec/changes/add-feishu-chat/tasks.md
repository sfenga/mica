# Tasks: add-feishu-chat

> 实现清单。apply 阶段照此勾选。单提交交付：§1–§9 全部完成后一次性提交（代码+迁移+测试+文档）。

## 1. 迁移 012
- [x] 1.1 `012_chat.up.sql`：建 `chat_session`、`channel_chat_binding`、`chat_message`（含全部索引与约束）
- [x] 1.2 `012_chat.up.sql`：`agent_task` 放开 `issue_id` NOT NULL，新增 `chat_session_id`/`chat_input_task_id`/`reply_to_message_id`/`reply_in_thread` + 索引
- [x] 1.3 `012_chat.down.sql`：按依赖逆序回滚

## 2. 领域模型
- [x] 2.1 `ChatSession`/`ChannelChatBinding`/`ChatMessage` dataclass（`mica/domain/models.py`）+ 导出
- [x] 2.2 `Task`：`issue_id` 改 Optional，加 `chat_session_id`/`chat_input_task_id`/`reply_to_message_id`/`reply_in_thread`；修正全部直接构造处的涟漪

## 3. 仓储层
- [x] 3.1 `chat_repository.py`（PG）：ensure_session（唯一冲突 re-read 协议）、append+去重同事务、seal、transcript/输入批次读取、pin 回写（无更新任务守卫）、未认领消息扫描
- [x] 3.2 内存实现（bootstrap 双容器用），行为与 PG 版一致
- [x] 3.3 `repositories.py` `claim_next`：`LEFT JOIN issue`、三分支互斥、chat 的 active 守卫；终态写回 SQL 加 chat 分支
- [x] 3.4 PostgreSQL 集成测试：chat 仓储契约、双任务类型并发认领（真实双连接交错，参照 KI-009 测试模式）

## 4. 应用层
- [x] 4.1 `application/chat.py` `ChatIngressService`：@bot 判定入参、隔离键、ensure+append、首条 `agent=` 绑定、后续 `agent=`/`squad=` 剥离+引导判定、无效 agent fail-closed、群 creator 系统用户解析
- [x] 4.2 `application/chat.py` `ChatBatcher`：3s 窗口/重挂/flush（密封+冻结 reply_to+解析 runtime+建任务）、`Drain()`、启动恢复
- [x] 4.3 `/issue` 命令：解析（首非空行、token 边界、裸 usage）、`channel_command` 落库不触发 run、`create_issue`+`issue_origin`、回执文本+深链
- [x] 4.4 `channels.py`：`IngressService` 删两个飞书 outbox 分支；`TaskNotificationProjector` chat 分支（用任务冻结 reply_to）
- [x] 4.5 `services.py`：`enqueue_chat_task`（session 行锁）、终态写回 chat 分支（pin+assistant 落库）、`resolve_chat_resume` 三级回退

## 5. Runtime
- [x] 5.1 `daemon.py` `run_once` chat 分叉：取 chat_session、resume 回退、workdir 建目录、无 worktree/finalize、输入读取失败保留重投、终态 chat 写回
- [x] 5.2 `workspace.py`：session 维度 prepare；`work_dir` 消失自愈
- [x] 5.3 `providers/`（base/cli/a2a/acp/pi/stub）：接受 daemon 拼好的 chat prompt 负载；CLI 沿用 session resume；stub 供测试

## 6. 集成层
- [x] 6.1 `feishu.py` `_on_message` 重写：@bot 防御、隔离键、调 ChatIngressService、batcher 调度；删除 parent/root `find_origin` 逻辑
- [x] 6.2 表情回执客户端（加/删 Typing），失败降级 no-op + warning
- [x] 6.3 `/issue` 解析接入；`parse_feishu_target` 复用

## 7. 装配与生命周期
- [x] 7.1 `bootstrap.py`：装配 ChatIngressService/ChatBatcher/chat 仓储（内存+PG 双容器）、系统用户解析
- [x] 7.2 API 优雅停机时 `batcher.Drain()`
- [x] 7.3 `mica/web` 与相关任务列表查询核对：必要处加 `issue_id IS NOT NULL` 过滤

## 8. 测试
- [x] 8.1 `tests/test_chat_ingress.py`：路由/绑定/去重/首条绑定/引导/@bot/媒体占位/post 拍平
- [x] 8.2 `tests/test_chat_batcher.py`：窗口/密封/冻结 reply_to/Drain/启动恢复/在跑时消息不建任务
- [x] 8.3 `tests/test_chat_daemon.py`：认领互斥（双 daemon）、resume 三级回退、workdir pin 回、输入读取失败保留、assistant 落库、issue 状态不受 chat 任务驱动
- [x] 8.4 `tests/test_chat_issue_command.py`：/issue 全场景（成功/裸命令/重复语义/不触发 chat run/通知回程）
- [x] 8.5 全量回归：309+ 通过、`MICA_TEST_DATABASE_URL` 集成套件通过

## 9. 文档（追加式约定见 proposal）
- [x] 9.1 `docs/known-issues.md` KI-001：状态→已设计、待决策项划线结案、追加「定稿方案（2026-10-09）」（含验收修订小节）；KI-008 追加交叉注记（issue_origin 保留出站锚点）
- [x] 9.2 `docs/foundations/postgresql-core.md`：追加 migration 012 章节
- [x] 9.3 `docs/interview/01、04、07`：按带日期追加格式补 chat 行为变化与边界
- [x] 9.4 `README.md`：飞书段落改 chat 用法、行为变更声明、表情权限前置、单 API 实例约束
- [x] 9.5 `docs/architecture.md`：入口图一行更新 + 追加 chat 模块一节；`docs/test-plan.md`：追加 chat 测试章节
- [x] 9.6 `.env.example`：注释备注表情权限（无新增必填项）

## 10. 交付
- [x] 10.1 单提交：`feat(chat): ...` 全量（代码+迁移+测试+文档）；提交信息含 Tests 行
