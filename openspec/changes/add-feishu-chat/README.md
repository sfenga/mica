# add-feishu-chat

KI-001 定稿落地：飞书消息与 Issue 解耦为 Chat 模型。新建 ChatSession/ChatMessage/channel_chat_binding
（隔离键 `chat_id` 与 `chat_id:thread_id`，话题即会话），会话绑单 Agent 不可变，3 秒静默窗口只防抖
任务创建，chat 任务与 issue 任务共表 `agent_task` 并在认领 SQL 与 daemon 主循环分叉，
daemon 属主三元组（session_id/runtime_id/work_dir）终态 pin 回，出站复用 KI-008 reply 机制 +
Typing 表情回执，`/issue` 命令建单（channel_command 不触发 chat run）。对齐 Multica chat 骨架并
按单机单 bot 现实裁剪；硬切、无历史兼容。新建 capability=mica-chat，修改 mica-core 两条 Requirement。
