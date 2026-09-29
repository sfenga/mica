import json
import sys
import time

flavor = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else "complete"

if flavor == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "codex-session-1"}), flush=True)
else:
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "claude-session-1"}), flush=True)

if mode == "slow":
    while True:
        time.sleep(1)

if flavor == "claude" and mode == "control-request":
    initial = json.loads(sys.stdin.readline())
    print(
        json.dumps(
            {
                "type": "control_request",
                "request_id": "permission-1",
                "request": {
                    "subtype": "tool_use",
                    "tool_name": "Bash",
                    "input": {"command": "printf ok", "run_in_background": True},
                },
            }
        ),
        flush=True,
    )
    approval = json.loads(sys.stdin.readline())
    approved = (
        initial.get("type") == "user"
        and approval.get("response", {}).get("response", {}).get("behavior") == "allow"
        and approval.get("response", {})
        .get("response", {})
        .get("updatedInput", {})
        .get("run_in_background")
        is False
    )
    print(
        json.dumps(
            {
                "type": "result",
                "is_error": not approved,
                "result": "permission approved" if approved else "invalid approval",
                "session_id": "claude-session-control",
            }
        ),
        flush=True,
    )
    raise SystemExit(0)

if flavor == "codex":
    print(
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "codex complete"}}
        ),
        flush=True,
    )
else:
    assistant_text = "claude " if mode == "assistant-only" else "claude streaming"
    print(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": assistant_text}]},
            }
        ),
        flush=True,
    )
    if mode == "assistant-only":
        print(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "streaming"}]},
                }
            ),
            flush=True,
        )
    if mode != "assistant-only":
        print(
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "claude complete",
                    "session_id": "claude-session-1",
                }
            ),
            flush=True,
        )
