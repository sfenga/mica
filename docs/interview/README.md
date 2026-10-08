# Mica 面试讲解手册

这组文档用于解释简历中四个核心技术点背后的业务问题、方案演进和当前代码实现。它不是接口说明，而是帮助面试者回答三个问题：为什么要做、为什么这样做、遇到什么问题后才迭代成现在的方案。

## 阅读顺序

面试前快速练习可先读 [简历四点口述与追问引导](./07-resume-storytelling.md)：每点包含业务场景、约一分钟讲稿、实现路线、埋点及回答边界。下面六篇用于继续深挖。

1. [多渠道任务触发与合并](./01-multi-channel-trigger-and-merge.md)
2. [本地 Agent 调度与隔离执行](./02-local-agent-runtime-and-isolation.md)
3. [异构 Agent 接入：CLI、ACP 与 A2A](./03-heterogeneous-agent-integration.md)
4. [Squad 编排与飞书协作](./04-squad-and-feishu.md)
5. [高频追问、方案取舍与当前边界](./05-interview-questions-and-boundaries.md)
6. [多 Agent 上下文管理](./06-multi-agent-context-management.md)

## 统一业务背景

企业内部已经存在两类 Agent：一类是开发者电脑上的 Codex、Claude Code、Pi 等本地 Coding Agent，能够读取或修改本地项目；另一类是 Geometry、Placement、Consultation 等远程业务 Agent，接收自然语言并返回文本或结构化 JSON。用户既可能在 Web 控制台创建任务，也可能在飞书群聊里发起任务或继续补充要求。

如果只是给每种 Agent 写一个调用接口，会很快遇到四类问题：

- 同一用户连续发送多条补充要求，可能重复启动昂贵的 Agent 进程。
- 本地 CLI 占用 CPU、内存和工作目录，多个任务并发时可能互相覆盖代码。
- CLI、ACP、A2A 的会话、事件、取消和返回结果完全不同，上层业务容易被协议细节污染。
- 多 Agent 协作如果简单广播给所有成员，既浪费调用，又没有明确的拆分和汇总责任。

Mica 因而被设计为一个任务控制平面：渠道负责接收用户输入，Task 负责记录一次执行，Daemon 负责调度本地资源，Provider 屏蔽 Agent 调用差异，Squad 负责多 Agent 协作，PostgreSQL 保存任务状态和并发约束。

## 文档中的事实标记

- “当前实现”来自 `mica/` 正式包、SQL migration 和自动化测试。
- “演进假设”是为了面试叙事补出的合理早期版本，不代表仓库中仍保留对应代码。
- “后续可演进”是设计方向，不能当作当前已经上线的能力。

## 模型更新日志

手册保留历史叙述，模型变化以追加方式记录：

- **2026-10-08：Worktree 改为一次性副本 + 分支交付**（对齐 Multica，解决 KI-004）。追加位置：[02](./02-local-agent-runtime-and-isolation.md) 第 5 节末尾、[05](./05-interview-questions-and-boundaries.md) 第 9 节末尾、[07](./07-resume-storytelling.md) 第二点追问表后。各追加小节标注了哪些旧口径仅描述对齐前的版本；旧段落原样保留供对照。

面试时可以讲演进假设，但建议使用“最开始我们考虑/验证过……”而不是“线上曾经发生过……”，避免把无法证明的生产事故说成事实。

## 当前最稳妥的四点简历表述

- **多渠道任务触发与合并**：面向用户在 Web、飞书中连续补充要求的场景，统一任务入口并按显式指派、@提及和回复关系路由 Agent；利用 PostgreSQL 部分唯一索引与 UPSERT 合并同一事项的待执行消息，在领取时固化有序输入快照，避免重复启动并保证新增要求不会混入正在执行的上下文。
- **本地 Agent 调度与隔离执行**：通过本地 Daemon 调度 Codex、Claude Code、Pi 等 Coding Agent，先预留 Runtime Slot 再领取任务，并结合执行租约、心跳和进程组取消恢复异常执行；代码修改任务可使用独立 Git Worktree，同一 Issue 与 Agent 的后续任务复用分支和会话继续处理。
- **异构 Agent 接入**：定义统一 Agent Provider 契约，通过 CLI Adapter 和 ACP 驱动本地 Coding Agent，通过 A2A Client 调用远程业务 Agent；将流式文本、工具调用、权限决策、状态和结构化结果转换为统一 TaskEvent，并复用任务追踪、取消、等待补充和结果校验流程。
- **Squad 与飞书协作**：支持在 Web 端按角色和能力组装包含本地、远程 Agent 的 Squad，并从 Web 或飞书指派任务；采用 Leader-first 编排，由 Leader 输出结构化委派决策，平台校验成员范围、调度成员任务并在本批成员均结束或等待输入后唤醒 Leader 汇总，最终将受理、补充信息请求和整体结果可靠回传原飞书会话。

最后一条特意写成“受理、补充信息请求和整体结果”，因为当前 Web 可以展示 TaskEvent 时间线，而飞书只投递受理、完成、失败、等待输入等关键消息，尚未同步每一条流式进度。
