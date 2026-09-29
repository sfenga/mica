# 异构 Agent 接入：CLI、ACP 与 A2A

## 1. 业务需求

企业内部 Agent 并不是同一种服务：

- Codex、Claude Code、Pi 安装在开发者机器上，以 CLI 方式运行。
- 某些 Coding Agent 实现 ACP，通过 JSON-RPC/stdio 提供标准 Session 和流式更新。
- Geometry、Placement、Consultation 等业务 Agent 已作为远程服务存在，通过 A2A 暴露 Agent Card、消息和远程 Task。

上层任务系统不应该为每种 Agent 分别实现一套状态机、取消接口和前端时间线。它需要一个稳定边界：无论底层是进程还是 HTTP，请求都接收相同的 Issue/Task 上下文，输出统一的执行结果和事件。

“统一”不代表把所有协议压缩成纯文本。平台统一的是生命周期和事件外壳，同时允许 `agent.permission`、`agent.data`、`agent.protocol` 等事件保留协议特有语义。

### 与 Multica 的关系

Mica 参考了 Multica 的 Agent 抽象、本地 CLI/ACP 适配和 Claude 流式处理经验，但两者并非同一套协议层：

- Multica 的核心过程记录是 `agent.Message`、持久化 `task_message` 与内部事件总线。
- Mica 在 Provider 出口统一生成持久化 `TaskEvent`，再供 Web 时间线和通知投影消费。
- 标准 A2A Agent Card、HTTP+JSON 调用、远端 Task 轮询和 JSON Schema 校验是 Mica 为企业业务 Agent 增加的能力，不是 Multica 当前实现。

## 2. 演进过程

### V0：只支持 Stub 或一个固定 CLI

合理的第一版通常把 Agent 调用写在 Daemon 里：拼接命令、读取 stdout、把最后一行当结果。这能验证任务链路，但一旦加入第二个 CLI，Daemon 就会出现大量 provider-specific 分支；加入远程 Agent 后，轮询、认证和状态映射会进一步侵入任务状态机。

### V1：抽象统一 Provider 接口

当前 `AgentProvider` 只要求一个 `run()`：

```python
run(
    agent,
    issue,
    task,
    on_event,
    workdir=None,
    resume_session_id=None,
    is_cancelled=None,
) -> ProviderRunResult
```

其中：

- `on_event(type, payload)` 输出过程事件。
- `resume_session_id` 传递上一轮协议会话。
- `is_cancelled` 把平台取消状态暴露给适配器。
- `ProviderRunResult` 统一返回 output、session_id，以及是否等待用户输入。

Daemon 只通过 `ProviderRegistry` 找实现，不判断 CLI、ACP 或 A2A 的协议细节。

### V2：CLI 能执行，但只收集最终 stdout

只读取最终输出会让十几分钟的任务看起来一直没有响应，也无法解释 Agent 在调用什么工具。Codex 和 Claude 又使用不同的 JSONL 事件结构，所以当前 `CliProvider` 增加流式解析并转换为统一事件。

随后实际运行暴露出两个问题：

- Claude 的 hook、thinking token 事件数量巨大，不应原样展示。
- 无人值守 Daemon 没有终端 UI，Claude 的权限请求会卡住或返回 `permission_denied`。

因此 Claude 适配器进一步改为双向 stream-json：保持 stdin 打开，接收 `control_request` 后返回 allow，并强制 `run_in_background=false`；同时过滤内部遥测，禁用没有平台 UI 承接的 `AskUserQuestion`。

### V3：引入 ACP，验证标准本地协议

CLI Adapter 解决了 Codex、Claude Code 的接入问题，但继续增加本地 Agent 时，成本已经不只是多写一个输出解析器。不同 CLI 对启动参数、Session 恢复、流式事件、工具权限和取消分别采用私有约定；这些差异一旦进入 Daemon，新增 Agent 或 CLI 升级都会影响任务状态、事件记录和前端展示。

因此 Mica 引入 ACP（Agent Client Protocol）v1，验证标准化的本地 Agent 接入方式。ACP 可以类比 Coding Agent 领域的 LSP：Mica 作为 Client 管理会话入口和执行边界，Agent 保留自己的模型、规划和工具实现；双方通过 JSON-RPC/stdio 交换标准方法，而不是依赖某个 CLI 的私有 JSONL。

当前 ACP Adapter 负责完整的一轮托管会话：

1. 发送 `initialize`，协商协议版本和 Agent 能力。
2. 通过 `session/new` 创建会话，或在 Agent 声明支持后通过 `session/load` 恢复历史会话，同时传入绝对工作目录和 MCP Servers。
3. 通过 `session/prompt` 提交 Issue/Task 上下文，将 `session/update` 中的文本、思考、计划、工具调用和用量转换为 Mica 的统一 TaskEvent。
4. 接收 `session/request_permission`，根据 Agent 配置的权限策略选择允许或拒绝，并单独记录权限事件。
5. Task 取消、Daemon 关闭或租约丢失时发送 `session/cancel`，最终终止本地进程组，确保 Agent 生命周期不脱离 Task。

这使后续兼容 ACP 的本地 Agent 可以复用同一个会话、权限、事件和取消适配器。不过 ACP 统一的是通信契约，并不负责部署：Mica 仍需配置 Agent 的启动命令、工作目录、MCP Servers、权限策略和超时时间。当前实现仅支持 ACP v1 的 stdio 子集，不声称已经支持远程 ACP Transport 或全部扩展方法。

### V4：引入 A2A，支持远程业务 Agent

ACP 解决的是“如何把一个 Agent 作为受 Mica 托管的交互式执行端接入平台”，但 Geometry、Placement、Consultation 等企业业务 Agent 已经作为独立服务部署：它们自行管理模型、内部工具、权限和计算资源，Mica 只应发现其能力、委派任务并接收结果，而不是管理其内部进程和工具授权。

这类调用还具有不同于本地 Coding Agent 的生命周期：简单问题可能直接返回 Message，复杂任务可能返回可持续数分钟的远程 Task；任务可以经历 `WORKING`、`INPUT_REQUIRED`、`AUTH_REQUIRED`、`COMPLETED` 或 `FAILED`，客户端即使暂时断开，也应能使用远端 Task ID 继续查询。最终结果也不一定是文本，还可能是带有 JSON、文件或其他数据的 Artifact。

因此 Mica 在统一 Provider 接口下增加 A2A Client，把远程业务 Agent 视为拥有独立生命周期的服务：

1. 读取 Agent Card，校验服务地址、协议版本和配置的业务 Skill，并选择 A2A 1.0 HTTP+JSON 接口。
2. 携带认证信息提交 Message；首次调用创建远端上下文，后续调用使用 `contextId` 延续业务会话，在需要补充输入时使用原 `taskId` 继续同一远程任务。
3. 对直接返回的 Message 立即提取结果；对远程 Task 定期查询状态，将进度、状态和 Artifact 转换为统一 TaskEvent。
4. 遇到 `INPUT_REQUIRED` 或 `AUTH_REQUIRED` 时，把本地 Task 转为等待输入；用户取消时调用远端取消接口，而不是只停止本地轮询。
5. 对 Data Part 或 Artifact 中的结构化输出执行 JSON Schema 校验，避免业务 Agent 返回字段缺失或类型错误的结果进入后续流程。

ACP 未来也可以使用远程传输，但协议是否经过网络并不是 Mica 选择 A2A 的根本标准：由 Mica 管理会话、工作目录和工具权限的交互式 Agent 适合 ACP；拥有独立能力描述、任务状态和结果交付边界的业务服务适合 A2A。两类 Adapter 最终都实现相同的 Provider 契约，因此 Daemon、Task 状态机、Web 时间线和飞书结果回传仍能复用。

## 3. Provider 能力对照

| Provider | 运行位置 | 传输 | 会话续接 | 过程事件 | 取消 | 结构化结果 |
|---|---|---|---|---|---|---|
| Codex CLI | 本地 | JSONL/stdout | thread ID | 文本、Item/工具 | 终止进程组 | 主要为文本 |
| Claude CLI | 本地 | 双向 stream-json | session ID | 文本、工具、系统事件 | 终止进程组 | 主要为文本 |
| Pi | 本地 | CLI Adapter | Provider 自身语义 | 统一事件 | 本地进程取消 | 主要为文本 |
| ACP | 本地 | JSON-RPC/stdio | session/new、session/load | update、plan、tool、usage | session/cancel + 进程终止 | 当前汇总文本 |
| A2A | 远程 | HTTP+JSON 1.0 | contextId、taskId | 状态、文本、data、artifact | 远端 task cancel | JSON Schema 可校验 |

## 4. CLI Adapter 当前实现

### Codex

Codex 使用 `exec --json`，首次任务创建线程，后续任务通过 `exec resume --json` 续接。非续接的代码执行启用 `workspace-write` sandbox。事件中的 thread、agent message 和其他 item 分别转换为 system、text 和 tool 事件。

### Claude Code

Claude 使用：

```text
--print --verbose
--output-format stream-json
--input-format stream-json
--permission-mode bypassPermissions
--disallowedTools AskUserQuestion
```

平台通过 stdin 发送 `user` frame，并在同一输入流上回复工具 `control_request`。这是无人值守运行的技术要求，不是面向普通用户的权限选择项。为了让 Daemon 掌握生命周期，后台工具请求被改成前台；如果仍检测到异步任务已启动，本次执行失败。

CLI 输出统一为：

- `agent.text`：Agent 文本。
- `agent.tool`：工具名和输入。
- `agent.system`：Session 初始化等可展示状态。
- `agent.error`：Provider 明确返回的错误。

当前实现还会合并连续 Claude 文本块，并在缺少 result event 时谨慎使用最后一个顶层文本作为结果；如果最后发生过工具调用却没有最终回复，则不会把工具前的解释误判为完成。

## 5. ACP Adapter 当前实现

ACP 的调用顺序为：

```text
initialize(protocolVersion=1)
    │
    ├─ 新会话：session/new(cwd, mcpServers)
    └─ 续接：session/load(sessionId, cwd, mcpServers)
            │
            ▼
session/prompt(prompt=[text])
            │
            ├─ session/update → TaskEvent
            ├─ session/request_permission → 平台策略决策
            └─ stopReason → ProviderRunResult
```

上图描述的是 Mica 当前使用的 ACP v1 会话流程。Multica 会根据 Agent 能力选择 `session/resume` 或 `session/load` 等续接方式，不能把 Mica 固定使用 `session/load` 的顺序概括成 ACP 接入的唯一实现。

权限策略默认 `deny`。只有 Agent 明确配置 `permission_policy=allow` 时，Adapter 才从服务端提供的选项中优先选择 `allow_once`，其次 `allow_always`。决策会记录成 `agent.permission`，因此权限不是静默发生。

Multica 的权限选择更保守：优先使用当前 Session 已存在的安全授权，其次选择 `allow_once`，没有合适授权时选择 `reject_once`，并明确不接受永久 `allow_always`。因此这里应表述为“Mica 当前的可配置权限策略”；如果要进一步向 Multica 对齐，应移除自动选择 `allow_always` 的回退，并补充用户审批或 Session 级授权来源。

取消时先发送 `session/cancel`，退出时再清理整个本地进程组。未知的带 ID 客户端方法会返回 JSON-RPC method not supported，而不是让请求无限等待。

## 6. A2A Client 当前实现

### 6.1 能力发现与地址校验

Agent 配置保存 `agent_card_url`，运行时读取 Agent Card：

1. 从 `supportedInterfaces` 选择 `protocolBinding=HTTP+JSON` 且版本为 1.0 的地址。
2. 校验配置的 `skill_id` 是否存在于 Card。
3. 生产地址要求 HTTPS；跨域 endpoint 必须显式允许。
4. Token 不直接持久化，只保存 `token_env`，运行时从环境变量读取。

这比在 Agent 配置中直接保存 bearer token 更适合后续接入 Secret Manager。

### 6.2 同步 Message 与异步 Task

调用 `/message:send` 后可能得到：

- 直接 Message：提取 parts 后立即完成。
- 远端 Task：轮询 `/tasks/{id}` 直到终态。

轮询中把状态转换成 `agent.status`，把 text、data 和未知 part 分别转换成 `agent.text`、`agent.data` 和 `agent.artifact`。通过内容指纹去重，避免轮询重复展示同一 Artifact。

### 6.3 会话续接和等待补充

本地 session token 使用 `a2a1:` 前缀编码：

```json
{
  "context_id": "remote-context",
  "task_id": "remote-task",
  "state": "TASK_STATE_INPUT_REQUIRED"
}
```

普通追问只复用 `contextId`；当上一轮是 `INPUT_REQUIRED` 或 `AUTH_REQUIRED` 时，同时携带原 `taskId`，使补充信息继续送到原远端任务，而不是创建一条无关任务。

远端需要输入时，Provider 返回 `requires_input=True`，上层将本地 Task 置为 `waiting_input`、Issue 置为 `blocked`，但不把它当成执行异常。用户补充后再创建后续 Task 并复用远端上下文。

### 6.4 结构化结果校验

远程业务 Agent 可配置 `output_schema`。如果响应是 data part，直接校验结构；如果是字符串，先解析 JSON。只有通过 JSON Schema 后才能完成本地 Task，否则报告精确到字段路径的校验错误。

例如 Placement Agent 声称返回：

```json
{"layoutId":"L-1","valid":true}
```

平台可以要求 `layoutId` 必须为字符串、`valid` 必须为布尔值，并禁止额外字段。这样“远端请求 HTTP 200”不等于“业务结果可用”。

## 7. 为什么统一事件，而不是统一底层协议

让所有 Agent 都改造为同一个协议在企业环境中不现实：已有 Agent 的发布周期、语言和维护团队不同。Mica 选择在边界处适配：

```text
CLI JSONL ─┐
ACP RPC ───┼─ Provider Adapter ─ TaskEvent ─ Web / 审计 / 飞书投影
A2A HTTP ──┘
```

这样任务追踪和前端不需要理解 Claude 的 `assistant`、ACP 的 `session/update` 或 A2A 的 `TASK_STATE_WORKING`。同时事件 payload 保留 `protocol`、tool、data 等字段，需要协议细节的诊断页面仍然可以使用。

`TaskEvent` 是 Mica 的统一持久化边界。Multica 达到相似目标时使用的是 Agent Message、`task_message` 和事件总线组合；两者概念相近，但表结构和投影链路不同。

## 8. 当前边界

- A2A Card 即使声明 streaming，当前 Client 仍使用同步 HTTP 和轮询，没有实现 SSE 流。
- A2A 每次执行都会读取 Agent Card，尚未实现带过期时间的缓存。
- Token 仅支持环境变量引用，尚未接入企业 Secret Manager 和 token refresh。
- ACP 当前实现稳定的 v1，不声称支持仍在变化的 ACP v2 草案。
- CLI 仍需要分别维护 Codex、Claude 的事件解析器；只有实现 ACP 的本地 Agent 才能完全复用 ACP Adapter。
- 统一 TaskEvent 目前没有正式版本号和 schema registry，协议升级依赖代码兼容和测试。
- Claude 自动授权适用于可信本机执行环境；系统级沙箱和企业权限审批仍是后续安全建设。

## 9. 面试时的 90 秒讲法

“项目里既有本地 Coding Agent，也有远程业务 Agent。最开始如果在 Daemon 里直接按类型分支，任务状态、取消和前端会被各种协议污染，所以我定义了统一 Provider 契约：输入 Issue/Task、事件回调、历史 Session 和取消检查，输出统一结果。Codex、Claude 通过 CLI Adapter 转换 JSONL，其中 Claude 使用双向 stream-json 处理无人值守权限；支持 ACP 的本地 Agent 走 JSON-RPC Session 协议，权限决策单独形成审计事件。远程业务 Agent 则通过 Mica 扩展的 A2A Provider 读取 Agent Card、轮询远端 Task，并用 JSON Schema 校验结构化结果。所有实现最后都产生 Mica 的统一 TaskEvent，所以上层可以复用追踪、取消、等待输入和结果回传；Multica 提供了本地 Agent 适配思路，但 A2A 与这套 TaskEvent 边界是本项目自己的扩展。”
