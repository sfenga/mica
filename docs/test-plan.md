# Mica 真实环境验收清单

自动化测试负责验证状态机、并发契约和协议解析；以下项目必须在真实 PostgreSQL、真实 Agent CLI 或可控的协议测试服务上验收。完成后在对应条目记录日期、环境和证据链接。

## 阶段 2：PostgreSQL 核心

- [ ] API 与 Daemon 使用相同 PostgreSQL、以两个独立进程运行。
- [ ] API 启动自动执行 migration；两个进程同时启动不会重复执行 migration。
- [ ] 创建 Issue 时，Issue、首个 Task 和 `task.queued` 事件在同一事务内提交。
- [ ] 两个 Daemon 并发领取时，同一个 Task 只会被一个 Daemon 获得。
- [ ] 同一 Issue/Agent 的多个 Task 串行执行，不同 Issue 可以并行执行。
- [x] 同一 Issue/Agent 的连续评论合并为一个 queued Task；已有 Task 执行时最多产生一个 queued 后续 Task。
- [x] Daemon 领取 Task 时按消息顺序固化输入快照，领取后到达的评论不会混入当前执行。
- [ ] API 重启后 Issue、Task、执行结果和事件仍可查询。
- [ ] WebSocket 只收到已提交事件，不会收到事务回滚产生的事件。

## 阶段 3：本地 Agent 闭环

- [ ] Codex 和 Claude Code 分别完成一次真实代码修改并产生流式事件。
- [ ] 原仓库保持不变，代码修改只出现在 `(Issue, Agent)` 对应的分支和 Worktree。
- [ ] 对同一 Issue 追问时复用原分支、Worktree 和上一次 Agent Session。
- [ ] `in_place` 项目直接使用当前目录，且同一路径的多个任务即使 Runtime 有空闲 Slot 也不会并发执行。
- [ ] 未关联项目的 Codex、Claude Code、Pi、ACP 任务在 `MICA_WORKSPACE_ROOT` 下的隔离空目录运行。
- [ ] Runtime A 的 Daemon 不领取绑定到 Runtime B 的 Agent Task。
- [ ] `MICA_MAX_SLOTS=N` 时最多同时运行 N 个本地 Agent 进程，Task 在获得 Slot 后才被领取。
- [ ] 取消运行中 Task 会终止整个进程组、释放 Slot，并产生取消事件。
- [ ] Daemon 正常退出后，子进程终止；租约到期后由新 Daemon 以新 attempt 恢复任务。
- [ ] 旧 attempt 的晚到结果无法覆盖新 attempt 的状态和输出。
- [ ] 脏仓库、无效 Git 路径、CLI 未安装和 CLI 未登录时返回可定位的失败原因。

## 阶段 4：ACP 与 A2A

- [ ] ACP Agent 完成 `initialize → session/new|session/load → session/prompt`，并将 `session/update` 转为 TaskEvent。
- [ ] ACP 权限请求按 Agent 配置执行允许或拒绝策略，决策过程产生审计事件。
- [ ] ACP Task 取消时发送 `session/cancel` 并终止失去响应的子进程。
- [ ] A2A Agent Card 能发现协议版本、REST endpoint、流式能力和技能。
- [ ] A2A 请求携带 `A2A-Version: 1.0`，认证令牌只从环境变量读取。
- [ ] A2A 同步响应和异步 Task 轮询都能转换为统一 TaskEvent。
- [ ] A2A 追问复用 `contextId`；远端要求补充输入时，本地 Task 进入 `waiting_input`、Issue 进入 `blocked`，用户补充后复用原 `taskId`。
- [ ] A2A 取消调用远端取消接口，本地 Task 最终进入 cancelled。
- [ ] Geometry、Placement 等业务 Agent 的 JSON 结果通过 Agent 配置的 JSON Schema 校验；不合格结果阻止 Task 完成。
- [ ] 429、5xx、超时、协议错误和业务 Schema 错误产生可区分的失败信息。

## 阶段 5：Web 与飞书入口

- [ ] Web 控制台可选择本地或远程 Agent 创建任务，刷新后仍能查看 Issue、Task、状态和输出。
- [ ] Web 使用同一个 `Idempotency-Key` 重试时只创建一个 Issue 和一个首 Task；相同 Key 携带不同内容返回 409。
- [ ] 飞书 URL 校验、签名事件和加密事件均能通过；错误 token、错误签名和超过时钟窗口的请求被拒绝。
- [ ] 飞书重复推送同一个 `event_id` 时，只创建一个 Issue/Task，并只登记一次受理回复。
- [x] 飞书线程回复能够命中原始消息对应的 Issue，并以评论触发后续执行而不是创建第二个 Issue。
- [ ] 同一飞书用户的不同消息映射到同一平台用户；不同租户的相同 `open_id` 不会串身份或 Workspace。
- [ ] 默认 Agent 和 `agent=<id>` 显式指派均能工作；本地 Codex/Claude 与远程 A2A Agent 各完成一次任务。
- [ ] 飞书回调在任务入库后快速返回，发送消息失败不会回滚任务；Outbox 重试后最终回传原会话。
- [ ] 永久投递失败达到 `MICA_CHANNEL_MAX_ATTEMPTS` 后进入 `failed`，不会无限占用投递循环。
- [ ] API 或投递线程在消息发送前后重启时，Outbox 租约能够恢复，飞书消息 UUID 防止重复消息。
- [ ] Agent 完成、失败、取消、等待补充信息四类结果都能回传原群聊或单聊，长输出会被截断。

## 阶段 6：Agent Squad 编排

- [ ] Web 创建 Squad 后可选择 Leader 和多个成员，刷新后 roster、角色、能力与协作规则仍可查询。
- [ ] Issue 指派给 Squad 时只生成一个 Leader Task，不会直接 fan-out 全体成员。
- [ ] Leader Task 的 `squad_id`、`is_leader_task` 正确，并在每次 Leader 唤醒时注入当前 roster 与 Squad instructions。
- [ ] Leader 的 `delegate` JSON 只允许指派 roster 内的非 Leader Agent；非 JSON、空指令、重复成员和越权成员均 fail-closed。
- [ ] 同一 Squad 可混合本地 Codex/Claude、ACP 与远程 A2A Agent，成员分别收到 Leader 的 handoff 指令。
- [ ] 多个成员 Task 可按 Runtime Slot 并行执行；只完成其中一个时不唤醒 Leader，整批完成后只生成一个汇总 Task。
- [ ] 成员失败或取消不会直接取消父 Issue，而是作为结果交给 Leader 决定重派、降级或收口。
- [ ] 成员要求补充输入时不会卡死批次；Leader 能看到 `waiting_input` 结果并决定向用户追问或续派。
- [ ] Leader 或成员执行期间新增的用户评论不会丢失，后续 Leader Task 在成员批次结束后处理评论并读取成员结果。
- [ ] Leader 委派后父 Issue 保持 `in_progress`；只有 Leader 汇总决定 `complete` 后进入 `in_review`，用户确认后进入 `done`。
- [ ] Web 和飞书均可将新 Issue 指派给 Squad，飞书最终只收到整体完成结果而不是每个内部 Task 的误报。

## 验收记录模板

```text
日期：
提交：
环境：macOS / PostgreSQL / Agent 及版本
场景：
结果：通过 / 失败
证据：Task ID、事件截图、SQL、日志或录屏
备注：
```
