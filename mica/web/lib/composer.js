import { $, esc, storageGet, storageSet, storageRemove, toast } from "./ui.js";
import {
  mentionTargets,
  resolveMention,
  agentById,
  squadById,
} from "./state.js";
import { MENTION_PATTERN } from "./markdown.js";
import { api, jsonOptions } from "./api.js";

export function serializeEditor(root) {
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE)
      return node.textContent.replace(/\u00a0/g, " ");
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    if (node.dataset.mentionId) {
      const label = node.textContent.replace(/^@/, "").replace(/[\[\]\\]/g, "");
      return `[@${label}](mention://${node.dataset.mentionKind}/${node.dataset.mentionId})`;
    }
    if (node.tagName === "BR") return "\n";
    let text = [...node.childNodes].map(walk).join("");
    if (
      ["DIV", "P"].includes(node.tagName) &&
      node !== root &&
      node.previousSibling
    )
      text = "\n" + text;
    return text;
  };
  return walk(root).replace(/\n+$/, "");
}
function chip(target) {
  const element = document.createElement("span");
  element.className = `mention-chip ${target.kind}`;
  element.contentEditable = "false";
  element.dataset.mentionKind = target.kind;
  element.dataset.mentionId = target.id;
  element.textContent = `@${target.name}`;
  element.title = `${target.kind}:${target.id}`;
  return element;
}
function restoreEditor(editor, source) {
  editor.replaceChildren();
  let end = 0;
  for (const match of source.matchAll(MENTION_PATTERN)) {
    editor.append(document.createTextNode(source.slice(end, match.index)));
    const kind = match[2] || match[4],
      id = match[3] || match[5],
      target = resolveMention(kind, id);
    editor.append(target ? chip(target) : document.createTextNode(match[0]));
    end = match.index + match[0].length;
  }
  editor.append(document.createTextNode(source.slice(end)));
}
export function routingSummary(body, issue, tasks = [], parentId = null) {
  const explicit = [],
    unknown = [];
  for (const match of body.matchAll(MENTION_PATTERN)) {
    const kind = match[2] || match[4],
      id = match[3] || match[5],
      target = resolveMention(kind, id);
    if (target) explicit.push(target);
    else unknown.push(`${kind}:${id}`);
  }
  const plain = body.replace(MENTION_PATTERN, "");
  for (const match of plain.matchAll(/(?:^|\s)@([^\s@]+)/g))
    unknown.push(match[1]);
  const unique = [
    ...new Map(
      explicit.map((item) => [`${item.kind}:${item.id}`, item]),
    ).values(),
  ];
  if (unique.length)
    return {
      label: `发送给 ${unique.map((item) => item.name + (item.kind === "squad" ? "（Leader）" : "")).join("、")}`,
      unknown,
    };
  const comments = issue.comments || [];
  const parent = comments.find((item) => item.id === parentId);
  if (parent) {
    let task = tasks.find((item) => item.id === parent.source_task_id);
    if (!task && parent.author_type === "agent")
      return {
        label: `回复给 ${agentById(parent.author_id)?.name || parent.author_id}`,
        unknown,
      };
    if (!task) {
      let root = parent;
      const visited = new Set();
      while (root.parent_comment_id && !visited.has(root.id)) {
        visited.add(root.id);
        const next = comments.find(
          (item) => item.id === root.parent_comment_id,
        );
        if (!next) break;
        root = next;
      }
      task = tasks.find(
        (item) =>
          item.trigger_comment_id === root.id ||
          (item.merged_comment_ids || []).includes(root.id),
      );
    }
    return {
      label: task
        ? `回复给 ${agentById(task.agent_id)?.name || task.agent_id}`
        : "仅保存回复，不触发新的执行",
      unknown,
    };
  }
  const owner =
    issue.assignee_type === "squad"
      ? squadById(issue.assignee_id)
      : agentById(issue.assignee_id);
  return {
    label: `发送给 ${owner?.name || issue.assignee_id}${issue.assignee_type === "squad" ? "（Leader）" : ""}`,
    unknown,
  };
}
export function mountComposer(root, { issueId, context, onSent }) {
  const key = `mica.draft.${issueId}`;
  let draft;
  try {
    draft = JSON.parse(storageGet(key, "{}"));
  } catch {
    draft = {};
  }
  let parentId = draft.parentId || null,
    busy = false,
    composing = false,
    options = [],
    selected = 0,
    queryRange = null;
  root.innerHTML = `<form class="composer" id="comment-form">
    <div class="reply-context" id="reply-context" hidden><span id="reply-label"></span><button type="button" class="text-button" id="clear-reply">取消回复</button></div>
    <div class="editor-wrap"><div id="comment-editor" class="comment-editor" contenteditable="true" role="textbox" aria-label="评论内容" aria-multiline="true" aria-autocomplete="list" aria-controls="mention-menu" data-placeholder="补充上下文、提出修改要求，输入 @ 选择 Agent…"></div>
      <div id="mention-menu" class="mention-menu" role="listbox" aria-label="提及对象" hidden></div></div>
    <div class="composer-routing" id="composer-routing" aria-live="polite"></div>
    <div class="composer-footer"><span class="composer-help">Markdown · @ 提及 · <kbd>⌘ / Ctrl</kbd> + <kbd>Enter</kbd> 发送</span><div class="inline-actions"><span class="draft-label" id="draft-label"></span><button class="primary-button" id="send-comment" disabled>发送评论 ↑</button></div></div>
    <p class="composer-error" id="composer-error" role="alert" hidden></p>
  </form>`;
  const editor = $("#comment-editor", root),
    menu = $("#mention-menu", root),
    form = $("form", root),
    send = $("#send-comment", root);
  restoreEditor(editor, draft.body || "");
  function body() {
    return serializeEditor(editor);
  }
  function hideMenu() {
    menu.hidden = true;
    queryRange = null;
    editor.removeAttribute("aria-activedescendant");
    editor.setAttribute("aria-expanded", "false");
  }
  function update() {
    const { issue, tasks } = context();
    if (!issue.comments?.some((item) => item.id === parentId)) parentId = null;
    const parent = issue.comments?.find((item) => item.id === parentId);
    $("#reply-context", root).hidden = !parent;
    $("#reply-label", root).textContent = parent
      ? `回复 ${parent.author_type === "agent" ? agentById(parent.author_id)?.name || parent.author_id : parent.author_id}`
      : "";
    const summary = routingSummary(body(), issue, tasks, parentId);
    $("#composer-routing", root).textContent = summary.unknown.length
      ? `未匹配：${summary.unknown.map((name) => "@" + name).join("、")}。请从候选列表选择，或删除 @。`
      : summary.label;
    $("#composer-routing", root).classList.toggle(
      "warning",
      summary.unknown.length > 0,
    );
    send.disabled = busy || !body().trim() || summary.unknown.length > 0;
    const enabled = !["backlog", "done", "cancelled"].includes(issue.status);
    editor.contentEditable = String(enabled && !busy);
    if (!enabled) send.disabled = true;
    root.hidden = !enabled;
  }
  function save() {
    if (
      !editor.textContent.trim() &&
      !editor.querySelector("[data-mention-id]")
    )
      editor.replaceChildren();
    storageSet(key, JSON.stringify({ body: body(), parentId }));
    $("#draft-label", root).textContent = body().trim() ? "草稿已保留" : "";
    update();
  }
  function renderOptions() {
    menu.innerHTML = options.length
      ? ["agent", "squad"]
          .map((kind) => {
            const items = options
              .map((item, index) => ({ item, index }))
              .filter(({ item }) => item.kind === kind);
            return items.length
              ? `<div class="mention-group">${kind === "agent" ? "AGENTS" : "SQUADS"}</div>${items.map(({ item, index }) => `<div class="mention-option${index === selected ? " selected" : ""}" id="mention-option-${index}" role="option" aria-selected="${index === selected}" data-option="${index}"><span class="avatar ${kind}">${esc(item.name.slice(0, 1))}</span><span><strong>${esc(item.name)}</strong><small>${esc(item.id)} · ${esc(kind === "squad" ? "Squad Leader" : item.provider)}</small></span><span class="mention-select-hint">↵</span></div>`).join("")}`
              : "";
          })
          .join("")
      : '<div class="mention-empty">没有匹配对象，试试名称或 ID</div>';
    if (options.length) {
      editor.setAttribute(
        "aria-activedescendant",
        `mention-option-${selected}`,
      );
      $(`[data-option="${selected}"]`, menu)?.scrollIntoView({
        block: "nearest",
      });
    } else editor.removeAttribute("aria-activedescendant");
  }
  function suggest() {
    if (composing || busy) return;
    const selection = window.getSelection();
    if (!selection.rangeCount || !selection.isCollapsed) {
      hideMenu();
      return;
    }
    const range = selection.getRangeAt(0),
      node = range.startContainer;
    if (
      node.nodeType !== Node.TEXT_NODE ||
      !editor.contains(node) ||
      node.parentElement.closest("[data-mention-id]")
    ) {
      hideMenu();
      return;
    }
    const before = node.textContent.slice(0, range.startOffset),
      match = /(?:^|\s)@([^\s@]*)$/.exec(before);
    if (!match) {
      hideMenu();
      return;
    }
    queryRange = range.cloneRange();
    queryRange.setStart(node, range.startOffset - match[1].length - 1);
    const query = match[1].toLowerCase();
    options = mentionTargets()
      .filter((item) =>
        `${item.name} ${item.id} ${item.kind}:${item.id}`
          .toLowerCase()
          .includes(query),
      )
      .slice(0, 30);
    selected = 0;
    menu.hidden = false;
    editor.setAttribute("aria-expanded", "true");
    renderOptions();
  }
  function choose(index) {
    if (!queryRange || !options[index]) return;
    const range = queryRange,
      token = chip(options[index]);
    range.deleteContents();
    range.insertNode(token);
    const space = document.createTextNode(" ");
    token.after(space);
    const caret = document.createRange();
    caret.setStart(space, 1);
    caret.collapse(true);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(caret);
    hideMenu();
    editor.focus();
    save();
  }
  function insertPlain(text) {
    const selection = window.getSelection();
    if (!selection.rangeCount || !editor.contains(selection.anchorNode)) return;
    const range = selection.getRangeAt(0);
    range.deleteContents();
    const container = document.createElement("div");
    restoreEditor(container, text);
    const fragment = document.createDocumentFragment();
    fragment.append(...container.childNodes);
    const node = fragment.lastChild || document.createTextNode("");
    if (!fragment.lastChild) fragment.append(node);
    range.insertNode(fragment);
    range.setStartAfter(node);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    save();
    suggest();
  }
  editor.addEventListener("input", () => {
    if (!composing) {
      save();
      suggest();
    }
  });
  editor.addEventListener("compositionstart", () => {
    composing = true;
    hideMenu();
  });
  editor.addEventListener("compositionend", () => {
    composing = false;
    save();
    suggest();
  });
  editor.addEventListener("click", suggest);
  editor.addEventListener("paste", (event) => {
    event.preventDefault();
    insertPlain(event.clipboardData.getData("text/plain"));
  });
  editor.addEventListener("drop", (event) => event.preventDefault());
  editor.addEventListener("keydown", (event) => {
    if (event.isComposing || composing || event.keyCode === 229) return;
    if (!menu.hidden && event.key === " ") {
      const query = queryRange?.toString().slice(1).toLowerCase();
      const exact = options
        .map((item, index) => ({ item, index }))
        .filter(({ item }) =>
          [
            item.name.toLowerCase(),
            item.id.toLowerCase(),
            `${item.kind}:${item.id}`.toLowerCase(),
          ].includes(query),
        );
      if (exact.length === 1) {
        event.preventDefault();
        choose(exact[0].index);
        return;
      }
      hideMenu();
    }
    if (!menu.hidden) {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        if (options.length)
          selected =
            (selected + (event.key === "ArrowDown" ? 1 : -1) + options.length) %
            options.length;
        renderOptions();
        return;
      }
      if (["Enter", "Tab"].includes(event.key) && options.length) {
        event.preventDefault();
        choose(selected);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        hideMenu();
        return;
      }
    }
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      if (!send.disabled) form.requestSubmit();
    }
  });
  menu.addEventListener("mousedown", (event) => {
    const option = event.target.closest("[data-option]");
    if (option) {
      event.preventDefault();
      choose(Number(option.dataset.option));
    }
  });
  $("#clear-reply", root).onclick = () => {
    parentId = null;
    save();
    editor.focus();
  };
  const outside = (event) => {
    if (!root.contains(event.target)) hideMenu();
  };
  document.addEventListener("pointerdown", outside);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    update();
    if (send.disabled || busy) return;
    const submittedBody = body().trim(),
      submittedParent = parentId;
    busy = true;
    hideMenu();
    update();
    send.textContent = "正在发送…";
    $("#composer-error", root).hidden = true;
    try {
      await api(
        `/api/issues/${encodeURIComponent(issueId)}/comments`,
        jsonOptions("POST", {
          body: submittedBody,
          author_id: "local-user",
          parent_comment_id: submittedParent,
        }),
      );
      editor.replaceChildren();
      parentId = null;
      storageRemove(key);
      $("#draft-label", root).textContent = "";
      toast("评论已发送");
      await onSent();
    } catch (error) {
      $("#composer-error", root).textContent =
        `发送失败：${error.message}。草稿已保留，可以重试。`;
      $("#composer-error", root).hidden = false;
    } finally {
      busy = false;
      send.textContent = "发送评论 ↑";
      update();
    }
  });
  save();
  return {
    update,
    reply(id) {
      parentId = id;
      save();
      editor.focus();
      root.scrollIntoView({ block: "nearest", behavior: "smooth" });
    },
    dispose() {
      document.removeEventListener("pointerdown", outside);
    },
  };
}
