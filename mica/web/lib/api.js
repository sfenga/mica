import { state } from "./state.js";
export async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    signal: options.signal || AbortSignal.timeout(20000),
  });
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`服务返回了无效响应 (${response.status})，请重试`);
  }
  if (!response.ok) {
    const detail = data?.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item) => item.msg).join("；")
          : `请求失败 (${response.status})`,
    );
  }
  return data;
}
export const jsonOptions = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
let loading;
export function loadData() {
  // Every view shares a single snapshot; overlapping refreshes reuse the request.
  if (loading) return loading;
  const keys = [
    "issues",
    "tasks",
    "agents",
    "squads",
    "runtimes",
    "capabilities",
    "projects",
    "projectCandidates",
  ];
  const urls = [
    "/api/issues",
    "/api/tasks",
    "/api/agents",
    "/api/squads?workspace_id=local",
    "/api/runtimes",
    "/api/runtime-capabilities",
    "/api/projects?workspace_id=local",
    "/api/project-candidates",
  ];
  loading = Promise.all(urls.map((url) => api(url)))
    .then((values) =>
      Object.assign(
        state,
        Object.fromEntries(keys.map((key, i) => [key, values[i]])),
      ),
    )
    .finally(() => {
      loading = null;
    });
  return loading;
}
