export const state = {
  route: "board",
  issues: [],
  tasks: [],
  agents: [],
  squads: [],
  runtimes: [],
  capabilities: [],
  projects: [],
  projectCandidates: [],
  search: "",
  selectedIssue: null,
  boardScroll: { top: 0, left: 0 },
};
export const ISSUE_COLUMNS = [
  ["backlog", "待规划"],
  ["todo", "待执行"],
  ["in_progress", "执行中"],
  ["in_review", "待确认"],
  ["blocked", "已阻塞"],
  ["done", "已完成"],
];
export const ISSUE_LABEL = Object.fromEntries([
  ...ISSUE_COLUMNS,
  ["cancelled", "已取消"],
]);
export const TASK_LABEL = {
  queued: "排队中",
  dispatched: "已派发",
  running: "执行中",
  waiting_input: "等待输入",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
export const ACTIVE_TASKS = new Set([
  "queued",
  "dispatched",
  "running",
  "waiting_input",
]);
export const USER_MOVES = {
  backlog: ["todo"],
  todo: ["backlog"],
  in_review: ["done", "in_progress", "blocked"],
  blocked: ["todo", "in_progress"],
};
export const agentById = (id) => state.agents.find((item) => item.id === id);
export const squadById = (id) => state.squads.find((item) => item.id === id);
export function assignee(issue) {
  if (issue.assignee_type === "squad")
    return {
      name: squadById(issue.assignee_id)?.name || issue.assignee_id,
      type: "squad",
      provider: "squad",
    };
  const agent = agentById(issue.assignee_id),
    provider = agent?.provider || "agent";
  return {
    name: agent?.name || issue.assignee_id,
    type: provider === "a2a" ? "remote" : "local",
    provider,
  };
}
export function mentionTargets() {
  return [
    ...state.agents.map((a) => ({ ...a, kind: "agent" })),
    ...state.squads.map((s) => ({ ...s, kind: "squad" })),
  ].filter((item) => /^[\w.-]+$/.test(item.id));
}
export const resolveMention = (kind, id) =>
  mentionTargets().find((item) => item.kind === kind && item.id === id);
