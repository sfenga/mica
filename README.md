# mica

Mica 是面向企业内部场景的多智能体任务协作平台：用户可以从 Web 等入口提交事项，将任务指派给本地 Coding Agent、远程业务 Agent 或 Agent Squad，并持续查看执行过程与结果。

`mica/` 是可独立安装的正式后端包，包含领域服务、PostgreSQL 仓储、API、Runtime Daemon、Agent Provider、实时事件与渠道集成。`sessions/zh/` 仅保留历史原型，不参与正式包构建和运行。

> 当前版本面向可信本机环境，API 默认监听 `127.0.0.1`。接入企业 SSO 和 Workspace 权限校验前，不应暴露到局域网或公网。

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# 启动 PostgreSQL；应用启动时自动执行版本化迁移
docker compose up -d postgres

# 分别启动 API 和本地 Runtime Daemon
python -m mica.main
python -m mica.daemon_main

# Web 控制台
# 浏览器访问 http://127.0.0.1:8000/app
# 就绪检查（同时验证数据库、Runtime 与 Provider 配置）
curl http://127.0.0.1:8000/health/ready

# 扫描并注册本机 Git 项目（Web 控制台会在首次选择时自动完成这一步）
curl http://127.0.0.1:8000/api/project-candidates
curl -X POST http://127.0.0.1:8000/api/projects \
  -H 'content-type: application/json' \
  -d '{"name":"orders-service","local_path":"/absolute/path/to/repository","workspace_id":"local","runtime_id":"local"}'

# 创建代码事项时引用 project_id，不再逐次传递本机路径
curl -X POST http://127.0.0.1:8000/api/issues \
  -H 'content-type: application/json' \
  -d '{"title":"修复订单导出异常","body":"定位并修复批量导出中断","workspace_id":"local","project_id":"project_xxx"}'

# 测试
python -m pytest -q
```

正式 Daemon 默认使用 Codex，也可以通过 `MICA_DEFAULT_PROVIDER=claude` 切换到 Claude Code；对应 CLI 需要已安装并登录。自动化测试使用 Stub 或伪 CLI，不会调用真实模型。

### 代码任务的 Worktree 交付

关联 Git 项目的 Issue 默认以 `worktree` 模式执行（项目也可显式选择 `in_place` 直接操作当前目录）。每个任务在 Daemon 管理的目录里获得一份一次性 Worktree 副本，执行结束后副本即回收；**唯一持久成果是落在用户仓库里的分支** `mica/<issue>/<agent>`：

- 用户目录里的未提交修改会作为快照注入副本（上限 2000 个文件 / 200MiB，gitignore 内容自动排除），Agent 看到的就是用户眼前的代码状态；用户自己的目录全程不被写入。
- 同一 Issue/Agent 的追问续接同一分支与 CLI 会话（副本新建、会话可跨目录 resume）；用户把分支合入主干后，下一轮自动从最新 HEAD 重新出发。
- 任务结束时残留改动自动提交进分支，结果评论附上分支名与 tip commit；其他 Agent 或用户可用 `git diff HEAD...<branch>` 直接审核本轮改动，无需检出。
- 两轮之间用户改了 Agent 改过的文件会产生真实合并冲突：冲突文件列表写进下一轮 prompt，Agent 未解决前平台拒绝交付并保留现场。
- 只读任务不留分支；取消/失败的副本保留现场，Daemon 启动时统一清理；分支归属以数据库 checkpoint（`recorded_tip`）为准，删除重建的同名分支不会被误续接。

### Web 与飞书入口

正式 Web 控制台位于 `http://127.0.0.1:8000/app`。Web 创建事项经过 `IngressService`（`Idempotency-Key` 防重复提交）；任务详情支持追问某条执行记录对应的 Agent，也可使用 `@agent:ID` 或 `@squad:ID` 显式路由。飞书侧自 KI-001 起与 Issue 解耦：**普通消息是与 Agent 的 Chat，只有显式 `/issue` 才建看板事项**。

飞书开放平台需要订阅 `im.message.receive_v1`，回调地址设为 `POST /api/integrations/feishu/events`。配置 `.env.example` 中的四个 `FEISHU_*` 必填项后启用该端点；官方 SDK 负责验签与解密，应用凭证不会写入数据库。设置 `FEISHU_WORKSPACE_ID=local` 后，飞书与本地 Web 可以使用同一批 Agent/Squad。

飞书 Chat 的行为：

- 私聊消息全部接收；群聊仅处理 @机器人 的消息。私聊与普通群按 `chat_id` 隔离，**群内每个话题是独立会话**（`chat_id + thread_id`）——一个话题就是一个 Agent 的房间。
- 会话在首条消息时绑定 Agent：`agent=geometry-agent 画个布局` 绑定 geometry-agent，无前缀则绑定 `FEISHU_DEFAULT_AGENT_ID`；**绑定后不可变**，后续 `agent=`/`squad=` 只剥离并回引导（`squad=` 引导走 `/issue`）。
- 同一会话 3 秒内的连续消息合并为一轮 Agent 执行（消息逐条保存）；处理中给原消息加 Typing 表情回执，完成后回复进原话题（建议在飞书应用后台开通表情回复权限，未开通时回执自动降级）。
- Agent 完成、失败、取消或需要补充信息后，由持久化 Outbox 异步回传；投递采用指数退避，超过 `MICA_CHANNEL_MAX_ATTEMPTS` 后进入 `failed` 状态。batcher 为 API 进程内存态，**不要运行多个 API 实例**。

需要进入看板跟踪的事项，在任意会话里发送：

```text
/issue 修复订单导出异常
（可另起一行写描述）
```

`/issue` 立即创建看板卡片（负责人为会话绑定的 Agent，可在 Web 修改）并回执深链；其终态通知会回复到命令所在话题。历史行为变更：升级后飞书普通消息不再创建 Issue，存量飞书起源 Issue 的推进请在 Web 进行。

当前 Web 身份来自本机请求头，尚未接入企业 SSO，因此仍只应监听 `127.0.0.1`。

同一 Issue/Agent 在尚未执行时收到的连续补充会合并到一个待执行 Task；已有 Task 正在运行时，后续消息只汇入一个 successor Task。Daemon 领取时固化本轮输入快照，从而减少重复启动，同时保证执行中的上下文不会被后来消息改写。

### Agent Squad

用户可以在 Web 控制台组装 Squad，选择 Leader、成员和 Leader 协作规则。Squad 是协调对象而不是执行进程：Issue 指派给 Squad 后只先运行 Leader，Leader 根据 roster 中的角色和能力返回结构化委派决策，平台校验成员身份后再创建成员 Task；同一批成员 Task 全部结束后，平台只唤醒一次 Leader 汇总，最终由 Leader 将 Issue 推进到人工确认。成员等待输入、失败或取消时同样把结果交回 Leader；执行期间到达的用户追问会排在当前成员批次之后处理，不会被去重逻辑吞掉。

也可以通过 API 配置更细的成员角色和能力：

```bash
curl -X POST http://127.0.0.1:8000/api/squads \
  -H 'content-type: application/json' \
  -d '{
    "name":"Layout Squad",
    "workspace_id":"local",
    "leader_agent_id":"consultant-agent",
    "instructions":"先校验几何约束，再生成摆放方案",
    "members":[
      {"agent_id":"geometry-agent","role":"约束校验","capabilities":["geometry"]},
      {"agent_id":"placement-agent","role":"方案生成","capabilities":["placement"]}
    ]
  }'
```

Web 创建任务时可直接选择该 Squad；飞书文本使用下面的形式指定：

```text
mica squad=<squad-id> 根据会议室尺寸生成合规布局
```

### ACP 本地 Agent

ACP Agent 仍绑定本机 Runtime，并按项目配置在当前目录或独立 Git Worktree 中运行；未关联项目时使用 Daemon 管理的空工作目录。创建 Agent 时配置 ACP bridge 命令；权限默认拒绝，只有可信测试环境才显式开启 `allow`：

```bash
curl -X POST http://127.0.0.1:8000/api/agents \
  -H 'content-type: application/json' \
  -d '{
    "id":"codex-acp",
    "name":"Codex ACP",
    "runtime_id":"local",
    "provider":"acp",
    "provider_config":{
      "command":["npx","-y","@agentclientprotocol/codex-acp"],
      "permission_policy":"allow"
    }
  }'
```

### A2A 远程业务 Agent

A2A Agent 通过 Agent Card 发现远端 HTTP+JSON 1.0 接口。访问令牌不写入数据库，只保存环境变量名；`output_schema` 用于校验 Geometry、Placement 等业务 Agent 的 JSON 结果：

```bash
export GEOMETRY_AGENT_TOKEN='replace-me'

curl -X POST http://127.0.0.1:8000/api/agents \
  -H 'content-type: application/json' \
  -d '{
    "id":"geometry-agent",
    "name":"Geometry Agent",
    "runtime_id":"local",
    "provider":"a2a",
    "instructions":"根据用户参数生成满足约束的空间方案",
    "provider_config":{
      "agent_card_url":"https://geometry.internal.example/.well-known/agent-card.json",
      "token_env":"GEOMETRY_AGENT_TOKEN",
      "skill_id":"layout",
      "output_schema":{
        "type":"object",
        "required":["layoutId","valid"],
        "properties":{
          "layoutId":{"type":"string"},
          "valid":{"type":"boolean"}
        }
      }
    }
  }'
```

阶段 2–6 的真实环境验收清单见 [`docs/test-plan.md`](docs/test-plan.md)。

## 安全说明

- 当前阶段默认单机运行,不要把尚未接入身份认证的控制台暴露到局域网或公网。
- `workspace/` 是运行时数据目录,默认已被 `.gitignore` 忽略。

## 仓库结构

```text
mica/          正式后端包：领域模型、应用服务、仓储接口、Daemon 与 Agent Provider
sessions/zh/   历史原型，不参与正式包构建
tests/         行为与契约测试
docs/wiki/     可同步到 Wiki 的设计与实现文档
docs/foundations/ 基础组件说明（PostgreSQL 核心用法等）
openspec/      早期规格与设计记录
```
