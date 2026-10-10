import { $, $$, esc, toast, storageGet, storageSet, formatTime } from "./ui.js";
import { api, jsonOptions } from "./api.js";
import { state, TASK_LABEL } from "./state.js";
import { markdown } from "./markdown.js";

const paths = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  back: '<path d="m14 6-6 6 6 6"/>',
  send: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  archive: '<path d="M4 8v12h16V8M3 4h18v4H3zM9 12h6"/>',
  pin: '<path d="m9 3 6 0-1 6 4 4H6l4-4-1-6M12 13v8"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.more}</svg>`;
let view = null;
let pendingAgent = null;
const sessionUrl = (id) => `/api/chat/sessions/${encodeURIComponent(id)}`;
const agentFor = (id) => state.agents.find((a) => a.id === id);
const avatar = (agent) =>
  `<span class="chat-avatar" aria-hidden="true">${esc((agent?.name || "A").slice(0, 1))}</span>`;
const title = (session) => session?.title || "新聊天";
const routeId = () => {
  try {
    return decodeURIComponent(location.hash.slice(6));
  } catch {
    return "";
  }
};
const active = (tasks) =>
  tasks.find((t) =>
    ["dispatched", "running", "waiting_input"].includes(t.status),
  );

export function disposeChat() {
  if (!view) return;
  clearInterval(view.timer);
  view.abort.abort();
  for (const socket of view.sockets.values()) socket.close();
  view.dialog?.remove();
  view.menuAbort?.abort();
  $(".chat-menu")?.remove();
  view = null;
  document.body.classList.remove("chat-workspace");
}

export async function mountChat() {
  const id = location.hash.startsWith("#chat/") ? routeId() : "";
  if (view && view.id === id) {
    await refreshChat();
    return;
  }
  const old = view;
  $(".chat-menu")?.remove();
  const sessions = old?.sessions || [];
  const archived = old?.archived || false;
  disposeChat();
  document.body.classList.add("chat-workspace");
  const v = (view = {
    id,
    sessions,
    archived,
    messages: [],
    tasks: [],
    live: new Map(),
    sockets: new Map(),
    turnTasks: [],
    abort: new AbortController(),
    loading: true,
    draftAgent: null,
    sending: false,
    draftKey: id || "new",
    request: null,
    follow: true,
    syncing: false,
  });
  $("#page").innerHTML =
    `<div class="chat-layout${id ? " has-conversation" : ""}">
    <aside class="chat-history"><header class="chat-list-header"><h1>Chat</h1>
      <button class="chat-icon" id="chat-new" title="新建聊天" aria-label="新建聊天">${icon("plus")}</button></header>
      <div class="chat-session-list" id="chat-sessions" aria-label="会话列表"></div>
      <button class="chat-archive-toggle" id="chat-archives">${icon("archive")}<span>已归档</span></button>
    </aside>
    <div class="chat-resizer" role="separator" tabindex="0" aria-label="调整会话列表宽度" aria-orientation="vertical"></div>
    <section class="chat-conversation" id="chat-conversation" aria-label="聊天内容"></section></div>`;
  $("#chat-new").onclick = showAgentPicker;
  $("#chat-archives").onclick = () => {
    v.archived = !v.archived;
    renderList();
  };
  bindResize(v);
  renderList();
  try {
    await refreshChat();
    if (view !== v) return;
    if (pendingAgent) {
      beginDraft(pendingAgent);
      pendingAgent = null;
    }
    v.timer = setInterval(() => refreshChat().catch(() => {}), 2500);
  } catch (error) {
    if (view === v) {
      v.loading = false;
      renderConversation();
      toast(error.message, true);
    }
  } finally {
    if (view === v && !v.timer)
      v.timer = setInterval(() => refreshChat().catch(() => {}), 2500);
  }
}

export async function refreshChat() {
  const v = view;
  if (!v || v.syncing) return;
  v.syncing = true;
  try {
    const sessions = await api("/api/chat/sessions", {
      signal: v.abort.signal,
    });
    if (view !== v) return;
    v.sessions = sessions;
    if (v.id) {
      const session = sessions.find((s) => s.id === v.id);
      if (!session) {
        location.hash = "chat";
        toast("会话不存在或已删除");
        return;
      }
      v.archived = session.status === "archived";
      const results = await Promise.all([
        api(`${sessionUrl(v.id)}/messages?limit=${v.loaded || 50}`, {
          signal: v.abort.signal,
        }),
        api(`${sessionUrl(v.id)}/pending-tasks`, { signal: v.abort.signal }),
      ]);
      if (view !== v) return;
      const page = results[0];
      const oldIds = new Set(v.messages.map((m) => m.id));
      const signature = JSON.stringify([
        page,
        results[1],
        session,
        state.agents,
      ]);
      const changed = v.signature !== signature;
      v.signature = signature;
      // Keep older pages when the periodic latest-page refresh arrives.
      const first = page.messages[0];
      v.messages = [
        ...v.messages.filter(
          (m) =>
            first &&
            (m.created_at < first.created_at ||
              (m.created_at === first.created_at && m.id < first.id)),
        ),
        ...page.messages,
      ];
      v.hasMore = v.olderExhausted ? false : page.has_more;
      v.tasks = results[1];
      v.turnTasks = [
        ...v.turnTasks.filter((t) => !page.tasks.some((n) => n.id === t.id)),
        ...page.tasks,
      ];
      const metadataChanged =
        v.session &&
        (v.session.title !== session.title ||
          v.session.status !== session.status);
      v.session = session;
      if (changed || v.loading) {
        const hasNew = page.messages.some((m) => !oldIds.has(m.id));
        if (!v.loading && !metadataChanged && $("#chat-messages"))
          updateMessages(hasNew);
        else renderConversation();
      }
      connectTasks(v);
      await markRead(v);
    } else if (v.loading) renderConversation();
    v.loading = false;
    renderList();
  } finally {
    v.syncing = false;
  }
}

function renderList() {
  const v = view;
  if (!v) return;
  const list = v.sessions.filter(
    (s) => (s.status === "archived") === v.archived,
  );
  $("#chat-sessions").innerHTML =
    `${v.archived ? '<div class="chat-history-label">已归档</div>' : ""}${
      list
        .map((s) => {
          const agent = agentFor(s.agent_id);
          return `<div class="chat-session-row${s.id === v.id ? " selected" : ""}">
      <a href="#chat/${encodeURIComponent(s.id)}" class="chat-session-link" ${s.id === v.id ? 'aria-current="page"' : ""}>
      ${avatar(agent)}<span class="chat-session-copy"><span class="chat-session-title">${s.pinned ? '<span class="chat-pin-mark">⌁</span>' : ""}${esc(title(s))}</span>
      <span class="chat-session-agent">${esc(agent?.name || s.agent_id)}${s.source === "feishu" ? " · 飞书" : ""}</span>
      <span class="chat-session-preview">${s.pending_tasks.length ? '<span class="chat-dots">执行中…</span>' : esc(s.last_message || "暂无消息")}</span></span>
      <span class="chat-session-meta"><time>${esc(formatTime(s.updated_at))}</time>${s.unread_count ? `<span class="chat-unread">${s.unread_count}</span>` : ""}</span></a>
      <button class="chat-icon chat-row-menu" data-row-menu="${esc(s.id)}" title="会话操作" aria-label="${esc(title(s))}的操作">${icon("more")}</button></div>`;
        })
        .join("") ||
      `<div class="chat-list-empty">${v.archived ? "没有归档会话" : "还没有聊天"}<span>${v.archived ? "归档的聊天会显示在这里" : "点击 ＋ 选择 Agent 开始"}</span></div>`
    }`;
  $("#chat-archives").innerHTML =
    `${icon(v.archived ? "back" : "archive")}<span>${v.archived ? "返回聊天" : `已归档 · ${v.sessions.filter((s) => s.status === "archived").length}`}</span>`;
  $$("[data-row-menu]").forEach(
    (button) =>
      (button.onclick = () =>
        showMenu(
          v.sessions.find((s) => s.id === button.dataset.rowMenu),
          button,
        )),
  );
}

function renderConversation() {
  const v = view;
  if (!v) return;
  const session = v.session;
  const agent = agentFor(session?.agent_id || v.draftAgent?.id);
  const targeted = !!(session || v.draftAgent);
  const archived = session?.status === "archived";
  $("#chat-conversation").innerHTML = targeted
    ? `<header class="chat-thread-header">
    <button class="chat-icon chat-mobile-back" id="chat-back" aria-label="返回会话列表">${icon("back")}</button>
    ${avatar(agent)}<div class="chat-heading"><button id="chat-rename" ${session ? "" : "disabled"} title="重命名会话">${esc(title(session))}</button><span>${esc(agent?.name || session?.agent_id)}${session?.source === "feishu" ? " · 飞书会话" : ""}</span></div>
    ${session ? `<button class="chat-icon" id="chat-thread-menu" aria-label="会话操作">${icon("more")}</button>` : ""}</header>
    <div class="chat-message-area"><div id="chat-messages" class="chat-messages" tabindex="0" aria-label="消息历史"></div><button id="chat-latest" class="chat-latest" hidden>↓ 回到最新</button></div>
    <footer class="chat-footer">${archived ? '<div class="chat-notice">此会话已归档。恢复后可继续聊天。</div>' : ""}
    ${!agent ? '<div class="chat-notice">会话绑定的 Agent 已不存在</div>' : ""}
    <div id="chat-queue" class="chat-queue"></div><form id="chat-form" class="chat-composer">
    <textarea id="chat-input" aria-label="消息" placeholder="发送消息给 ${esc(agent?.name || "Agent")}…" rows="2" ${archived || !agent ? "disabled" : ""}></textarea>
    <div class="chat-composer-bottom"><span>⌘ / Ctrl + Enter 发送</span><button id="chat-submit" class="chat-send" type="submit" aria-label="发送消息" ${archived || !agent ? "disabled" : ""}>${icon("send")}</button></div></form></footer>`
    : `<div class="chat-empty"><span class="chat-empty-symbol">${icon("plus")}</span><h2>开始一段聊天</h2><p>选择一个 Agent，讨论想法或一起完成工作</p><button class="secondary-button" id="chat-start">新建聊天</button></div>`;
  if (!targeted) {
    $("#chat-start").onclick = showAgentPicker;
    return;
  }
  $(".chat-layout").classList.add("has-conversation");
  $("#chat-back").onclick = () => {
    if (v.id) location.hash = "chat";
    else {
      v.draftAgent = null;
      $(".chat-layout").classList.remove("has-conversation");
      renderConversation();
    }
  };
  if (session) {
    $("#chat-thread-menu").onclick = (event) =>
      showMenu(session, event.currentTarget);
    $("#chat-rename").onclick = () => renameSession(session);
  }
  const input = $("#chat-input");
  input.value = storageGet(`mica.chat.draft.${v.draftKey}`, "");
  input.oninput = () => {
    storageSet(`mica.chat.draft.${v.draftKey}`, input.value);
    v.request = null;
    storageSet(`mica.chat.request.${v.draftKey}`, "");
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 384)}px`;
    updateSendButton();
  };
  input.onkeydown = (event) => {
    if (
      !event.isComposing &&
      event.key === "Enter" &&
      (event.metaKey || event.ctrlKey)
    ) {
      event.preventDefault();
      send();
    }
  };
  $("#chat-form").onsubmit = (event) => {
    event.preventDefault();
    if (!input.value.trim() && active(v.tasks)) stopTask(active(v.tasks));
    else send();
  };
  const scroller = $("#chat-messages");
  scroller.onscroll = () => {
    v.follow =
      scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop < 80;
    $("#chat-latest").hidden = v.follow;
  };
  $("#chat-latest").onclick = () => {
    v.follow = true;
    scroller.scrollTop = scroller.scrollHeight;
    $("#chat-latest").hidden = true;
  };
  updateMessages(true);
}

function messageHtml(v) {
  if (!v.messages.length && !v.tasks.length) {
    const agent = agentFor(v.session?.agent_id || v.draftAgent?.id);
    return `<div class="chat-welcome">${avatar(agent)}<h2>与 ${esc(agent?.name || "Agent")} 聊聊</h2><p>描述你的问题，或从一个想法开始</p><div class="chat-starters"><button data-starter="帮我梳理一个想法">梳理一个想法</button><button data-starter="帮我分析一个问题">分析一个问题</button></div></div>`;
  }
  let html = v.hasMore
    ? '<button class="chat-load-older" id="chat-older">加载更早的消息</button>'
    : "";
  const completed = new Set(
    v.messages.filter((m) => m.role === "assistant").map((m) => m.task_id),
  );
  const lastInput = new Map(
    v.messages.filter((m) => m.role === "user").map((m) => [m.task_id, m.id]),
  );
  let previousDay = "";
  for (const message of v.messages) {
    const day = new Date(message.created_at).toLocaleDateString("zh-CN");
    if (day !== previousDay) {
      html += `<div class="chat-date">${esc(day)}</div>`;
      previousDay = day;
    }
    html += `<article class="chat-message ${message.role}" data-message="${esc(message.id)}"><div class="chat-message-body markdown">${markdown(message.body)}</div>${message.role === "assistant" ? `<button class="chat-copy" data-copy="${esc(message.id)}" aria-label="复制回复">复制</button>` : ""}</article>`;
    const task = v.tasks.find(
      (t) => t.id === message.task_id && t.status !== "queued",
    );
    if (
      task &&
      !completed.has(task.id) &&
      lastInput.get(task.id) === message.id
    )
      html += taskHtml(task, v);
    const finished = v.turnTasks.find(
      (t) =>
        t.id === message.task_id && ["failed", "cancelled"].includes(t.status),
    );
    if (message.role === "user" && finished)
      html += `<div class="chat-turn-status ${finished.status}">${esc(TASK_LABEL[finished.status])}${finished.last_error ? ` · ${esc(finished.last_error)}` : ""}</div>`;
  }
  return html;
}
function taskHtml(task, v) {
  const live = v.live.get(task.id);
  return `<article class="chat-message assistant"><div class="chat-progress"><span class="chat-pulse"></span>${esc(task.cancel_requested ? "正在停止…" : TASK_LABEL[task.status])}</div>${live?.text ? `<div class="chat-message-body markdown">${markdown(live.text)}</div>` : ""}${live?.detail ? `<div class="chat-execution-detail">${esc(live.detail)}</div>` : ""}</article>`;
}
function updateMessages(hasNew = false) {
  const v = view,
    scroller = $("#chat-messages");
  if (!v || !scroller) return;
  const oldTop = scroller.scrollTop,
    oldHeight = scroller.scrollHeight;
  scroller.innerHTML = messageHtml(v);
  if (v.follow) scroller.scrollTop = scroller.scrollHeight;
  else scroller.scrollTop = oldTop;
  $("#chat-latest").hidden = v.follow || (!hasNew && $("#chat-latest").hidden);
  if (hasNew && !v.follow) $("#chat-latest").hidden = false;
  const older = $("#chat-older");
  if (older) older.onclick = () => loadOlder(v, oldHeight);
  $$("[data-starter]").forEach(
    (button) =>
      (button.onclick = () => {
        $("#chat-input").value = button.dataset.starter;
        $("#chat-input").dispatchEvent(new Event("input"));
        $("#chat-input").focus();
      }),
  );
  $$("[data-copy]").forEach(
    (button) =>
      (button.onclick = async () => {
        try {
          await navigator.clipboard.writeText(
            v.messages.find((m) => m.id === button.dataset.copy).body,
          );
          toast("已复制");
        } catch {
          toast("复制失败", true);
        }
      }),
  );
  $("#chat-queue").innerHTML = v.tasks
    .filter((t) => t.status === "queued")
    .map(
      (task, i) =>
        `<div class="chat-queued-item"><span>排队 ${i + 1} · ${esc(v.messages.find((m) => m.task_id === task.id)?.body || "消息")}</span><button data-cancel="${esc(task.id)}" aria-label="取消排队消息">×</button></div>`,
    )
    .join("");
  $$("[data-cancel]").forEach(
    (button) =>
      (button.onclick = () =>
        stopTask(v.tasks.find((t) => t.id === button.dataset.cancel))),
  );
  updateSendButton();
}
function updateSendButton() {
  const v = view,
    button = $("#chat-submit"),
    input = $("#chat-input");
  if (!v || !button || !input) return;
  const running = active(v.tasks);
  const stop = running && !input.value.trim();
  button.innerHTML = stop
    ? '<span class="chat-stop-square"></span>'
    : icon("send");
  button.setAttribute("aria-label", stop ? "停止执行" : "发送消息");
  button.disabled =
    v.sending ||
    input.disabled ||
    (!stop && !input.value.trim()) ||
    (stop && running.cancel_requested);
}
async function send() {
  const v = view,
    input = $("#chat-input");
  if (!v || v.sending || !input || input.disabled || !input.value.trim())
    return;
  const body = input.value;
  let previous;
  try {
    previous = JSON.parse(
      storageGet(`mica.chat.request.${v.draftKey}`, "null"),
    );
  } catch {}
  const request = (v.request ||=
    previous?.body === body
      ? previous
      : { body, request_id: crypto.randomUUID() });
  storageSet(`mica.chat.request.${v.draftKey}`, JSON.stringify(request));
  const targetId = v.id;
  v.sending = true;
  updateSendButton();
  try {
    const result = await api(
      targetId ? `${sessionUrl(targetId)}/messages` : "/api/chat/sessions",
      jsonOptions("POST", {
        ...request,
        ...(targetId
          ? {}
          : { agent_id: v.draftAgent.id, workspace_id: "local" }),
      }),
    );
    // Remove only the submitted snapshot: switching views or typing while the
    // request is pending must not erase a newer draft.
    if (storageGet(`mica.chat.draft.${v.draftKey}`) === body)
      storageSet(`mica.chat.draft.${v.draftKey}`, "");
    if (view !== v) return;
    if (input.value === body) input.value = "";
    v.request = null;
    storageSet(`mica.chat.request.${v.draftKey}`, "");
    v.follow = true;
    if (!targetId) {
      if (input.value && input.value !== body) {
        storageSet(`mica.chat.draft.${result.session.id}`, input.value);
        storageSet(`mica.chat.draft.${v.draftKey}`, "");
      }
      location.hash = `chat/${encodeURIComponent(result.session.id)}`;
    } else await refreshChat();
  } catch (error) {
    if (view === v) toast(error.message, true);
  } finally {
    v.sending = false;
    if (view === v) updateSendButton();
  }
}
async function stopTask(task) {
  if (!task) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(task.id)}/cancel`, {
      method: "POST",
    });
    await refreshChat();
  } catch (error) {
    toast(error.message, true);
  }
}
async function loadOlder(v) {
  const scroller = $("#chat-messages");
  const height = scroller.scrollHeight,
    top = scroller.scrollTop;
  $("#chat-older").disabled = true;
  try {
    const page = await api(
      `${sessionUrl(v.id)}/messages?before=${encodeURIComponent(v.messages[0].id)}&limit=50`,
      { signal: v.abort.signal },
    );
    if (view !== v) return;
    const ids = new Set(v.messages.map((m) => m.id));
    v.turnTasks = [
      ...v.turnTasks.filter((t) => !page.tasks.some((n) => n.id === t.id)),
      ...page.tasks,
    ];
    v.messages = [
      ...page.messages.filter((m) => !ids.has(m.id)),
      ...v.messages,
    ];
    v.hasMore = page.has_more;
    v.olderExhausted = !page.has_more;
    v.follow = false;
    updateMessages();
    scroller.scrollTop = top + scroller.scrollHeight - height;
  } catch (error) {
    if (view === v) {
      toast(error.message, true);
      $("#chat-older").disabled = false;
    }
  }
}
async function markRead(v) {
  const last = v.messages.at(-1);
  if (!last || !v.follow || document.hidden || v.readId === last.id) return;
  await api(
    `${sessionUrl(v.id)}/read`,
    jsonOptions("POST", { message_id: last.id }),
  );
  if (view !== v) return;
  v.readId = last.id;
  v.session.unread_count = 0;
}
function connectTasks(v) {
  const wanted = new Set(v.tasks.map((t) => t.id));
  for (const [id, socket] of v.sockets)
    if (!wanted.has(id)) {
      socket.close();
      v.sockets.delete(id);
      v.live.delete(id);
    }
  for (const task of v.tasks) {
    if (v.sockets.has(task.id)) continue;
    const socket = new WebSocket(
      `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/tasks/${encodeURIComponent(task.id)}/events`,
    );
    v.sockets.set(task.id, socket);
    const delivered = new Set();
    const live = { text: "", detail: "" };
    v.live.set(task.id, live);
    socket.onmessage = (event) => {
      if (view !== v) return;
      const data = JSON.parse(event.data);
      if (!data.id || delivered.has(data.id)) return;
      delivered.add(data.id);
      if (data.event_type === "agent.text")
        live.text += data.payload?.text || "";
      if (data.event_type === "agent.message")
        live.detail = data.payload?.content || "";
      if (["agent.tool_call", "agent.tool"].includes(data.event_type))
        live.detail = data.payload?.name || "正在使用工具…";
      if (data.event_type.startsWith("task.")) refreshChat().catch(() => {});
      else updateMessages(true);
    };
    socket.onclose = () => {
      if (view === v && v.sockets.get(task.id) === socket)
        v.sockets.delete(task.id);
    };
  }
}
function showAgentPicker() {
  const v = view;
  if (!v) return;
  v.menuAbort?.abort();
  $(".chat-menu")?.remove();
  const dialog = document.createElement("div");
  dialog.className = "chat-menu chat-agent-picker";
  dialog.innerHTML =
    '<input class="chat-agent-search" aria-label="搜索 Agent" placeholder="搜索 Agent…"><div class="chat-agent-options"></div>';
  document.body.append(dialog);
  const rect = $("#chat-new").getBoundingClientRect();
  dialog.style.left = `${Math.max(8, Math.min(rect.right - 256, innerWidth - 264))}px`;
  dialog.style.top = `${rect.bottom + 5}px`;
  const abort = new AbortController();
  v.menuAbort = abort;
  dialog.close = () => {
    dialog.remove();
    abort.abort();
    $("#chat-new")?.focus();
  };
  document.addEventListener(
    "pointerdown",
    (event) => {
      if (
        !dialog.contains(event.target) &&
        !$("#chat-new").contains(event.target)
      )
        dialog.close();
    },
    { signal: abort.signal },
  );
  dialog.onkeydown = (event) => {
    if (event.key === "Escape") dialog.close();
  };
  function render(query = "") {
    const agents = state.agents.filter((a) =>
      a.name.toLowerCase().includes(query.toLowerCase()),
    );
    $(".chat-agent-options", dialog).innerHTML =
      agents
        .map(
          (a) =>
            `<button data-agent="${esc(a.id)}">${avatar(a)}<span>${esc(a.name)}<small>${esc(a.provider)}</small></span></button>`,
        )
        .join("") || '<p class="chat-list-empty">没有可用的 Agent</p>';
    $$("[data-agent]", dialog).forEach(
      (button) =>
        (button.onclick = () => {
          const agent = agentFor(button.dataset.agent);
          dialog.close();
          if (v.id) {
            pendingAgent = agent;
            location.hash = "chat";
          } else beginDraft(agent);
        }),
    );
  }
  render();
  $(".chat-agent-search", dialog).focus();
  $(".chat-agent-search", dialog).oninput = (e) => render(e.target.value);
}
function beginDraft(agent) {
  if (!view) return;
  view.archived = false;
  renderList();
  view.session = null;
  view.draftAgent = agent;
  view.draftKey = `new.${agent.id}`;
  view.messages = [];
  view.tasks = [];
  view.turnTasks = [];
  view.request = null;
  renderConversation();
  $("#chat-input").focus();
}
function openDialog(heading, body, footer = "") {
  view.dialog?.remove();
  const dialog = document.createElement("dialog");
  dialog.className = "chat-dialog";
  dialog.innerHTML = `<h2>${esc(heading)}</h2>${body}${footer || '<div class="chat-dialog-footer"><button class="secondary-button" data-close>取消</button></div>'}`;
  document.body.append(dialog);
  view.dialog = dialog;
  $("[data-close]", dialog).onclick = () => dialog.close();
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.showModal();
  return dialog;
}
function showMenu(session, trigger) {
  $(".chat-menu")?.remove();
  const menu = document.createElement("div");
  menu.className = "chat-menu";
  menu.setAttribute("role", "menu");
  const busy = session.pending_tasks.length > 0;
  const actions =
    session.status === "archived"
      ? [
          ["restore", "恢复会话"],
          ["delete", "删除会话"],
        ]
      : [
          ["rename", "重命名"],
          ["profile", "查看 Agent"],
          ["pin", session.pinned ? "取消置顶" : "置顶"],
          ...(busy ? [["stop", "停止执行"]] : [["archive", "归档会话"]]),
        ];
  menu.innerHTML = actions
    .map(
      ([action, label]) =>
        `<button role="menuitem" data-action="${action}" class="${action === "delete" ? "destructive" : ""}">${esc(label)}</button>`,
    )
    .join("");
  document.body.append(menu);
  const rect = trigger.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(rect.right - 156, innerWidth - 164))}px`;
  menu.style.top = `${Math.min(rect.bottom + 5, innerHeight - menu.offsetHeight - 8)}px`;
  const abort = new AbortController();
  view.menuAbort?.abort();
  view.menuAbort = abort;
  const close = () => {
    menu.remove();
    abort.abort();
    trigger.focus();
  };
  document.addEventListener(
    "pointerdown",
    (e) => {
      if (!menu.contains(e.target) && !trigger.contains(e.target)) close();
    },
    { signal: abort.signal },
  );
  menu.onkeydown = (e) => {
    if (e.key === "Escape") close();
    if (["ArrowDown", "ArrowUp"].includes(e.key)) {
      e.preventDefault();
      const buttons = $$("button", menu),
        index = buttons.indexOf(document.activeElement);
      buttons[
        (index + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) %
          buttons.length
      ].focus();
    }
  };
  $$("[data-action]", menu).forEach(
    (button) =>
      (button.onclick = () => {
        const action = button.dataset.action;
        close();
        if (action === "rename") renameSession(session);
        else if (action === "delete") deleteSession(session);
        else if (action === "stop")
          stopTask(active(session.pending_tasks) || session.pending_tasks[0]);
        else if (action === "profile") {
          const agent = agentFor(session.agent_id);
          if (agent)
            openDialog(
              agent.name,
              `<p>${esc(agent.provider)} · ${esc(state.runtimes.find((r) => r.id === agent.runtime_id)?.name || agent.runtime_id)}</p><div class="chat-agent-instructions markdown">${markdown(agent.instructions || "暂无说明")}</div>`,
            );
        } else
          mutate(
            session,
            action === "pin"
              ? { pinned: !session.pinned }
              : { archived: action === "archive" },
          );
      }),
  );
  $("button", menu).focus();
}
async function mutate(session, body) {
  try {
    await api(sessionUrl(session.id), jsonOptions("PATCH", body));
    if (body.archived && view?.id === session.id) {
      const next = view.sessions.find(
        (s) => s.id !== session.id && s.status !== "archived",
      );
      location.hash = next ? `chat/${encodeURIComponent(next.id)}` : "chat";
    } else {
      if (view?.id === session.id) view.signature = null;
      await refreshChat();
    }
  } catch (error) {
    toast(error.message, true);
  }
}
function renameSession(session) {
  if (session.id !== view.id) {
    const dialog = openDialog(
      "重命名会话",
      `<input id="chat-title-input" aria-label="会话标题" maxlength="200" value="${esc(title(session))}">`,
      '<div class="chat-dialog-footer"><button class="secondary-button" data-close>取消</button><button class="primary-button" data-save>保存</button></div>',
    );
    $("[data-save]", dialog).onclick = async () => {
      const value = $("#chat-title-input").value.trim();
      if (value) {
        await mutate(session, { title: value });
        dialog.close();
      }
    };
    return;
  }
  const button = $("#chat-rename");
  const input = document.createElement("input");
  input.className = "chat-title-input";
  input.maxLength = 200;
  input.value = session.title;
  input.setAttribute("aria-label", "会话标题");
  button.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    if (save && input.value.trim() && input.value.trim() !== session.title)
      await mutate(session, { title: input.value.trim() });
    if (input.isConnected) input.replaceWith(button);
  };
  input.onkeydown = (e) => {
    if (!e.isComposing && e.key === "Enter") {
      e.preventDefault();
      finish(true);
    }
    if (e.key === "Escape") finish(false);
  };
  input.onblur = () => finish(true);
}
function deleteSession(session) {
  const dialog = openDialog(
    "删除会话？",
    `<p>${esc(title(session))}</p>`,
    '<div class="chat-dialog-footer"><button class="secondary-button" data-close>取消</button><button class="chat-danger" data-delete>删除</button></div>',
  );
  const button = $("[data-delete]", dialog);
  button.onclick = async () => {
    button.disabled = true;
    $("[data-close]", dialog).disabled = true;
    dialog.oncancel = (e) => e.preventDefault();
    button.textContent = "删除中…";
    try {
      await api(sessionUrl(session.id), { method: "DELETE" });
      dialog.close();
      toast("会话已删除");
      if (view?.id === session.id) location.hash = "chat";
      else await refreshChat();
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
      $("[data-close]", dialog).disabled = false;
      dialog.oncancel = null;
      button.textContent = "删除";
    }
  };
}
function bindResize(v) {
  const layout = $(".chat-layout"),
    handle = $(".chat-resizer");
  let width = Number(storageGet("mica.chat.list-width", "260"));
  const setWidth = (value) => {
    width = Math.min(480, Math.max(240, value));
    layout.style.setProperty("--chat-list-width", `${width}px`);
    handle.setAttribute("aria-valuenow", width);
  };
  handle.setAttribute("aria-valuemin", "240");
  handle.setAttribute("aria-valuemax", "480");
  setWidth(width);
  handle.onpointerdown = (e) => {
    handle.setPointerCapture(e.pointerId);
    const origin = e.clientX,
      start = width;
    handle.onpointermove = (event) => setWidth(start + event.clientX - origin);
    handle.onpointerup = () => {
      handle.onpointermove = null;
      storageSet("mica.chat.list-width", width);
    };
  };
  handle.onkeydown = (e) => {
    if (["ArrowLeft", "ArrowRight"].includes(e.key)) {
      e.preventDefault();
      setWidth(width + (e.key === "ArrowLeft" ? -16 : 16));
      storageSet("mica.chat.list-width", width);
    }
  };
}
