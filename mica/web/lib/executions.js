import { $, esc, badge, duration } from "./ui.js";
import { agentById, ACTIVE_TASKS, TASK_LABEL } from "./state.js";
import { api } from "./api.js";
import { markdown } from "./markdown.js";

export function executionCard(task) {
  const agent = agentById(task.agent_id),
    cancellable = ACTIVE_TASKS.has(task.status);
  const inputCount = (
    task.delivered_comment_ids ||
    task.merged_comment_ids ||
    []
  ).length;
  const actions = cancellable
    ? `<div class="detail-actions"><button class="danger-button small-button" data-cancel-task="${esc(task.id)}">取消执行</button></div>`
    : "";
  return `<details class="execution-card" data-task-events="${esc(task.id)}"><summary><div class="execution-title"><strong>${esc(agent?.name || task.agent_id)}</strong>${badge(TASK_LABEL[task.status] || task.status, task.status === "failed" ? "danger" : task.status === "waiting_input" ? "warning" : "neutral")}</div><div class="execution-meta">${task.is_leader_task ? badge("LEADER", "squad") : task.squad_id ? badge("MEMBER", "local") : ""}${badge(agent?.provider?.toUpperCase() || "AGENT", agent?.provider === "a2a" ? "remote" : "local")}${inputCount ? badge(`合并 ${inputCount} 条输入`, "neutral") : ""}${badge(`尝试 ${task.attempt}`, "neutral")}</div></summary>${task.handoff_note ? `<div class="execution-output">交接：${esc(task.handoff_note)}</div>` : ""}${task.output ? `<div class="execution-output markdown">${markdown(task.output)}</div>` : ""}${task.error ? `<div class="execution-output error">${esc(task.error)}</div>` : ""}${actions}<div class="event-list" id="events-${esc(task.id)}"><span class="field-hint">展开后加载事件 · ${duration(task)}</span></div></details>`;
}
export function compactTimelineEvents(events) {
  return events.reduce((items, event) => {
    const previous = items[items.length - 1];
    if (
      previous?.event_type === "agent.text" &&
      event.event_type === "agent.text" &&
      previous.payload.parent_tool_use_id === event.payload?.parent_tool_use_id
    ) {
      previous.payload.text = `${previous.payload?.text || ""}${event.payload?.text || ""}`;
      return items;
    }
    items.push({ ...event, payload: { ...event.payload } });
    return items;
  }, []);
}
const EVENT_LABELS = {
  "task.queued": "等待执行",
  "task.input_merged": "合并输入",
  "task.dispatched": "分配执行节点",
  "task.recovered": "恢复执行",
  "task.cancelled": "执行已取消",
  "task.cancel_requested": "请求取消",
  "task.started": "开始执行",
  "task.completed": "执行完成",
  "task.failed": "执行失败",
  "task.input_required": "等待补充输入",
  "agent.system": "会话事件",
  "agent.error": "执行错误",
};
export function eventPresentation(event) {
  const payload = event.payload || {};
  const item = payload.item || {};
  const update = payload.update || {};
  if (event.event_type === "agent.text")
    return {
      label: "Agent 回复",
      text: String(payload.text || ""),
      kind: "text",
    };
  if (event.event_type === "agent.tool") {
    const thinking = payload.name === "reasoning";
    return {
      label: thinking ? "思考" : "工具调用",
      text: String(
        thinking ? item.text || "" : payload.name || update.title || "工具活动",
      ),
      kind: thinking ? "text" : "tool",
    };
  }
  return {
    label: EVENT_LABELS[event.event_type] || event.event_type,
    text: String(payload.message || payload.phase || payload.subtype || ""),
    kind: "event",
  };
}
function eventRow(event) {
  const presentation = eventPresentation(event);
  return `<details class="event-entry" ${presentation.kind === "text" ? "open" : ""}><summary><strong>${esc(presentation.label)}</strong><span>${esc(presentation.kind === "text" ? "" : presentation.text)}</span></summary>${presentation.kind === "text" ? `<div class="markdown">${markdown(presentation.text)}</div>` : ""}<details class="event-raw"><summary>原始事件</summary><pre>${esc(JSON.stringify(event.payload || {}, null, 2))}</pre></details></details>`;
}
export async function loadEvents(taskId) {
  const root = $(`#events-${CSS.escape(taskId)}`);
  if (!root || root.dataset.loaded) return;
  root.dataset.loaded = "loading";
  try {
    const events = compactTimelineEvents(
      await api(`/api/tasks/${taskId}/events?include_internal=false&limit=200`),
    );
    root.dataset.loaded = "1";
    root.innerHTML = `<span class="field-hint">最近 200 条事件</span>${events.map(eventRow).join("") || '<span class="field-hint">暂无事件</span>'}`;
  } catch (error) {
    delete root.dataset.loaded;
    root.innerHTML = `<span class="field-hint">${esc(error.message)}</span><button type="button" class="secondary-button small-button">重试</button>`;
    root.querySelector("button").onclick = () => loadEvents(taskId);
  }
}
