export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [
  ...root.querySelectorAll(selector),
];
export const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
export const badge = (text, type = "neutral") =>
  `<span class="badge ${esc(type)}">${esc(text)}</span>`;
export function toast(message, error = false) {
  const item = document.createElement("div");
  item.className = `toast${error ? " error" : ""}`;
  item.textContent = message;
  $("#toast-region").append(item);
  setTimeout(() => item.remove(), 4500);
}
export function formatTime(value) {
  const date = new Date(value);
  return !value || Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date);
}
export function duration(task) {
  const seconds = Math.max(
    0,
    (new Date(task.updated_at) - new Date(task.created_at)) / 1000,
  );
  return seconds < 60
    ? `${Math.round(seconds)} 秒`
    : `${Math.round(seconds / 60)} 分钟`;
}
export function storageGet(key, fallback = "") {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}
export function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* In-memory editor remains usable. */
  }
}
export function storageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    /* Storage may be disabled. */
  }
}

export const empty = (title, detail) =>
  `<div class="empty-state"><div><strong>${esc(title)}</strong>${detail}</div></div>`;
