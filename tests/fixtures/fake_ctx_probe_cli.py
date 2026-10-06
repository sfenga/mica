"""Fake claude-flavor CLI that reports the mica-ctx channel it received.

Modes:
  probe — report the MICA_* env vars and the prompt it was invoked with
  pull  — actually run `mica-ctx comments` and report what came back

With command_prefix the daemon appends the prompt as the last argv, so
argv = [script, mode, prompt].
"""

import json
import os
import subprocess
import sys

mode = sys.argv[1]
prompt = sys.argv[-1] if len(sys.argv) > 2 else ""

print(
    json.dumps({"type": "system", "subtype": "init", "session_id": "ctx-probe-session"}),
    flush=True,
)

if mode == "probe":
    findings = {
        "api_url": os.environ.get("MICA_API_URL"),
        "issue_id": os.environ.get("MICA_ISSUE_ID"),
        "task_id": os.environ.get("MICA_TASK_ID"),
        "prompt": prompt,
    }
elif mode == "pull":
    result = subprocess.run(["mica-ctx", "comments"], capture_output=True, text=True, timeout=30)
    findings = {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
else:
    findings = {"error": f"unknown mode {mode}"}

print(
    json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": json.dumps(findings),
            "session_id": "ctx-probe-session",
        }
    ),
    flush=True,
)
