import { esc } from "./ui.js";
import { showDeleteIssue } from "./forms.js?v=delete-ui-v6";

let menu = null, anchor = null;
function closeMenu(restore = false) {
  menu?.remove();
  menu = null;
  anchor?.setAttribute("aria-expanded", "false");
  if (restore && anchor?.isConnected) anchor.focus();
  anchor = null;
}
export function openIssueMenu(issue, source, position) {
  closeMenu();
  anchor = source;
  anchor.setAttribute("aria-expanded", "true");
  menu = document.createElement("div");
  menu.className = "issue-action-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", `${issue.title}的操作`);
  menu.innerHTML = `<button role="menuitem" class="issue-delete-action">${trashIcon} 删除任务</button>`;
  document.body.append(menu);
  const rect = source.getBoundingClientRect();
  const x = position?.x ?? rect.right - menu.offsetWidth;
  const y = position?.y ?? rect.bottom + 4;
  menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8))}px`;
  const item = menu.querySelector("button");
  item.onclick = () => { closeMenu(true); showDeleteIssue(issue); };
  item.focus();
}
export const trashIcon = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg>`;
export function issueMenuButton(issueId) {
  return `<button class="icon-button issue-menu-button" data-issue-menu="${esc(issueId)}" aria-label="任务更多操作" aria-haspopup="menu" aria-expanded="false" title="更多操作"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg></button>`;
}
export function bindIssueMenu(button, getIssue) {
  button.onclick = (event) => {
    event.stopPropagation();
    const issue = getIssue();
    if (issue) openIssueMenu(issue, button);
  };
  button.onkeydown = (event) => {
    event.stopPropagation();
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      button.click();
    }
  };
}
if (typeof document !== "undefined") {
document.addEventListener("pointerdown", (event) => {
  if (menu && !menu.contains(event.target) && !anchor?.contains(event.target)) closeMenu();
});
document.addEventListener("keydown", (event) => {
  if (!menu) return;
  if (event.key === "Escape") { event.preventDefault(); closeMenu(true); }
  else if (event.key === "Tab") closeMenu();
  else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault(); menu.querySelector("button").focus();
  }
});
window.addEventListener("hashchange", () => closeMenu());
window.addEventListener("resize", () => closeMenu());
document.addEventListener("scroll", () => closeMenu(), true);

}
