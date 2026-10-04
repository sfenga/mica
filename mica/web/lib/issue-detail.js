import {
  $,
  $$,
  esc,
  badge,
  formatTime,
  storageGet,
  storageSet,
  toast,
} from "./ui.js";
import {
  state,
  assignee,
  agentById,
  ISSUE_LABEL,
  ACTIVE_TASKS,
  USER_MOVES,
} from "./state.js";
import { api, jsonOptions } from "./api.js";
import { markdown } from "./markdown.js";
import { commentThreads } from "./comments.js";
import { mountComposer } from "./composer.js";
import { executionCard, loadEvents } from "./executions.js";

import { issueMenuButton, bindIssueMenu } from "./issue-actions.js";

let current = null;
export function disposeIssue() {
  if (!current) return;
  current.disposed = true;
  clearTimeout(current.liveTimer);
  current.composer?.dispose();
  for (const entry of current.sockets.values()) {
    clearTimeout(entry.timer);
    entry.socket?.close();
  }
  current.sockets.clear();
  current = null;
}
export async function mountIssue(id) {
  disposeIssue();
  const propertyKey = "mica.properties";
  const view = {
    id,
    disposed: false,
    issue: null,
    tasks: [],
    composer: null,
    sockets: new Map(),
    loading: null,
    tab: "discussion",
    propertyKey,
    properties: storageGet(propertyKey, "open") === "open",
    commentsKey: "",
    tasksKey: "",
  };
  current = view;
  $("#page").innerHTML =
    '<div class="loading-state"><span class="spinner"></span>正在加载任务…</div>';
  $("#page").scrollTop = 0;
  window.scrollTo(0, 0);
  await fetchIssue(view, true);
}
export async function refreshIssue() {
  if (current) await fetchIssue(current);
}
async function fetchIssue(view, initial = false) {
  if (view.disposed) return;
  if (view.loading) {
    await view.loading;
    return;
  }
  view.loading = (async () => {
    try {
      const [issue, tasks] = await Promise.all([
        api(`/api/issues/${encodeURIComponent(view.id)}`),
        api(`/api/tasks?issue_id=${encodeURIComponent(view.id)}`),
      ]);
      if (view.disposed) return;
      view.issue = issue;
      view.tasks = tasks;
      if (initial || !$("#issue-view")) buildShell(view);
      updateView(view);
      syncSockets(view);
      // Open active timelines refresh with polling and live updates, even if task metadata is unchanged.
      for (const detail of $$("[data-task-events][open]")) {
        const task = tasks.find(
          (item) => item.id === detail.dataset.taskEvents,
        );
        const events = detail.querySelector(".event-list");
        if (
          task &&
          ACTIVE_TASKS.has(task.status) &&
          events?.dataset.loaded === "1"
        ) {
          delete events.dataset.loaded;
          loadEvents(task.id);
        }
      }
      $("#detail-error").hidden = true;
      state.issues = state.issues.map((item) =>
        item.id === issue.id ? issue : item,
      );
      state.tasks = [
        ...state.tasks.filter((item) => item.issue_id !== issue.id),
        ...tasks,
      ];
    } catch (error) {
      if (view.disposed) return;
      if (!view.issue) {
        $("#page").innerHTML =
          `<div class="empty-state"><div><strong>任务加载失败</strong><p>${esc(error.message)}</p><button class="primary-button" id="retry-detail">重新加载</button> <a class="secondary-button" href="#board">返回看板</a></div></div>`;
        $("#retry-detail").onclick = () => fetchIssue(view, true);
      } else {
        $("#detail-error").hidden = false;
        $("#detail-error-message").textContent =
          `更新失败：${error.message}。正在保留当前内容，稍后自动重试。`;
      }
    } finally {
      view.loading = null;
    }
  })();
  await view.loading;
}
function buildShell(view) {
  $("#page").innerHTML = `<div class="issue-view" id="issue-view">
    <div class="issue-heading"><a class="back-link" href="#board">← 返回任务看板</a><div class="issue-heading-actions">${issueMenuButton(view.id)}<button class="secondary-button small-button" id="toggle-properties" aria-controls="issue-properties">任务属性</button></div></div>
    <div class="detail-error" id="detail-error" role="status" hidden><span id="detail-error-message"></span><button class="text-button" id="retry-detail">重试</button></div>
    <header class="issue-title-block"><div id="issue-status"></div><h2 id="issue-title"></h2><div class="issue-subtitle"><span id="issue-owner"></span><span id="detail-connection">正在连接实时更新…</span></div></header>
    <div class="issue-layout"><section class="discussion-main">
      <nav class="detail-tabs" role="tablist" aria-label="任务详情"><button id="discussion-tab" role="tab" aria-controls="discussion-panel" data-tab="discussion">讨论 <span id="comment-count"></span></button><button id="execution-tab" role="tab" aria-controls="execution-panel" data-tab="execution">执行 <span id="execution-count"></span></button></nav>
      <div id="discussion-panel" role="tabpanel" aria-labelledby="discussion-tab"><section class="issue-goal"><div class="markdown" id="issue-body"></div></section><div class="discussion-heading"><h3>讨论与追问</h3><span>用户与 Agent 的协作记录</span></div><div id="comments" class="comments"></div></div>
      <div id="execution-panel" role="tabpanel" aria-labelledby="execution-tab" hidden><div class="execution-intro"><h3>执行记录</h3><p>查看各轮 Agent 的输出、交接信息与运行事件。</p></div><div class="execution-list" id="executions"></div></div>
      <div class="composer-dock" id="composer-dock"></div><div class="closed-discussion" id="closed-discussion" hidden>当前状态仅供阅读；草稿会保留。</div>
    </section><aside id="issue-properties" class="issue-properties"><h3>任务属性</h3><div id="issue-facts" class="detail-facts"></div><div id="issue-actions" class="detail-actions"></div><p class="property-hint">执行完成后确认结果，或提出修改要求继续协作。</p></aside></div>
  </div>`;
  bindIssueMenu($("[data-issue-menu]"), () => view.issue);
  view.composer = mountComposer($("#composer-dock"), {
    issueId: view.id,
    context: () => ({ issue: view.issue, tasks: view.tasks }),
    onSent: async () => {
      await refreshIssue();
      if (!view.disposed)
        $("#comments").lastElementChild?.scrollIntoView({
          block: "nearest",
          behavior: "smooth",
        });
    },
  });
  $("#retry-detail").onclick = () => refreshIssue();
  $("#toggle-properties").onclick = () => {
    view.properties = !view.properties;
    storageSet(view.propertyKey, view.properties ? "open" : "closed");
    applyTabs(view);
  };
  $$("[data-tab]").forEach((button) => {
    button.onclick = () => {
      view.tab = button.dataset.tab;
      applyTabs(view);
    };
    button.onkeydown = (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
        return;
      event.preventDefault();
      view.tab = view.tab === "discussion" ? "execution" : "discussion";
      applyTabs(view);
      $(`[data-tab="${view.tab}"]`).focus();
    };
  });
  $("#comments").onclick = (event) => {
    const button = event.target.closest("[data-reply-comment]");
    if (button) view.composer.reply(button.dataset.replyComment);
    const execution = event.target.closest("[data-comment-task]");
    if (execution) {
      view.tab = "execution";
      applyTabs(view);
      const card = $(
        `[data-task-events="${CSS.escape(execution.dataset.commentTask)}"]`,
      );
      if (card) {
        card.open = true;
        card.scrollIntoView({ block: "nearest" });
      }
    }
  };
  $("#issue-facts").onchange = (event) => {
    if (event.target.id !== "property-status") return;
    const control = event.target;
    mutate(
      view,
      control,
      `/api/issues/${view.id}/status`,
      jsonOptions("PATCH", { status: control.value }),
      "任务状态已更新",
    ).then(() => {
      if (control.isConnected) control.value = view.issue.status;
    });
  };
  $("#issue-actions").onclick = (event) => {
    const button = event.target.closest("[data-status-action]");
    if (button)
      mutate(
        view,
        button,
        `/api/issues/${view.id}/status`,
        jsonOptions("PATCH", { status: button.dataset.statusAction }),
        "任务状态已更新",
      );
  };
  $("#executions").onclick = (event) => {
    const button = event.target.closest("[data-cancel-task]");
    if (button)
      mutate(
        view,
        button,
        `/api/tasks/${button.dataset.cancelTask}/cancel`,
        { method: "POST" },
        "已请求取消执行",
      );
  };
}
function applyTabs(view) {
  $$("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === view.tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $("#discussion-panel").hidden = view.tab !== "discussion";
  $("#execution-panel").hidden = view.tab !== "execution";
  const writable = !["done", "cancelled", "backlog"].includes(
    view.issue.status,
  );
  $("#composer-dock").hidden = view.tab !== "discussion" || !writable;
  $("#closed-discussion").hidden = view.tab !== "discussion" || writable;
  $("#issue-properties").hidden = !view.properties;
  $("#toggle-properties").setAttribute(
    "aria-expanded",
    String(view.properties),
  );
  $("#toggle-properties").textContent = view.properties
    ? "收起属性 ⇥"
    : "显示属性 ⇤";
  $(".issue-layout").classList.toggle("properties-hidden", !view.properties);
}
function updateView(view) {
  const { issue, tasks } = view,
    owner = assignee(issue),
    comments = issue.comments || [];
  $("#issue-title").textContent = issue.title;
  document.title = `${issue.title} · Mica`;
  $("#issue-status").innerHTML = badge(
    ISSUE_LABEL[issue.status],
    issue.status === "blocked"
      ? "danger"
      : issue.status === "in_review"
        ? "warning"
        : "neutral",
  );
  $("#issue-owner").textContent =
    `${owner.name} · ${owner.provider.toUpperCase()}`;
  const goal = markdown(issue.body);
  if ($("#issue-body").innerHTML !== goal) $("#issue-body").innerHTML = goal;
  $("#comment-count").textContent = comments.length;
  $("#execution-count").textContent = tasks.length;
  const commentsKey = JSON.stringify([
    comments,
    issue.status,
    state.agents,
    state.squads,
    tasks.map((task) => [task.id, task.status]),
  ]);
  if (commentsKey !== view.commentsKey) {
    const writable = !["backlog", "done", "cancelled"].includes(issue.status);
    const names = new Map(
      comments.map((comment) => [comment.id, authorName(comment)]),
    );
    $("#comments").innerHTML = comments.length
      ? commentThreads(comments)
          .map(
            (thread) =>
              `<section class="comment-thread" aria-label="${esc(authorName(thread.root))} 发起的讨论">${[thread.root, ...thread.replies].map((comment) => commentCard(comment, names, writable, tasks)).join("")}</section>`,
          )
          .join("")
      : '<div class="discussion-empty"><span>⌁</span><strong>从这里开始协作</strong><p>补充任务背景，或 @ 一个 Agent 提出具体问题。</p></div>';
    view.commentsKey = commentsKey;
  }
  const mode = issue.project_path
    ? issue.project_execution_mode === "in_place"
      ? "当前目录"
      : "独立 Worktree"
    : "独立空工作目录";
  const allowed = USER_MOVES[issue.status] || [];
  const statusControl = `<div class="property-row"><span>状态</span><select id="property-status" class="property-status" aria-label="任务状态" ${allowed.length ? "" : "disabled"}>${[issue.status, ...allowed].map((status) => `<option value="${status}" ${status === issue.status ? "selected" : ""}>${esc(ISSUE_LABEL[status])}</option>`).join("")}</select></div>`;
  $("#issue-facts").innerHTML =
    statusControl +
    [
      ["负责人", owner.name],
      [
        "执行类型",
        owner.type === "squad"
          ? "Squad"
          : owner.type === "remote"
            ? "远程 Agent"
            : "本地 Agent",
      ],
      ["Provider", owner.provider],
      ["项目路径", issue.project_path || "未关联项目"],
      ["目录策略", mode],
      ["创建时间", formatTime(issue.created_at)],
      ["更新时间", formatTime(issue.updated_at)],
    ]
      .map(
        ([name, value]) =>
          `<div class="property-row"><span>${esc(name)}</span><strong>${esc(value)}</strong></div>`,
      )
      .join("");
  const actions =
    issue.status === "in_review"
      ? [
          ["确认完成", "done", "primary"],
          ["要求返工", "in_progress"],
          ["标记阻塞", "blocked"],
        ]
      : issue.status === "blocked"
        ? [
            ["重新排队", "todo"],
            ["恢复执行", "in_progress"],
          ]
        : issue.status === "backlog"
          ? [["开始执行", "todo", "primary"]]
          : [];
  if (!["done", "cancelled"].includes(issue.status))
    actions.push(["取消任务", "cancelled", "danger"]);
  $("#issue-actions").innerHTML = actions
    .map(
      ([label, status, kind = "secondary"]) =>
        `<button class="${kind}-button small-button" data-status-action="${status}">${label}</button>`,
    )
    .join("");
  const tasksKey = JSON.stringify(tasks);
  if (tasksKey !== view.tasksKey) {
    const opened = new Set(
      $$("details[open]", $("#executions")).map(
        (item) => item.dataset.taskEvents,
      ),
    );
    $("#executions").innerHTML =
      [...tasks]
        .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))
        .map(executionCard)
        .join("") ||
      '<div class="discussion-empty"><strong>尚无执行记录</strong><p>任务开始执行后，记录会出现在这里。</p></div>';
    $$("[data-task-events]").forEach((detail) => {
      detail.addEventListener(
        "toggle",
        () => detail.open && loadEvents(detail.dataset.taskEvents),
      );
      if (opened.has(detail.dataset.taskEvents)) detail.open = true;
    });
    view.tasksKey = tasksKey;
  }
  view.composer.update();
  applyTabs(view);
}
function commentCard(comment, names, writable, tasks) {
  const task = tasks.find((item) => item.id === comment.source_task_id);
  const isAgent = comment.author_type === "agent";
  const name = authorName(comment),
    role = isAgent ? "AI Agent" : "用户";
  const icon = isAgent
    ? '<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M9 3h6M1 12v4M23 12v4M9 16h6"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/>'
    : '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>';
  return `<article class="comment-card comment-${isAgent ? "agent" : "user"}${comment.parent_comment_id ? " is-reply" : ""}" id="comment-${esc(comment.id)}" aria-label="${esc(`${name} · ${role}`)}">
    <header class="comment-header"><div class="avatar ${isAgent ? "agent" : "user"}" aria-hidden="true"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${icon}</svg></div><strong>${esc(name)}</strong>${isAgent ? badge(role, "local") : ""}<time datetime="${esc(comment.created_at)}">${formatTime(comment.created_at)}</time>${task ? `<button class="comment-execution-link" data-comment-task="${esc(task.id)}">${esc(task.status === "completed" ? "执行已完成" : task.status === "failed" ? "执行失败" : "查看执行")} ↗</button>` : ""}</header>
    <div class="comment-body">${comment.parent_comment_id ? `<div class="comment-reply-context">↳ 回复 ${esc(names.get(comment.parent_comment_id) || "此前评论")}</div>` : ""}<div class="markdown">${markdown(comment.body)}</div>${writable ? `<button class="text-button comment-reply" data-reply-comment="${esc(comment.id)}">↩ 回复</button>` : ""}</div>
  </article>`;
}

function authorName(comment) {
  return comment.author_type === "agent"
    ? agentById(comment.author_id)?.name || comment.author_id
    : comment.author_id === "local-user"
      ? "你"
      : comment.author_id;
}
async function mutate(view, button, url, options, message) {
  if (button.disabled || view.mutating) return;
  view.mutating = true;
  button.disabled = true;
  try {
    await api(url, options);
    toast(message);
    await refreshIssue();
  } catch (error) {
    toast(error.message, true);
  } finally {
    view.mutating = false;
    if (button.isConnected) button.disabled = false;
  }
}
function syncSockets(view) {
  const active = new Set(
    view.tasks
      .filter((task) => ACTIVE_TASKS.has(task.status))
      .map((task) => task.id),
  );
  for (const [id, entry] of view.sockets) {
    if (!active.has(id)) {
      clearTimeout(entry.timer);
      view.sockets.delete(id);
      entry.socket?.close();
    }
  }
  for (const id of active)
    if (!view.sockets.has(id)) {
      const entry = { socket: null, attempts: 0, timer: null };
      view.sockets.set(id, entry);
      connect(view, id, entry);
    }
  connectionLabel(view);
}
function connectionLabel(view) {
  if (view.disposed || !$("#detail-connection")) return;
  const connected = [...view.sockets.values()].some(
    (entry) => entry.socket?.readyState === WebSocket.OPEN,
  );
  $("#detail-connection").textContent = !view.sockets.size
    ? "每 8 秒自动更新"
    : connected
      ? "● 实时更新已连接"
      : "连接恢复中 · 自动更新仍在继续";
}
function connect(view, id, entry) {
  if (view.disposed || !view.sockets.has(id)) return;
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(
    `${protocol}://${location.host}/api/tasks/${encodeURIComponent(id)}/events`,
  );
  entry.socket = socket;
  socket.onopen = () => connectionLabel(view);
  socket.onmessage = (event) => {
    entry.attempts = 0;
    try {
      if (JSON.parse(event.data).event_type === "heartbeat") return;
    } catch {
      return;
    }
    if (view.liveTimer) return;
    view.liveTimer = setTimeout(() => {
      view.liveTimer = null;
      if (!view.disposed) fetchIssue(view);
    }, 500);
  };
  socket.onerror = () => socket.close();
  socket.onclose = () => {
    if (view.disposed || view.sockets.get(id) !== entry) return;
    connectionLabel(view);
    entry.timer = setTimeout(
      () => connect(view, id, entry),
      Math.min(15000, 1000 * 2 ** entry.attempts++),
    );
  };
}
