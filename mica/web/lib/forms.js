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
} from "./ui.js";
import { state, agentById, squadById } from "./state.js";
import { api, jsonOptions } from "./api.js";

let refresh, openIssue, openDrawer, onIssueDeleted;
export function configureForms(actions) {
  ({ refresh, openIssue, openDrawer, onIssueDeleted } = actions);
}
function capabilitiesFor(runtimeId) {
  return state.capabilities.filter((item) => item.runtime_id === runtimeId);
}

let modalReturnFocus;
function modal(title, subtitle, body, wide = false, confirmation = false) {
  delete $("#modal-content").dataset.deleting;
  $(".modal").setAttribute("role", "dialog");
  $(".modal").removeAttribute("aria-describedby");
  modalReturnFocus = document.activeElement;
  $(".modal").classList.toggle("task-create-modal", title === "新建任务");
  $(".modal").classList.toggle("wide", wide);
  $(".modal").classList.toggle("delete-confirm-modal", confirmation);
  $("#modal-backdrop").classList.toggle("delete-confirm-backdrop", confirmation);
  $("#modal-backdrop").dataset.confirmation = confirmation ? "1" : "";
  $("#modal-content").innerHTML =
    confirmation
      ? `<div class="delete-confirm-header"><h2 id="modal-title">${title}</h2>${body}</div>`
      : `<header class="modal-header"><div><h2 id="modal-title">${title}</h2>${subtitle ? `<p>${subtitle}</p>` : ""}</div><button class="icon-button" data-close-modal>×</button></header><div class="modal-body">${body}</div>`;
  $("#modal-backdrop").hidden = false;
  if (!confirmation) $("[data-close-modal]").onclick = closeModal;
  $$(".field", $("#modal-content")).forEach((field, i) => {
    const label = $("label", field),
      input = $("input,textarea,select", field);
    if (label && input) {
      if (!input.id) input.id = `modal-field-${i}`;
      label.htmlFor = input.id;
    }
  });
  const form = $("#modal-content form");
  if (form) {
    form.addEventListener(
      "submit",
      (event) => {
        if (form.dataset.busy) {
          event.preventDefault();
          event.stopImmediatePropagation();
          return;
        }
        const handler = form.onsubmit;
        if (!handler) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        form.dataset.busy = "1";
        const submit = form.querySelector(
          'button:not([type]), button[type="submit"]',
        );
        if (submit) submit.disabled = true;
        Promise.resolve()
          .then(() => handler.call(form, event))
          .finally(() => {
            delete form.dataset.busy;
            if (submit) submit.disabled = false;
          });
      },
      true,
    );
  }
  requestAnimationFrame(() =>
    $(
      "#modal-content input:not([readonly]), #modal-content select, #modal-content textarea",
    )?.focus(),
  );
}
export function closeModal() {
  if ($("#modal-backdrop").hidden || $("#modal-content").dataset.deleting) return;
  $("#modal-backdrop").hidden = true;
  modalReturnFocus?.isConnected && modalReturnFocus.focus();
}
function field(label, name, opts = {}) {
  const tag = opts.textarea
    ? `<textarea id="field-${name}" name="${name}" ${opts.required ? "required" : ""} ${opts.readonly ? "readonly" : ""} placeholder="${esc(opts.placeholder || "")}">${esc(opts.value || "")}</textarea>`
    : `<input id="field-${name}" name="${name}" type="${opts.type || "text"}" ${opts.required ? "required" : ""} ${opts.readonly ? "readonly" : ""} value="${esc(opts.value || "")}" placeholder="${esc(opts.placeholder || "")}">`;
  return `<div class="field ${opts.full ? "full" : ""}"><label for="field-${name}">${label}</label>${tag}${opts.hint ? `<span class="field-hint">${opts.hint}</span>` : ""}</div>`;
}
function targetOptions() {
  return `<optgroup label="本地 Agent">${state.agents
    .filter((a) => a.provider !== "a2a")
    .map(
      (a) =>
        `<option value="agent|${esc(a.id)}">${esc(a.name)} · ${esc(a.provider)}</option>`,
    )
    .join("")}</optgroup><optgroup label="远程业务 Agent">${state.agents
    .filter((a) => a.provider === "a2a")
    .map(
      (a) => `<option value="agent|${esc(a.id)}">${esc(a.name)} · A2A</option>`,
    )
    .join(
      "",
    )}</optgroup><optgroup label="Agent Squad">${state.squads.map((s) => `<option value="squad|${esc(s.id)}">${esc(s.name)} · ${s.members.length} 人</option>`).join("")}</optgroup>`;
}
function projectOptions(runtimeId) {
  const projects = state.projects.filter(
    (project) => project.runtime_id === runtimeId,
  );
  const registeredPaths = new Set(
    projects.map((project) => project.local_path),
  );
  const registered = projects.map((project) => ({
    value: `project|${project.id}`,
    label: `${project.name} · ${project.execution_mode === "in_place" ? "当前目录" : "Worktree"}`,
  }));
  const discovered = state.projectCandidates
    .filter((candidate) => !registeredPaths.has(candidate.local_path))
    .map((candidate) => ({
      value: `candidate|${candidate.local_path}`,
      label: `${candidate.name} · 本机发现`,
    }));
  const options = [...registered, ...discovered];
  const recent = storageGet("mica.recentProject") || "";
  return `<option value="">不关联项目</option>${options.map((item) => `<option value="${esc(item.value)}"${item.value === recent ? " selected" : ""}>${esc(item.label)}</option>`).join("")}`;
}
function targetAgent(target) {
  const [type, id] = target.split("|");
  if (type === "agent") return agentById(id);
  return agentById(squadById(id)?.leader_agent_id);
}
function targetRuntimeId(target) {
  return targetAgent(target)?.runtime_id || state.runtimes[0]?.id;
}
function targetSupportsProject(target) {
  return ["codex", "claude", "pi", "acp"].includes(
    targetAgent(target)?.provider,
  );
}
export function showCreateIssue() {
  if (!state.agents.length) {
    toast("请先注册 Agent", true);
    location.hash = "#agents";
    return;
  }
  modal(
    "新建任务",
    "",
    `<form id="create-issue" class="form-grid task-create-form">${field("任务标题", "title", { required: true, full: true, placeholder: "任务标题" })}<div class="field"><label>执行方</label><select name="target" id="issue-target">${targetOptions()}</select></div>${field("任务描述", "body", { required: true, textarea: true, full: true, placeholder: "说明背景、期望结果和必要上下文" })}<details class="create-project-options field full"><summary>关联项目与工作目录</summary><div class="form-grid"><div class="field"><label>项目</label><select name="project" id="issue-project"></select><span class="field-hint" id="project-hint"></span></div><div class="field"><label>项目执行方式</label><select name="execution_mode" id="issue-execution-mode"><option value="worktree">独立 Worktree</option><option value="in_place">当前目录</option></select><span class="field-hint" id="execution-mode-hint"></span></div></div></details><div class="modal-actions field full"><button type="button" class="secondary-button" data-close-modal>取消</button><button class="primary-button">创建并执行</button></div></form>`,
  );
  $$("[data-close-modal]").forEach((button) => (button.onclick = closeModal));
  const updateProjectHints = () => {
    const target = $("#issue-target").value;
    const project = $("#issue-project");
    const mode = $("#issue-execution-mode");
    const supported = targetSupportsProject(target);
    project.disabled = !supported;
    if (!supported) project.value = "";
    const selected = project.value;
    mode.disabled = !supported || !selected;
    $("#project-hint").textContent = !supported
      ? "远程业务 Agent 不访问本机项目。"
      : selected
        ? "所选项目将作为 Agent 的工作目录。"
        : "不关联项目时，Agent 在独立的空工作目录中执行。";
    $("#execution-mode-hint").textContent = !selected
      ? "选择项目后可配置。"
      : mode.value === "in_place"
        ? "直接操作当前目录；同一路径任务串行执行，修改会立即出现在工作区。"
        : "每个任务使用隔离 Worktree，可并发执行并保留独立分支。";
  };
  const selectProjectMode = () => {
    const selected = $("#issue-project").value;
    const separator = selected.indexOf("|");
    const kind = selected.slice(0, separator),
      id = selected.slice(separator + 1);
    const registered =
      kind === "project" ? state.projects.find((item) => item.id === id) : null;
    $("#issue-execution-mode").value = registered?.execution_mode || "worktree";
    updateProjectHints();
  };
  const loadProjectOptions = () => {
    const target = $("#issue-target").value;
    $("#issue-project").innerHTML = projectOptions(targetRuntimeId(target));
    selectProjectMode();
  };
  $("#issue-target").onchange = loadProjectOptions;
  $("#issue-project").onchange = selectProjectMode;
  $("#issue-execution-mode").onchange = updateProjectHints;
  loadProjectOptions();
  let submittedPayload = "",
    idempotencyKey = "";
  $("#create-issue").onsubmit = async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    const [assignee_type, assignee_id] = data.target.split("|");
    try {
      let projectId = null;
      if (data.project) {
        const separator = data.project.indexOf("|");
        const kind = data.project.slice(0, separator),
          value = data.project.slice(separator + 1);
        if (kind === "project") projectId = value;
        else {
          const candidate = state.projectCandidates.find(
            (item) => item.local_path === value,
          );
          if (!candidate)
            throw new Error("所选项目已不在扫描结果中，请刷新后重试");
          const project = await api(
            "/api/projects",
            jsonOptions("POST", {
              name: candidate.name,
              local_path: candidate.local_path,
              workspace_id: "local",
              runtime_id: targetRuntimeId(data.target),
              execution_mode: data.execution_mode,
            }),
          );
          projectId = project.id;
        }
        if (kind === "project") {
          const existing = state.projects.find((item) => item.id === projectId);
          if (existing && existing.execution_mode !== data.execution_mode)
            await api(
              `/api/projects/${projectId}`,
              jsonOptions("PATCH", { execution_mode: data.execution_mode }),
            );
        }
        storageSet("mica.recentProject", data.project);
      }
      const payload = {
        title: data.title,
        body: data.body,
        workspace_id: "local",
        assignee_type,
        assignee_id,
        project_id: projectId,
      };
      const signature = JSON.stringify(payload);
      if (signature !== submittedPayload) {
        submittedPayload = signature;
        idempotencyKey = crypto.randomUUID();
      }
      const issue = await api("/api/issues", {
        ...jsonOptions("POST", payload),
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
      });
      closeModal();
      await refresh();
      openIssue(issue.id);
      toast("任务已创建");
    } catch (error) {
      toast(error.message, true);
    }
  };
}

function memberRow() {
  return `<div class="member-row"><select name="member_agent"><option value="">选择成员</option>${state.agents.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")}</select><input name="member_role" placeholder="职责"><input name="member_caps" placeholder="能力，逗号分隔"><button type="button" class="icon-button" data-remove-row>×</button></div>`;
}
export function showCreateSquad() {
  if (state.agents.length < 2) {
    toast("至少注册两个 Agent 后才能组装 Squad", true);
    return;
  }
  modal(
    "组装 Agent Squad",
    "Leader 负责理解目标、委派成员并汇总结果。",
    `<form id="create-squad" class="form-grid">${field("Squad 名称", "name", { required: true, placeholder: "空间方案小队" })}<div class="field"><label>Leader Agent</label><select name="leader_agent_id">${state.agents.map((a) => `<option value="${esc(a.id)}">${esc(a.name)} · ${esc(a.provider)}</option>`).join("")}</select></div>${field("业务场景", "description", { textarea: true, full: true, placeholder: "这个 Squad 解决什么问题" })}${field("协作规则", "instructions", { textarea: true, full: true, placeholder: "例如：先进行几何校验，再生成摆放方案，最后由 Leader 汇总。" })}<div class="field full"><label>成员与职责</label><div class="member-editor" id="member-editor">${memberRow()}</div><button type="button" class="secondary-button small-button" id="add-member">＋ 添加成员</button></div><div class="modal-actions field full"><button type="button" class="secondary-button" data-close-modal>取消</button><button class="primary-button">创建 Squad</button></div></form>`,
    true,
  );
  const bindRows = () =>
    $$("[data-remove-row]").forEach(
      (b) => (b.onclick = () => b.closest(".member-row").remove()),
    );
  bindRows();
  $("#add-member").onclick = () => {
    $("#member-editor").insertAdjacentHTML("beforeend", memberRow());
    bindRows();
  };
  $$("[data-close-modal]").forEach((b) => (b.onclick = closeModal));
  $("#create-squad").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target,
      data = new FormData(form),
      agents = data.getAll("member_agent"),
      roles = data.getAll("member_role"),
      caps = data.getAll("member_caps");
    const members = agents
      .map((id, i) => ({
        agent_id: id,
        role: roles[i],
        capabilities: caps[i]
          .split(/[,，]/)
          .map((x) => x.trim())
          .filter(Boolean),
      }))
      .filter((x) => x.agent_id && x.agent_id !== data.get("leader_agent_id"));
    try {
      await api(
        "/api/squads",
        jsonOptions("POST", {
          name: data.get("name"),
          workspace_id: "local",
          leader_agent_id: data.get("leader_agent_id"),
          description: data.get("description"),
          instructions: data.get("instructions"),
          members,
        }),
      );
      closeModal();
      await refresh();
      toast("Squad 已创建");
    } catch (error) {
      toast(error.message, true);
    }
  };
}
export function openSquad(id) {
  const s = squadById(id);
  if (!s) return;
  openDrawer(
    `<header class="drawer-header"><div>${badge("SQUAD", "squad")}<h2>${esc(s.name)}</h2></div><button class="icon-button" data-close-drawer>×</button></header><div class="drawer-body"><section class="detail-section"><h3 class="section-title">业务场景</h3><div class="prose">${esc(s.description || "尚未填写")}</div></section><section class="detail-section"><h3 class="section-title">Leader 协作规则</h3><div class="prose">${esc(s.instructions || "尚未填写")}</div></section><section class="detail-section"><h3 class="section-title">成员 <span>${s.members.length}</span></h3><div class="member-list">${s.members
      .map((m) => {
        const a = agentById(m.agent_id),
          leader = m.agent_id === s.leader_agent_id;
        return `<div class="member-item"><div><strong>${esc(a?.name || m.agent_id)} ${leader ? badge("LEADER", "squad") : ""}</strong><p>${esc(m.role || "未指定职责")} · ${(m.capabilities || []).map(esc).join("、") || "未填写能力"} · ${esc(a?.provider || "")}</p></div>${!leader ? `<button class="danger-button small-button" data-remove-member="${esc(m.agent_id)}">移除</button>` : ""}</div>`;
      })
      .join("")}</div></section></div>`,
  );
  $("[data-close-drawer]").onclick = closeDrawer;
  $$("[data-remove-member]").forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          await api(`/api/squads/${id}/members/${b.dataset.removeMember}`, {
            method: "DELETE",
          });
          await refresh();
          openSquad(id);
          toast("成员已移除");
        } catch (error) {
          toast(error.message, true);
        }
      }),
  );
}
export function probeConfiguredRuntime() {
  const runtime = state.runtimes[0];
  if (!runtime) {
    toast("尚未发现正在运行的 Daemon", true);
    return;
  }
  probeRuntime(runtime.id);
}

export function showEditRuntime(id) {
  const runtime = state.runtimes.find((item) => item.id === id);
  if (!runtime) return;
  modal(
    "编辑执行节点",
    "Runtime ID 和 Daemon 归属不可修改。",
    `<form id="edit-runtime" class="form-grid">${field("显示名称", "name", { required: true, value: runtime.name })}${field("最大并发 Slot", "max_slots", { required: true, type: "number", value: String(runtime.max_slots), hint: "只影响后续任务的并发领取。" })}<div class="field full"><label>Runtime ID</label><input value="${esc(runtime.id)}" disabled></div><div class="modal-actions field full"><button type="button" class="secondary-button" data-close-modal>取消</button><button class="primary-button">保存修改</button></div></form>`,
  );
  $$("[data-close-modal]").forEach((button) => (button.onclick = closeModal));
  $("#edit-runtime").onsubmit = async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    try {
      await api(
        `/api/runtimes/${id}`,
        jsonOptions("PATCH", {
          name: data.name,
          max_slots: Number(data.max_slots),
        }),
      );
      closeModal();
      await refresh();
      toast("执行节点已更新");
    } catch (error) {
      toast(error.message, true);
    }
  };
}
export async function probeRuntime(id) {
  try {
    toast("正在探测本机 Agent CLI…");
    await api(`/api/runtimes/${id}/probe`, { method: "POST" });
    await refresh();
    toast("Runtime 探测完成");
  } catch (error) {
    toast(error.message, true);
  }
}
function runtimeProviderOptions(runtimeId, selectedProvider = "") {
  const selected = (provider) =>
    provider === selectedProvider ? " selected" : "";
  const discovered = capabilitiesFor(runtimeId).filter(
    (item) => item.status === "available",
  );
  let local = discovered
    .map(
      (item) =>
        `<option value="${esc(item.provider)}"${selected(item.provider)}>${esc(item.provider.toUpperCase())} · 已发现</option>`,
    )
    .join("");
  if (
    selectedProvider &&
    ["codex", "claude", "pi"].includes(selectedProvider) &&
    !discovered.some((item) => item.provider === selectedProvider)
  ) {
    local += `<option value="${esc(selectedProvider)}" selected disabled>${esc(selectedProvider.toUpperCase())} · 当前不可用</option>`;
  }
  return `<optgroup label="本地已发现">${local || "<option disabled>没有发现本地 CLI</option>"}</optgroup><optgroup label="协议接入"><option value="acp"${selected("acp")}>ACP · 手动命令</option><option value="a2a"${selected("a2a")}>A2A · 远程 Agent</option></optgroup><option value="stub"${selected("stub")}>Stub · 仅测试</option>`;
}
export function showAgentForm(agent = null) {
  if (!state.runtimes.length) {
    toast("请先启动本机 Daemon", true);
    location.hash = "#runtimes";
    return;
  }
  const editing = Boolean(agent);
  const selectedRuntime = agent?.runtime_id || state.runtimes[0].id;
  modal(
    editing ? "编辑 Agent" : "注册 Agent",
    "先选择执行节点，再从该节点发现的 Provider 中选择运行方式。",
    `<form id="agent-form" class="form-grid">${field("Agent ID", "id", { required: true, readonly: editing, value: agent?.id || "", placeholder: "geometry-agent" })}${field("显示名称", "name", { required: true, value: agent?.name || "", placeholder: "Geometry Agent" })}<div class="field"><label>执行节点</label><select name="runtime_id" id="agent-runtime">${state.runtimes.map((runtime) => `<option value="${esc(runtime.id)}"${runtime.id === selectedRuntime ? " selected" : ""}>${esc(runtime.name)}</option>`).join("")}</select></div><div class="field"><label>Runtime / Provider</label><select name="provider" id="agent-provider"></select></div>${field("Agent 指令", "instructions", { textarea: true, full: true, value: agent?.instructions || "", placeholder: "描述 Agent 的职责、边界与输出要求" })}<div class="field full" id="provider-fields-v2"></div><div class="modal-actions field full"><button type="button" class="secondary-button" data-close-modal>取消</button><button class="primary-button">${editing ? "保存修改" : "保存 Agent"}</button></div></form>`,
    true,
  );
  const renderProviderFields = () => {
    const provider = $("#agent-provider").value;
    const root = $("#provider-fields-v2");
    const config =
      provider === agent?.provider ? agent.provider_config || {} : {};
    if (provider === "a2a")
      root.innerHTML = `${field("Agent Card URL", "agent_card_url", { required: true, value: config.agent_card_url || "", placeholder: "https://agent.internal/.well-known/agent-card.json" })}${field("Skill ID", "skill_id", { value: config.skill_id || "", placeholder: "consultation" })}${field("Token 环境变量", "token_env", { value: config.token_env || "", placeholder: "A2A_API_TOKEN" })}${field("输出 JSON Schema", "output_schema", { textarea: true, value: config.output_schema ? JSON.stringify(config.output_schema, null, 2) : "", placeholder: '{"type":"object"}' })}`;
    else if (provider === "acp")
      root.innerHTML = field("ACP 启动命令", "command", {
        required: true,
        value: (config.command || []).join(" "),
        placeholder: "my-agent --acp",
        hint: "ACP 无统一可执行文件名，需要显式配置。",
      });
    else if (provider === "claude")
      root.innerHTML = `${field("模型（可选）", "model", { value: config.model || "", placeholder: "使用 Runtime 默认模型" })}<div class="field"><label>执行权限</label><div class="field-hint">无人值守模式：平台自动批准 Claude 的工具调用，并禁止脱离 Daemon 管理的后台任务。关联真实项目时，优先使用独立 Worktree。</div></div>`;
    else
      root.innerHTML = field("模型（可选）", "model", {
        value: config.model || "",
        placeholder: "使用 Runtime 默认模型",
      });
  };
  const renderProvider = (preferred) => {
    $("#agent-provider").innerHTML = runtimeProviderOptions(
      $("#agent-runtime").value,
      preferred,
    );
    renderProviderFields();
  };
  $("#agent-runtime").onchange = () => renderProvider("");
  $("#agent-provider").onchange = renderProviderFields;
  renderProvider(agent?.provider || "");
  $$("[data-close-modal]").forEach((button) => (button.onclick = closeModal));
  $("#agent-form").onsubmit = async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target)),
      config =
        data.provider === agent?.provider
          ? { ...(agent.provider_config || {}) }
          : {};
    try {
      if (data.provider === "a2a") {
        config.agent_card_url = data.agent_card_url;
        if (data.skill_id) config.skill_id = data.skill_id;
        else delete config.skill_id;
        if (data.token_env) config.token_env = data.token_env;
        else delete config.token_env;
        if (data.output_schema)
          config.output_schema = JSON.parse(data.output_schema);
        else delete config.output_schema;
      } else if (data.provider === "acp") {
        config.command = data.command.trim().split(/\s+/);
        config.permission_policy = "deny";
      } else {
        if (data.model) config.model = data.model;
        else delete config.model;
        if (data.provider === "claude") delete config.permission_mode;
      }
      await api(
        "/api/agents",
        jsonOptions("POST", {
          id: data.id,
          name: data.name,
          runtime_id: data.runtime_id,
          provider: data.provider,
          instructions: data.instructions,
          provider_config: config,
        }),
      );
      closeModal();
      await refresh();
      toast(editing ? "Agent 已更新" : "Agent 已保存");
    } catch (error) {
      toast(error.message, true);
    }
  };
}

export function showDeleteIssue(issue) {
  const active = state.tasks.some((task) => task.issue_id === issue.id &&
    ["queued", "dispatched", "running", "waiting_input"].includes(task.status));
  modal("删除任务", "", `
    <p id="delete-description">将永久删除该任务及其讨论和执行记录。此操作不可撤销。${active ? "关联的 Agent 执行将被取消。" : ""}</p>
    <p id="delete-error" class="delete-error" role="alert" hidden></p>
    <div class="delete-confirm-footer"><button class="secondary-button" id="cancel-delete">取消</button>
    <button class="danger-button" id="confirm-delete">删除</button></div>`, false, true);
  $(".modal").setAttribute("role", "alertdialog");
  $(".modal").setAttribute("aria-describedby", "delete-description");
  $("#cancel-delete").onclick = closeModal;
  $("#cancel-delete").focus();
  $("#confirm-delete").onclick = async () => {
    const content = $("#modal-content");
    if (content.dataset.deleting) return;
    content.dataset.deleting = "1";
    $("#confirm-delete").setAttribute("aria-busy", "true");
    $("#delete-error").hidden = true;
    const buttons = $$("button", content);
    buttons.forEach((button) => button.disabled = true);
    $("#confirm-delete").textContent = "删除中…";
    try {
      await api(`/api/issues/${encodeURIComponent(issue.id)}`, { method: "DELETE" });
    } catch (error) {
      delete content.dataset.deleting;
      $("#confirm-delete").removeAttribute("aria-busy");
      buttons.forEach((button) => button.disabled = false);
      $("#confirm-delete").textContent = "删除";
      $("#delete-error").textContent = error.message;
      $("#delete-error").hidden = false;
      $("#confirm-delete").focus();
      return;
    }
    delete content.dataset.deleting;
    closeModal();
    state.issues = state.issues.filter((item) => item.id !== issue.id);
    state.tasks = state.tasks.filter((item) => item.issue_id !== issue.id);
    toast("已删除任务");
    // A refresh failure must never present an already successful delete as failed.
    try { await onIssueDeleted(issue.id); }
    catch (error) { toast(`任务已删除，刷新失败：${error.message}`, true); }
  };
}
