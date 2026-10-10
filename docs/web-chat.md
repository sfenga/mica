# Web Chat

Web Chat lives at `/app#chat`; a saved conversation uses `/app#chat/{session_id}`.
The first version follows Multica's full-page Chat: resizable history pane,
Agent picker, editable title, message transcript, composer, task progress,
queued turns, pinning, unread counts, archive/restore and archived-only deletion.

## Sending and execution

Select an Agent before composing. A draft does not create a database session.
The first send atomically creates the session, user message, task and queued
execution event. Further sends create independent tasks. The existing claim
mutex serializes execution per conversation. The Agent remains bound to the
session; select another Agent by starting a new chat.

`request_id` is an opaque client-generated idempotency key. The browser retains
it for an uncertain send and reuses it on retry, including after a refresh.
Reusing a key with different content returns 409. Message bodies are trimmed.
Drafts are stored locally per conversation or selected Agent.

Web sends execute immediately. If the same session has pending Feishu input,
that batch is sealed first in the same transaction, preserving arrival order
without mixing channel and Web messages. Only channel-origin task input creates
Feishu outbound replies. Both entry points share the conversation history.
An incoming Feishu message restores an archived channel conversation.

A turn's prompt excludes future queued/unowned messages and cancelled turns.
Task event WebSockets supply progress; periodic durable reads reconcile session
metadata, terminal messages and changes originating in another tab or Feishu.
Reconnects rebuild progress from event history. Messages use cursor pagination.

## API

All session endpoints default to workspace `local`; existing-session endpoints
accept `workspace_id` in the query. User identity follows Mica's existing local
`X-Mica-User` header convention (default `local-user`).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/chat/sessions` | History, last message, source, unread count, pending tasks |
| POST | `/api/chat/sessions` | First send: `agent_id`, `body`, `request_id`, optional `workspace_id` |
| GET | `/api/chat/sessions/{id}` | Session summary |
| PATCH | `/api/chat/sessions/{id}` | Optional `title`, `pinned`, `archived` |
| DELETE | `/api/chat/sessions/{id}` | Delete an archived, idle session |
| POST | `/api/chat/sessions/{id}/messages` | Send `body` and `request_id` |
| GET | `/api/chat/sessions/{id}/messages` | `limit` 1–100, optional `before` message ID; messages, tasks, cursor |
| GET | `/api/chat/sessions/{id}/pending-tasks` | Active and queued turns |
| POST | `/api/chat/sessions/{id}/read` | Advance this user's cursor through `message_id` |
| POST | `/api/tasks/{id}/cancel` | Stop a running turn or cancel a queued turn |
| WS | `/api/tasks/{id}/events` | Existing execution event stream |

Archive is blocked while tasks or unsealed channel messages remain pending.
Delete takes the session lock and transactionally removes messages, bindings,
tasks, task events, read cursors and associated outbound notifications. The same
lock fences terminal write-back and notification projection. Local managed Chat
workspaces are cleaned after commit; a filesystem failure is logged without
undoing database deletion. Directories on other runtime hosts require cleanup on
those hosts and are never deleted using a remotely supplied `work_dir` path.

Migration `013_web_chat` adds pin/read state and fixes assistant messages' nullable
external IDs. Run the normal application startup migration; editing an already
applied migration is unnecessary.

## First-version scope

Text, Markdown/code blocks, reply copying, execution progress, stop, queue
cancellation, history pagination and session management are included. Attachments,
rich mentions, project context, floating chat, queue editing/prioritization and
suggested follow-up actions are deferred. The UI exposes no placeholder controls
for these features.

## Verification

`tests/test_web_chat.py` runs against memory and, when `MICA_TEST_DATABASE_URL`
is set, isolated PostgreSQL schemas. It covers lifecycle, idempotency and
concurrent retries, transaction rollback, pagination, workspace boundaries,
future/cancelled turn isolation, Feishu routing and deletion cleanup.
