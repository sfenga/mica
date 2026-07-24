# s03 — `claude` CLI runner on task(stream-json + fail-closed + session_id 捕获)

s02 把认领执行循环立住了,但执行器是 `StubRunner`——发合成事件、不碰模型。s03 把执行器换成真实的 `claude`(Claude Code)CLI:daemon 认领一条 task 后,spawn `claude -p --verbose --output-format stream-json`,逐行解析它吐出的事件,经 `on_event` 透传,最后按 fail-closed 契约定 done/failed,并捕获 `session_id`(claude 会话标识,存入 task 行)。从这一章起,mica 的 agent 才真正接上真实模型。

## 一、核心设计思想

claude 在 `-p --verbose --output-format stream-json` 模式下,stdout 逐行输出 JSON 事件,每行一个 `{"type": ...}`。不是一段最终文本,而是一条事件流:`system`(init,带 session_id)、`assistant`(模型回合,含 text/thinking/tool_use)、`user`(tool_result)、`result`(终态,带 session_id)、`log`。mica 要做的不是"跑完拿 stdout 字符串",而是**逐行解析这条流,把过程事件透传出去,再用终态事件定结论,顺便捕获 session_id**。

第一个思想:**解析与 spawn 分离**。`parse_stream_line(line)` 是纯函数——吃一行 JSON,返回"要发的事件列表 + 终态(或 None)"。`ClaudeCliRunner.run` 负责 spawn + 逐行喂给解析器 + finalize。解析逻辑用 canned 行单测,不依赖、不 spawn 真 claude:

```python
def parse_stream_line(line: str) -> tuple[list[dict], dict | None]:
    s = line.strip()
    if not s:
        return [], None
    try:
        evt = json.loads(s)
    except json.JSONDecodeError:
        return [], None
    events: list[dict] = []
    terminal: dict | None = None
    t = evt.get("type")
    if t == "system":
        if evt.get("subtype") in ("thinking_tokens", "hook_started", "hook_response"):
            pass                           # 高频噪声/内部 hook 子类型,丢弃
        else:
            events.append({"kind": "system", "session_id": evt.get("session_id", "")})
    elif t == "assistant":
        for block in evt.get("message", {}).get("content", []):
            if block.get("type") == "text":
                events.append({"kind": "text", "text": block.get("text", "")})
            elif block.get("type") == "tool_use":
                events.append({"kind": "tool_use", "tool": block.get("name", "")})
    elif t == "result":
        is_error = bool(evt.get("is_error")) or evt.get("subtype") == "error"
        terminal = {"status": "failed" if is_error else "done",
                    "output": evt.get("result", ""), "is_error": is_error,
                    "session_id": evt.get("session_id", "")}
    # ...
    return events, terminal
```

第二个思想:**fail-closed 定终态**。进程正常退出 ≠ 成功。claude 的 stream-json 协议要求最后有一个 `result` 事件才算正常结束;没有 `result`(子进程崩了、stdout 截断)就不能当成功,否则会把半截输出误当结果。finalize 的契约是「缺 result / is_error / exit≠0 → 一律 failed」:

```python
if terminal is None:                      # 没 result 事件
    return {"status": "failed",
            "output": stderr or f"stream ended without result event (exit {rc})",
            "exit_code": rc, "session_id": captured_sid}
if terminal["is_error"]:                  # result 显式报错
    return {"status": "failed", "output": terminal["output"] or stderr,
            "exit_code": rc, "session_id": captured_sid}
if rc != 0:                               # 进程非零退出
    return {"status": "failed", "output": stderr or f"exit {rc}",
            "exit_code": rc, "session_id": captured_sid}
return {"status": "done", "output": terminal["output"],
        "exit_code": rc, "session_id": captured_sid}
```

第三个思想:**捕获 session_id**。claude 的 stream-json 在 `system`(init)和 `result` 事件里都带 `session_id`。`ClaudeCliRunner.run` 在解析时把它捕获进 `RunResult.session_id`——daemon 会把这个 session_id 存到 task 行上,作为 claude 会话标识(跨执行续会话时用它做 `--resume`):

```python
captured_sid = ""
for line in proc.stdout:
    events, term = parse_stream_line(line)
    for ev in events:
        if ev.get("kind") == "system" and ev.get("session_id"):
            captured_sid = ev["session_id"]      # system init 带 session_id
        ev["task_id"] = task.id
        on_event(ev)
    if term is not None:
        terminal = term
        if term.get("session_id"):
            captured_sid = term["session_id"]     # result 的 session_id 优先
```

`result` 的 session_id 覆盖 `system` init 的——result 是权威终态。如果 claude 吐回与传入不同的全新 session_id(说明 resume 没接上),daemon 会用这个新 id 覆盖 task 上的旧 id。

第四个思想:**执行器可替换,daemon 不变**。`ClaudeCliRunner` 实现的是 s02 定义的 `AgentRunner` Protocol:`run(task, issue, context, on_event, resume_session_id=None) -> RunResult`。s02 的 daemon 构造时把 `StubRunner()` 换成 `ClaudeCliRunner()`,daemon 的认领/执行/标终态逻辑一行不改:

```python
# s02: daemon = Daemon(issue_store, task_store, agent, StubRunner())
# s03: daemon = Daemon(issue_store, task_store, agent, ClaudeCliRunner())
```

## 二、为什么这么设计

### 为什么用 stream-json,不让 claude 直接输出纯文本?

纯文本(`claude -p "<prompt>"`)只给最终结果,过程全丢——看不到模型调了什么工具、走到了哪一步。stream-json 把每一回合的 text/tool_use、每条 tool_result、最终 result 都吐出来,mica 才能把"执行过程"透传出去(本章先经 `on_event` 打印)。过程和结果是两条通道,这也是 s02 `on_event` 回调 + `RunResult` 分开的用意。

### 为什么 fail-closed,不"exit 0 就算成功"?

子进程 exit 0 只说明"claude 进程正常退出了",不说明"它把任务做完了并给了最终答案"。claude 可能中途被 kill、stdout pipe 断了、或者它自己报了 error result。把这些当成功,task 会错误地标 completed、半截输出被当成结果。成功必须见到 `result` 且非 `is_error` 且 exit 0,任一不满足即 failed。

### 为什么把 `parse_stream_line` 拆成纯函数?

如果解析逻辑长在 `ClaudeCliRunner.run` 内部,要测它就得 spawn 真 claude——每次单测都调真实模型。拆成纯函数后,测试喂 canned JSON 行就能覆盖:tool_use 透传、tool_result 透传、result 成功/失败、无 result、坏行、session_id 捕获。spawn 那层用 monkeypatch 喂假进程验 finalize。要测的是解析契约,不是真的调一次 claude。

### 为什么 demo 用无需工具的 prompt?

`ClaudeCliRunner._cmd` 默认传 `--permission-mode bypassPermissions`——让 claude 自动批准所有工具调用(Bash/Read/Edit),能执行真实编码任务。demo 仍用无需工具的 prompt(`Reply with exactly: mica s03 ok`)确保确定性,但生产任务可以放心让 claude 调工具。

## 三、事件怎么流转

```
  daemon 认领 task(queued→dispatched→running)
        │  issue: todo → in_progress
        ▼
  ClaudeCliRunner.run(task, issue, ...)
        │  spawn `claude -p --verbose --output-format stream-json "<issue.title+body>"`
        │
  逐行读 stdout:
    system(init)  → progress(kind=system, session_id)  ← 捕获 session_id
    assistant     → text/thinking/tool_use → progress
    user          → tool_result → progress
    result        → terminal{status, output, is_error, session_id}  ← 覆盖 session_id
    log           → progress(kind=log)
    control_request → 忽略
        │
        ▼
  stdout 读完 → wait() 取 exit code + stderr
        │
  fail-closed finalize:
    terminal is None?      → failed("stream ended without result event")
    terminal.is_error?     → failed(result output)
    exit code != 0?       → failed(stderr)
    否则                   → done(result output, session_id)
        │
        ▼
  RunResult → daemon: task completed → issue in_review
              (或 task failed → issue blocked)
```

事件映射表(贴生产 `pkg/agent/claude.go` 的处理):

| stream-json 事件 | mica 处理 |
|---|---|
| `system`(init) | progress(kind=system,带 session_id);thinking_tokens/hook 子类型丢弃 |
| `assistant` 的 `text` 块 | progress(kind=text,带文本) |
| `assistant` 的 `tool_use` 块 | progress(kind=tool_use,带工具名) |
| `user` 的 `tool_result` 块 | progress(kind=tool_result,带 tool_use_id + is_error) |
| `result`(subtype=success) | 终态 done + session_id |
| `result`(subtype=error / is_error=true) | 终态 failed + session_id |
| `log` | progress(kind=log) |
| `control_request` | 忽略(权限策略不属本章) |

`tool_use` 与 `tool_result` 一定要透传——claude 的真实工作方式就是调工具,丢掉它们等于把执行过程削成只剩最终文本。

## 试一试

```bash
python -m pytest tests/test_stream_parse.py tests/test_cli_runner_finalize.py -v   # 解析+finalize 单测(不调真 claude)
python sessions/zh/s03_claude_runner.py                                            # demo:真跑 claude(需已登录)
```

demo 派一个无需工具的 task,daemon 认领 → spawn claude → 流式打印 `[event] {...}` → 收到 result → task `completed`、issue `in_review`:

```
--- 派活前 ---
ID            STATUS       ASSIGNEE                TITLE
...           todo         agent:agent-1           Reply with exactly: mica s03 ok
[daemon] claimed task ... for issue ...
[event] {'kind': 'system', 'session_id': '...', 'task_id': '...'}
[event] {'kind': 'text', 'text': 'mica s03 ok', 'task_id': '...'}
--- 认领执行后 ---
ID            STATUS       ASSIGNEE                TITLE
...           in_review    agent:agent-1           Reply with exactly: mica s03 ok
```

> `claude` 未装/未登录时,`ClaudeCliRunner` 的 `Popen` 抛 `FileNotFoundError` → 返回 `failed`(`CLI not found`),session_id 为空,task 标 failed、issue 标 blocked,不静默跳过。

## 四、与完整 multiCa 的区别

| 维度 | 完整 multiCa(生产) | mica s03 |
|---|---|---|
| 调用方式 | `claude --input-format stream-json --output-format stream-json --verbose --permission-mode bypassPermissions`,stdin 写 user 消息、处理 control_request 自动批工具 | `claude -p --verbose --output-format stream-json --permission-mode bypassPermissions "<prompt>"`(单次 prompt,默认 bypassPermissions) |
| 事件处理 | 全:system/assistant/user/result/log/control_request(auto-approve) | system/assistant/user/result/log 透传;control_request 忽略 |
| 终态契约 | `finalizeStreamResult` fail-closed(sawResult + 非 is_error + exit 0) | **完全一致**(照搬) |
| session_id | 从 stream 捕获,写 task.session_id 列 | 同(从 system/result 捕获 → RunResult.session_id) |
| 事件去处 | Session.Messages → realtime hub → transcript | `on_event` 回调 → 打印(s02 既有) |
| 工具权限 | 自动 approve(control_request 回 control_response) | 默认 `--permission-mode bypassPermissions`(claude 自动批工具) |
| 进程模型 | 流式 scanner + stdin 长开 + 超时/取消 | `Popen` 流式读 stdout 至 EOF,无超时/取消 |

一句话:**s03 把 multiCa「spawn 真实 CLI + 解析 stream-json + fail-closed 定终态 + 捕获 session_id」做实了,执行器从 stub 升级为真实 claude**;砍掉的是 control_request 自动批工具、超时/取消、stdin 长开会话——那些是生产化主题。
