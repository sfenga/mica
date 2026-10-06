"""`mica-ctx`: the task-scoped read channel for agents running inside mica.

The daemon injects MICA_API_URL / MICA_ISSUE_ID / MICA_TASK_ID into the agent
subprocess environment (see providers.base.build_child_env). Agents use this
command to pull issue context on demand instead of relying on the platform to
predict and push everything into the prompt.

Read-only by design: result delivery and status transitions stay
platform-owned, so this command exposes no write operations.
"""

from __future__ import annotations

import json
import os
import sys
from typing import List, NoReturn

import httpx

EXCERPT_LENGTH = 100

USAGE = """usage:
  mica-ctx comments           List this issue's comments (id, time, author, excerpt)
  mica-ctx comment <id>       Print one comment's full body
"""

# TODO(auth): once the platform gains task-scoped tokens, send
# MICA_TASK_TOKEN as a bearer header here and enforce it on the API side.


def _fail(message: str, code: int = 1) -> NoReturn:
    print(f"mica-ctx: {message}", file=sys.stderr)
    sys.exit(code)


def _issue_id() -> str:
    value = os.environ.get("MICA_ISSUE_ID", "").strip()
    if not value:
        _fail("MICA_ISSUE_ID is not set; this command runs inside a mica task", code=2)
    return value


def _default_api_url() -> str:
    host = os.environ.get("MICA_API_HOST", "127.0.0.1")
    port = os.environ.get("MICA_API_PORT", "8000")
    return f"http://{host}:{port}"


def _fetch_issue() -> dict:
    api = os.environ.get("MICA_API_URL") or _default_api_url()
    api = api.rstrip("/")
    issue_id = _issue_id()
    try:
        # trust_env=False: this channel talks to an explicit platform URL; ambient
        # proxies (env vars or macOS system proxy) must not intercept it.
        with httpx.Client(trust_env=False) as client:
            response = client.get(f"{api}/api/issues/{issue_id}", timeout=10)
    except httpx.HTTPError as exc:
        _fail(f"cannot reach mica API at {api}: {exc}")
    if response.status_code != 200:
        _fail(f"GET /api/issues/{issue_id} returned {response.status_code}")
    try:
        return response.json()
    except json.JSONDecodeError:
        _fail(f"GET /api/issues/{issue_id} did not return JSON")


def _list_comments(issue: dict) -> None:
    comments: List[dict] = issue.get("comments") or []
    if not comments:
        print("(no comments)")
        return
    for comment in comments:
        excerpt = " ".join(str(comment.get("body", "")).split())[:EXCERPT_LENGTH]
        author = f"{comment.get('author_type', '?')}:{comment.get('author_id', '?')}"
        print(f"{comment.get('id')}  {comment.get('created_at', '')}  {author}  {excerpt}")


def _show_comment(issue: dict, comment_id: str) -> None:
    for comment in issue.get("comments") or []:
        if comment.get("id") == comment_id:
            print(comment.get("body", ""))
            return
    _fail(f"comment not found on this issue: {comment_id}")


def main() -> None:
    args = sys.argv[1:]
    if args == ["comments"]:
        _list_comments(_fetch_issue())
    elif len(args) == 2 and args[0] == "comment":
        _show_comment(_fetch_issue(), args[1])
    else:
        sys.stderr.write(USAGE)
        sys.exit(2)


if __name__ == "__main__":
    main()
