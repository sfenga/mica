# s03 — 真实 Claude CLI Runner

s02 的 daemon 已经能认领 task,但执行器还是 `StubRunner`:它只发几条假事件,然后返回 done。

s03 把执行器换成真实的 Claude Code CLI:

```text
daemon claim task
   |
   v
ClaudeCliRunner spawn `claude`
   |
   v
逐行读取 stream-json
   |
   v
on_event 透传过程事件
   |
   v
根据 result / exit code 决定 task completed 或 failed
```

从这一章开始,mica 的 agent 才真正接上模型。系统仍然不调用 Anthropic API;模型连接由本机已登录的 `claude` CLI 自己处理。

## 1. 为什么不能只拿最终 stdout

coding agent 的执行过程很重要。它可能:

```text
输出中间解释
调用工具
收到工具结果
失败并给出错误
最后产出 result
```

如果只把 stdout 当成最终文本,这些过程都丢了。`claude -p --verbose --output-format stream-json` 会把执行过程变成一行行 JSON:

```text
system       初始化信息,通常含 session_id
assistant    模型消息,可能含 text / thinking / tool_use
user         工具结果 tool_result
result       终态结果
log          日志
```

s03 要做的就是读这条事件流,而不是等进程结束后读一大段字符串。

## 2. Runner 的命令

`ClaudeCliRunner._cmd()` 构造命令:

```python
cmd = [self.cli, "-p", "--verbose", "--output-format", "stream-json", prompt]
if permission_mode:
    cmd += ["--permission-mode", permission_mode]
if resume_session_id:
    cmd += ["--resume", resume_session_id]
```

默认 `permission_mode` 是 `bypassPermissions`,让 Claude Code 自动批准工具调用。生产 multiCa 的 Claude backend 也会掌控 `--output-format stream-json`、`--input-format stream-json`、`--permission-mode bypassPermissions` 这些协议参数,避免用户自定义参数破坏 daemon 和 CLI 的传输契约。mica s03 用更简单的 `claude -p` 单次 prompt 形态。

注意:命令里不能带 `--no-session-persistence`。后面 s05 需要 Claude CLI 自己保存 session,才能通过 `--resume <session_id>` 续会话。

## 3. 解析和执行分开

解析函数是纯函数:

```python
def parse_stream_line(line: str) -> tuple[list[dict], dict | None]:
    ...
```

它只负责:

```text
输入: 一行 stream-json
输出: 要透传的过程事件列表 + 终态 result 或 None
```

这样测试可以直接喂假 JSON 行,不需要真的启动 `claude`。

主要映射:

```text
system                     -> {"kind": "system", "session_id": ...}
assistant text             -> {"kind": "text", "text": ...}
assistant thinking         -> {"kind": "thinking", "text": ...}
assistant tool_use         -> {"kind": "tool_use", "tool": ...}
user tool_result           -> {"kind": "tool_result", ...}
log                        -> {"kind": "log", "message": ...}
result success             -> terminal done
result error / is_error    -> terminal failed
```

`thinking_tokens`、hook 类 system 事件属于高频内部噪声,本章丢弃。`control_request` 在 mica 里也不处理;生产 multiCa 会保持 stdin 打开并自动回应部分 control request,这是生产化权限协议,不放进 s03。

## 4. on_event 是过程通道

`ClaudeCliRunner.run()` 会逐行读 stdout:

```python
for line in proc.stdout:
    events, term = parse_stream_line(line)
    for ev in events:
        if ev.get("kind") == "system" and ev.get("session_id"):
            captured_sid = ev["session_id"]
        ev["task_id"] = task.id
        on_event(ev)
    if term is not None:
        terminal = term
        if term.get("session_id"):
            captured_sid = term["session_id"]
```

`on_event` 只负责过程。最终成功/失败不靠它决定,而靠 `RunResult` 返回给 daemon。

这延续了 s02 的分工:

```text
on_event      执行过程
RunResult     终态结果
```

后面 s04 会把 `on_event` 落盘,s06 会把它实时推到 WebSocket。

## 5. session_id 要捕获到 task 上

Claude CLI 的 stream-json 里会出现 `session_id`。s03 把它捕获进 `RunResult.session_id`,daemon 再写回 task:

```text
system session_id   先记录
result session_id   如果存在,覆盖前者
```

为什么 result 优先?因为它是终态事件,最接近这次执行最终归属的会话。

这个字段在 s03 看起来只是记录;到 s05,它会变成续会话的关键:

```text
上一条 task.session_id -> claude --resume <id>
```

## 6. 成功必须 fail-closed 判断

进程退出码是 0,不一定代表任务成功。stream-json 正常结束时应该看到 `result` 事件。

s03 的终态判断是:

```text
没有 result 事件       -> failed
result 显式 is_error   -> failed
exit code 非 0         -> failed
否则                  -> done
```

代码形状:

```python
if terminal is None:
    return {"status": "failed",
            "output": stderr or f"stream ended without result event (exit {rc})",
            "exit_code": rc, "session_id": captured_sid}
if terminal["is_error"]:
    return {"status": "failed",
            "output": terminal["output"] or stderr or "result is_error",
            "exit_code": rc, "session_id": captured_sid}
if rc != 0:
    return {"status": "failed", "output": stderr or f"exit {rc}",
            "exit_code": rc, "session_id": captured_sid}
return {"status": "done", "output": terminal["output"],
        "exit_code": rc, "session_id": captured_sid}
```

这叫 fail-closed:只在协议完整、终态非错误、进程退出码正常时标成功。生产 multiCa 的 `finalizeStreamResult` 也明确把“必须看到 result”作为成功条件。

## 7. 整体链路

```text
Daemon.claim_next()
   |
   v
TaskStore.claim(): queued -> dispatched
TaskStore.start(): dispatched -> running
issue -> in_progress
   |
   v
ClaudeCliRunner.run(...)
   |
   |-- spawn claude
   |-- parse stdout stream-json
   |-- on_event(system/text/tool_use/tool_result/log)
   |-- capture session_id
   `-- finalize RunResult
          |
          |-- done   -> task completed -> issue in_review
          `-- failed -> task failed    -> issue blocked
```

## 8. 试一试

单测不调真 `claude`:

```bash
python -m pytest tests/test_stream_parse.py tests/test_cli_runner_finalize.py -v
```

它们覆盖:

```text
坏 JSON / 空行
system session_id
assistant text / thinking / tool_use
user tool_result
result success / error
无 result 判失败
exit 非 0 判失败
CLI 不存在判失败
resume_session_id 为空时不加 --resume
```

demo 会调真实 CLI:

```bash
python sessions/zh/s03_claude_runner.py
```

需要本机已安装并登录 `claude`。如果没装,runner 会返回:

```text
CLI not found: claude
```

然后 task 标 failed,issue 标 blocked,不会静默跳过。

## 9. 本章边界

s03 做了:

```text
真实 claude CLI 执行器
stream-json 逐行解析
tool_use / tool_result 过程透传
fail-closed 终态判断
session_id 捕获
```

s03 不做:

```text
JSONL 持久化              s04
评论后 --resume           s05
WebSocket 实时             s06
control_request 自动回应   生产化主题
stdin 长开会话             生产化主题
超时/取消/重试洋葱         生产化主题
```

一句话总结:**s03 把 s02 的假执行器换成真实 Claude CLI。它读的是事件流,不是最终文本;成功必须看到完整 result。**
