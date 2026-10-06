import { $, $$, esc, badge, toast, formatTime } from "./ui.js";
import {
  state,
  ISSUE_COLUMNS,
  ISSUE_LABEL,
  TASK_LABEL,
  ACTIVE_TASKS,
  USER_MOVES,
  assignee,
} from "./state.js";
import { api, jsonOptions } from "./api.js";
import { showCreateIssue } from "./forms.js?v=delete-ui-v6";
import { openIssueMenu } from "./issue-actions.js?v=delete-ui-v6";

let refresh, openIssue, render;

export function configureBoard(actions) {
  ({ refresh, openIssue, render } = actions);
}

function tasksFor(issueId) {
  return state.tasks
    .filter((task) => task.issue_id === issueId)
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
}
function issueCard(issue) {
  const owner = assignee(issue),
    tasks = tasksFor(issue.id),
    latest = tasks[0],
    waiting = tasks.some((t) => t.status === "waiting_input"),
    failed = latest?.status === "failed";
  const squadTasks = tasks.filter((t) => !t.is_leader_task),
    finished = squadTasks.filter((t) =>
      ["completed", "failed", "cancelled"].includes(t.status),
    ).length;
  const note = waiting
    ? `<div class="status-note">Agent 正在等待用户补充信息</div>`
    : failed
      ? `<div class="status-note error">${esc(latest.error || "最近一次执行失败")}</div>`
      : "";
  const progress =
    owner.type === "squad" && squadTasks.length
      ? `<div class="progress" title="${finished}/${squadTasks.length} 个成员任务已结束"><span style="width:${(finished / squadTasks.length) * 100}%"></span></div>`
      : "";
  return `<article class="issue-card" role="button" tabindex="0" draggable="true" data-issue-id="${esc(issue.id)}" data-status="${esc(issue.status)}">
    <div class="card-top"><span class="card-kind">${owner.type === "squad" ? "Squad" : owner.type === "remote" ? "远程 Agent" : "Agent"}</span>${latest ? badge(TASK_LABEL[latest.status] || latest.status, waiting ? "warning" : failed ? "danger" : "neutral") : ""}</div>
    <h3 class="card-title">${esc(issue.title)}</h3>${note}${progress}
    <div class="card-foot"><span class="assignee"><span class="assignee-avatar" aria-hidden="true">${esc(owner.name.slice(0, 1).toUpperCase())}</span>${esc(owner.name)}</span><span>${formatTime(issue.updated_at)}</span></div>
  </article>`;
}
export function renderBoard() {
  const visible = state.issues.filter(
    (i) =>
      i.status !== "cancelled" &&
      (!state.search ||
        `${i.title} ${assignee(i).name}`
          .toLowerCase()
          .includes(state.search.toLowerCase())),
  );
  const active = state.tasks.filter((t) => ACTIVE_TASKS.has(t.status)).length,
    waiting = state.tasks.filter((t) => t.status === "waiting_input").length;
  $("#page").innerHTML =
    `<div class="board-workspace"><div class="board-toolbar"><div class="stats"><span class="stat-chip"><strong>${visible.length}</strong>任务</span><span class="stat-chip"><strong>${active}</strong>活动执行</span><span class="stat-chip"><strong>${waiting}</strong>等待输入</span></div><div class="search-box"><input id="board-search" aria-label="搜索任务或负责人" value="${esc(state.search)}" placeholder="搜索任务或负责人"></div></div><div class="board-scroller" id="board-scroll" tabindex="0" aria-label="任务状态看板"><div class="board">${ISSUE_COLUMNS.map(
      ([status, label]) => {
        const items = visible.filter((i) => i.status === status);
        return `<section class="board-column ${status}" data-drop-status="${status}" aria-label="${label}，${items.length} 个任务"><header class="column-head"><span class="column-title"><i class="column-dot ${status}"></i>${label}<span class="column-count">${items.length}</span></span>${status === "todo" ? `<button class="column-add" data-column-create="${status}" aria-label="创建并执行任务" title="创建并执行任务">＋</button>` : ""}</header><div class="column-body" data-column-scroll="${status}">${items.map(issueCard).join("") || '<p class="empty-column">暂无任务</p>'}</div></section>`;
      },
    ).join("")}</div></div></div>`;
  const scroller = $("#board-scroll");
  let pointer = null;
  scroller.onpointerdown = (event) => {
    if (
      event.button !== 0 ||
      event.pointerType !== "mouse" ||
      event.target.closest("button,a,input,select,[role=button]")
    )
      return;
    pointer = { id: event.pointerId, x: event.clientX, moved: false };
    scroller.setPointerCapture(event.pointerId);
  };
  scroller.onpointermove = (event) => {
    if (!pointer || event.pointerId !== pointer.id) return;
    const dx = event.clientX - pointer.x;
    if (!pointer.moved && Math.abs(dx) < 5) return;
    pointer.moved = true;
    scroller.classList.add("panning");
    scroller.scrollLeft -= dx;
    pointer.x = event.clientX;
  };
  const stopPan = () => {
    if (pointer && scroller.hasPointerCapture(pointer.id))
      scroller.releasePointerCapture(pointer.id);
    pointer = null;
    scroller.classList.remove("panning");
  };
  scroller.onpointerup = stopPan;
  scroller.onpointercancel = stopPan;
  scroller.onlostpointercapture = stopPan;
  $$("[data-column-scroll]").forEach((column) => {
    column.scrollTop = state.boardColumns?.[column.dataset.columnScroll] || 0;
    column.onscroll = () => {
      state.boardColumns ||= {};
      state.boardColumns[column.dataset.columnScroll] = column.scrollTop;
    };
  });
  $("#board-scroll").addEventListener("scroll", () => {
    state.boardScroll.left = $("#board-scroll").scrollLeft;
  });
  $("#board-search").addEventListener("input", (event) => {
    state.search = event.target.value;
    state.boardScroll = {
      top: $("#page").scrollTop,
      left: $("#board-scroll").scrollLeft,
    };
    render();
  });
  setupBoardEvents();
}
function setupBoardEvents() {
  $$("[data-column-create]").forEach(
    (button) => (button.onclick = showCreateIssue),
  );
  $$(".issue-card").forEach((card) => {
    const getIssue = () => state.issues.find((issue) => issue.id === card.dataset.issueId);
    card.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      const issue = getIssue();
      if (issue) openIssueMenu(issue, card, { x: event.clientX, y: event.clientY });
    });
    card.addEventListener("click", () => openIssue(card.dataset.issueId));
    card.addEventListener("keydown", (event) => {
      if (event.target !== card) return;
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault();
        const issue = getIssue();
        if (issue) openIssueMenu(issue, card);
      }
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault();
        openIssue(card.dataset.issueId);
      }
    });
    card.addEventListener("dragstart", (event) => {
      if (event.target.closest("button")) { event.preventDefault(); return; }
      card.classList.add("dragging");
      event.dataTransfer.setData("text/plain", card.dataset.issueId);
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
  });
  $$(".board-column").forEach((column) => {
    column.addEventListener("dragover", (event) => {
      event.preventDefault();
      column.classList.add("drag-over");
    });
    column.addEventListener("dragleave", () =>
      column.classList.remove("drag-over"),
    );
    column.addEventListener("drop", async (event) => {
      event.preventDefault();
      column.classList.remove("drag-over");
      await moveIssue(
        event.dataTransfer.getData("text/plain"),
        column.dataset.dropStatus,
      );
    });
  });
}
async function moveIssue(id, target) {
  const issue = state.issues.find((i) => i.id === id);
  if (!issue || issue.status === target) return;
  if (!(USER_MOVES[issue.status] || []).includes(target)) {
    toast(
      `不能直接从“${ISSUE_LABEL[issue.status]}”移动到“${ISSUE_LABEL[target]}”`,
      true,
    );
    return;
  }
  try {
    await api(
      `/api/issues/${id}/status`,
      jsonOptions("PATCH", { status: target }),
    );
    await refresh();
    toast(`任务已移至“${ISSUE_LABEL[target]}”`);
  } catch (error) {
    toast(error.message, true);
  }
}
