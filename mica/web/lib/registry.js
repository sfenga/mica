import { $, $$, esc, badge, formatTime, empty } from "./ui.js";
import { state, agentById } from "./state.js";
import {
  showAgentForm,
  openSquad,
  showEditRuntime,
  probeRuntime,
} from "./forms.js?v=delete-ui-v6";
const searches = {};
const icons = {
  agent:
    '<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M8 12v2m8-2v2M9 17h6"/>',
  squad:
    '<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6m3 9v-2a6 6 0 0 0-3-5"/>',
  runtime:
    '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 17.5h.01M11 7.5h6M11 17.5h6"/>',
};
function nameCell(kind, name, description) {
  return `<div class="resource-name"><span class="avatar" aria-hidden="true"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${icons[kind]}</svg></span><div><strong>${esc(name)}</strong><small title="${esc(description)}">${esc(description)}</small></div></div>`;
}
function list(kind, count, head, rows) {
  $("#page").innerHTML =
    `<div class="resource-toolbar"><span>${count} ${kind === "agent" ? "个 Agent" : kind === "squad" ? "个协作小队" : "个运行节点"}</span><input id="resource-search" aria-label="搜索名称或配置" placeholder="搜索名称或配置" value="${esc(searches[kind] || "")}"></div><div class="resource-table"><div class="resource-head">${head.map((label) => `<span>${label}</span>`).join("")}</div>${rows || empty("暂无记录", "创建后将在这里显示。")}</div><p id="resource-no-match" class="field-hint" hidden>没有匹配的记录</p>`;
  const filter = () => {
    const query = $("#resource-search").value.toLowerCase();
    searches[kind] = $("#resource-search").value;
    const rows = $$("[data-resource-row]");
    rows.forEach((row) => {
      row.hidden = !row.textContent.toLowerCase().includes(query);
      const detail = row.nextElementSibling;
      if (detail?.classList.contains("resource-runtime-details"))
        detail.hidden = row.hidden;
    });
    $("#resource-no-match").hidden =
      !rows.length || rows.some((row) => !row.hidden);
  };
  $("#resource-search").oninput = filter;
  filter();
}
function configSummary(agent) {
  const c = agent.provider_config || {};
  if (agent.provider === "a2a")
    return c.skill_id || c.agent_card_url || "未指定 Card";
  if (agent.provider === "acp")
    return (c.command || []).join(" ") || "未指定命令";
  return c.model || c.executable || "默认配置";
}
export function renderAgents() {
  list(
    "agent",
    state.agents.length,
    ["Agent", "Provider", "运行节点", "配置"],
    state.agents
      .map(
        (agent) =>
          `<article class="resource-row entity-card clickable" data-resource-row role="button" tabindex="0" data-agent-id="${esc(agent.id)}">${nameCell("agent", agent.name, agent.instructions || agent.id)}<div class="resource-cell">${badge(agent.provider.toUpperCase(), agent.provider === "a2a" ? "remote" : "neutral")}</div><div class="resource-cell">${esc(state.runtimes.find((r) => r.id === agent.runtime_id)?.name || agent.runtime_id || "未绑定")}</div><div class="resource-cell">${esc(configSummary(agent))}</div></article>`,
      )
      .join(""),
  );
  $$("[data-agent-id]").forEach(
    (row) =>
      (row.onclick = () => showAgentForm(agentById(row.dataset.agentId))),
  );
}
export function renderSquads() {
  list(
    "squad",
    state.squads.length,
    ["协作小队", "成员", "Leader", "成员构成"],
    state.squads
      .map(
        (s) =>
          `<article class="resource-row entity-card clickable" data-resource-row role="button" tabindex="0" data-squad-id="${esc(s.id)}">${nameCell("squad", s.name, s.description || s.instructions || s.id)}<div class="resource-cell">${s.members.length} 名成员</div><div class="resource-cell">${esc(agentById(s.leader_agent_id)?.name || s.leader_agent_id)}</div><div class="resource-cell">${esc(s.members.map((m) => agentById(m.agent_id)?.name || m.agent_id).join("、"))}</div></article>`,
      )
      .join(""),
  );
  $$("[data-squad-id]").forEach(
    (row) => (row.onclick = () => openSquad(row.dataset.squadId)),
  );
}
export function renderRuntimeNodes() {
  list(
    "runtime",
    state.runtimes.length,
    ["运行节点", "状态", "最近活动", "操作"],
    state.runtimes
      .map((runtime) => {
        const caps = state.capabilities.filter(
          (c) => c.runtime_id === runtime.id,
        );
        return `<article class="resource-row" data-resource-row>${nameCell("runtime", runtime.name, `${runtime.hostname || runtime.id} · ${runtime.platform || "未知平台"} · ${runtime.max_slots} 个并发 Slot`)}<div class="resource-cell">${badge(runtime.status === "online" ? "在线" : "离线", runtime.status === "online" ? "neutral" : "danger")}</div><div class="resource-cell">${formatTime(runtime.last_seen_at)}</div><div class="detail-actions"><button class="secondary-button small-button" data-edit-runtime="${esc(runtime.id)}">编辑</button><button class="secondary-button small-button" data-probe-runtime="${esc(runtime.id)}">探测</button></div></article><details class="resource-runtime-details"><summary>${caps.filter((c) => c.status === "available").length} 个可用 Provider · 查看详情</summary><div class="member-list">${caps.map((c) => `<div class="member-item"><div><strong>${esc(c.provider.toUpperCase())} ${badge(c.status === "available" ? "可用" : "异常", c.status === "available" ? "neutral" : "danger")}</strong><p>${esc(c.executable_path)} · ${esc(c.version || "版本未知")}${c.error ? ` · ${esc(c.error)}` : ""}</p></div></div>`).join("") || "尚未发现 Provider"}</div></details>`;
      })
      .join(""),
  );
  $$("[data-edit-runtime]").forEach(
    (button) =>
      (button.onclick = () => showEditRuntime(button.dataset.editRuntime)),
  );
  $$("[data-probe-runtime]").forEach(
    (button) =>
      (button.onclick = () => probeRuntime(button.dataset.probeRuntime)),
  );
}
export function bindRegistryKeys() {
  $$(".entity-card.clickable").forEach(
    (row) =>
      (row.onkeydown = (event) => {
        if (["Enter", " "].includes(event.key)) {
          event.preventDefault();
          row.click();
        }
      }),
  );
}
