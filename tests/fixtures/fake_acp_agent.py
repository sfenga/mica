from __future__ import annotations

import json
import sys
import time


def send(payload):
    print(json.dumps(payload, separators=(",", ":")), flush=True)


mode = sys.argv[1] if len(sys.argv) > 1 else "complete"
session_id = "acp-session-1"

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {"loadSession": True},
                    "authMethods": [],
                    "agentInfo": {"name": "fake-acp", "version": "1.0"},
                },
            }
        )
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": session_id}})
    elif method == "session/load":
        session_id = message["params"]["sessionId"]
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "session/prompt":
        if mode == "slow":
            time.sleep(60)
            continue
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "working "},
                    },
                },
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": 900,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": "tool-1", "title": "edit file"},
                    "options": [
                        {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                        {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    ],
                },
            }
        )
        permission = json.loads(sys.stdin.readline())
        outcome = permission.get("result", {}).get("outcome", {}).get("optionId", "cancelled")
        send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"permission={outcome}"},
                    },
                },
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"stopReason": "end_turn"},
            }
        )
    elif method == "session/cancel":
        break
