import {
  $,
  $$,
  esc,
  badge,
  toast,
  formatTime,
  storageGet,
  storageSet,
  empty,
} from "./lib/ui.js";
import { state } from "./lib/state.js";
import { api, loadData } from "./lib/api.js";
import { mountIssue, refreshIssue, disposeIssue } from "./lib/issue-detail.js?v=delete-ui-v6";

import { renderBoard, configureBoard } from "./lib/board.js?v=delete-ui-v6";
import {
  renderAgents,
  renderSquads,
  renderRuntimeNodes,
  bindRegistryKeys,
} from "./lib/registry.js?v=delete-ui-v6";
import {
  configureForms,
  showCreateIssue,
  showCreateSquad,
  showAgentForm,
  probeConfiguredRuntime,
  closeModal,
} from "./lib/forms.js?v=delete-ui-v6";

configureForms({ refresh, openIssue, openDrawer, onIssueDeleted });
async function onIssueDeleted(id) {
  if (state.selectedIssue === id) {
    disposeIssue();
    state.selectedIssue = null;
    state.route = "board";
    location.hash = "board";
  }
  render();
  await refresh();
}
configureBoard({ refresh, openIssue, render });

const routeMeta = {
  board: ["MISSION CONTROL", "任务看板", "＋ 新建任务", "issue"],
  agents: ["AGENT REGISTRY", "Agents", "＋ 注册 Agent", "agent"],
  squads: ["COLLABORATION", "Squads", "＋ 组装 Squad", "squad"],
  runtimes: ["INFRASTRUCTURE", "运行环境", "↻ 探测本机", "runtime"],
};
async function checkHealth() {
  try {
    await api("/health/ready");
    $("#health-dot").className = "health-dot ready";
    $("#health-label").textContent = "服务已就绪";
  } catch (error) {
    $("#health-dot").className = "health-dot error";
    $("#health-label").textContent = "服务未就绪";
  }
}
function openIssue(id) {
  if (state.route === "board")
    state.boardScroll = {
      top: $("#page").scrollTop,
      left: $("#board-scroll")?.scrollLeft || 0,
    };
  location.hash = `issue/${encodeURIComponent(id)}`;
}
async function setRoute() {
  const hash = location.hash.slice(1);
  $("#page").classList.toggle("issue-page", hash.startsWith("issue/"));
  $("#page").classList.toggle("board-page", !hash || hash === "board");
  closeDrawer();
  if (hash.startsWith("issue/")) {
    let id;
    try {
      id = decodeURIComponent(hash.slice(6));
    } catch {
      location.hash = "board";
      return;
    }
    if (!id) {
      location.hash = "board";
      return;
    }
    state.route = "issue";
    state.selectedIssue = id;
    $("#page-eyebrow").textContent = "ISSUE WORKSPACE";
    $("#page-title").textContent = "任务详情";
    $("#primary-action").hidden = true;
    $$("[data-route]").forEach((link) =>
      link.classList.toggle("active", link.dataset.route === "board"),
    );
    await mountIssue(id);
    return;
  }
  disposeIssue();
  state.selectedIssue = null;
  state.route = routeMeta[hash] ? hash : "board";
  $("#primary-action").hidden = false;
  const [eyebrow, title, action] = routeMeta[state.route];
  $("#page-eyebrow").textContent = eyebrow;
  $("#page-title").textContent = title;
  $("#primary-action").textContent = action;
  document.title = `${title} · Mica`;
  $$("[data-route]").forEach((link) =>
    link.classList.toggle("active", link.dataset.route === state.route),
  );
  render();
}
function render() {
  if (state.route === "issue") return;
  const page = $("#page"),
    position =
      state.route === "board"
        ? state.boardScroll
        : { top: page.scrollTop, left: page.scrollLeft };
  const search = $("#board-search") || $("#resource-search"),
    searchId = search?.id,
    focused = document.activeElement === search,
    cursor = search?.selectionStart;
  ({
    board: renderBoard,
    agents: renderAgents,
    squads: renderSquads,
    runtimes: renderRuntimeNodes,
  })[state.route]();
  bindRegistryKeys();
  page.scrollTop = position.top;
  page.scrollLeft = state.route === "board" ? 0 : position.left;
  if (state.route === "board" && $("#board-scroll"))
    $("#board-scroll").scrollLeft = position.left;
  if (focused && searchId && $(`#${searchId}`)) {
    $(`#${searchId}`).focus();
    $(`#${searchId}`).setSelectionRange(cursor, cursor);
  }
}

function openDrawer(html) {
  $("#drawer-content").innerHTML = html;
  $("#drawer-backdrop").hidden = false;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawer-backdrop").hidden = true;
}

async function refresh() {
  await loadData();
  if (state.route === "issue") await refreshIssue();
  else render();
  checkHealth();
}
async function refreshIfChanged() {
  if (!$("#modal-backdrop").hidden) return;
  if (state.route === "issue") {
    await loadData();
    await refreshIssue();
    checkHealth();
    return;
  }
  const before = JSON.stringify([
    state.issues,
    state.tasks,
    state.agents,
    state.squads,
    state.runtimes,
    state.capabilities,
    state.projects,
    state.projectCandidates,
  ]);
  await loadData();
  const after = JSON.stringify([
    state.issues,
    state.tasks,
    state.agents,
    state.squads,
    state.runtimes,
    state.capabilities,
    state.projects,
    state.projectCandidates,
  ]);
  if (before !== after) render();
  checkHealth();
}
$("#page").addEventListener("scroll", () => {
  if (state.route === "board")
    state.boardScroll = {
      top: $("#page").scrollTop,
      left: $("#board-scroll")?.scrollLeft || 0,
    };
});
$("#primary-action").onclick = () =>
  ({
    issue: showCreateIssue,
    agent: showAgentForm,
    squad: showCreateSquad,
    runtime: probeConfiguredRuntime,
  })[routeMeta[state.route][3]]();
$("#refresh-button").onclick = async () => {
  try {
    await refresh();
    toast("数据已刷新");
  } catch (error) {
    toast(error.message, true);
  }
};
$("#drawer-backdrop").onclick = closeDrawer;
$("#modal-backdrop").addEventListener("pointerdown", (event) => {
  if (event.target === event.currentTarget && event.currentTarget.dataset.confirmation) event.preventDefault();
});
$("#modal-backdrop").addEventListener("click", (event) => {
  if (event.target === $("#modal-backdrop") && !event.currentTarget.dataset.confirmation) closeModal();
});
window.addEventListener("hashchange", setRoute);
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeModal();
    closeDrawer();
  }
  if (event.key === "Tab" && !$("#modal-backdrop").hidden) {
    const elements = $$(
      "button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)",
      $("#modal-content"),
    );
    if (!elements.length) {
      event.preventDefault();
      return;
    }
    const first = elements[0],
      last = elements.at(-1);
    if (!$("#modal-content").contains(document.activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first)?.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }
});
async function start() {
  try {
    await loadData();
    await setRoute();
    checkHealth();
    setInterval(
      () =>
        refreshIfChanged().catch(() => {
          $("#health-label").textContent = "更新失败，稍后重试";
          $("#health-dot").className = "health-dot error";
        }),
      8000,
    );
  } catch (error) {
    $("#page").innerHTML = empty(
      "工作台加载失败",
      `${esc(error.message)}<p><button class="primary-button" id="retry-start">重新加载</button></p>`,
    );
    $("#retry-start").onclick = start;
    checkHealth();
  }
}
start();
