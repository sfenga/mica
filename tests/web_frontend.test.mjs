import test from "node:test";
import assert from "node:assert/strict";
import { markdown } from "../mica/web/lib/markdown.js";
import { routingSummary, serializeEditor } from "../mica/web/lib/composer.js";
import { state } from "../mica/web/lib/state.js";

state.agents = [
  { id: "owner", name: "Owner", provider: "stub" },
  { id: "reviewer", name: "Reviewer", provider: "stub" },
];
state.squads = [
  { id: "frontend", name: "Frontend Squad", leader_agent_id: "owner" },
];
const issue = {
  assignee_type: "agent",
  assignee_id: "owner",
  comments: [
    {
      id: "agent-comment",
      author_type: "agent",
      author_id: "owner",
      source_task_id: "task-1",
    },
    { id: "human-root", author_type: "user", author_id: "user" },
    {
      id: "human-reply",
      author_type: "user",
      author_id: "user",
      parent_comment_id: "human-root",
    },
  ],
};

test("Markdown escapes HTML and disallows executable links", () => {
  const html = markdown(
    "<img src=x onerror=alert(1)>\n\n[x](javascript:alert)\n\n[x](https://example.com)",
  );
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
  assert.ok(html.includes("&lt;img"));
});

test("Markdown renders blocks without parsing code as mentions or HTML", () => {
  const html = markdown(
    "# Heading\n\n- **bold**\n- *italic*\n\n> quote\n\n```js\n@agent:reviewer <script>\n```",
  );
  assert.match(html, /<h2>Heading<\/h2>/);
  assert.match(html, /<ul>/);
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<blockquote>/);
  assert.match(html, /&lt;script&gt;/);
  assert.ok(!html.includes("mention-chip"));
});

test("Both legacy and canonical mentions render names as colored tokens", () => {
  const html = markdown(
    "@agent:reviewer 和 [@旧名称](mention://squad/frontend)",
  );
  assert.match(html, /@Reviewer/);
  assert.match(html, /mention-chip squad/);
  assert.match(html, /@Frontend Squad/);
});

test("Explicit mention wins over reply and default owner; targets deduplicate", () => {
  const summary = routingSummary(
    "[@Reviewer](mention://agent/reviewer) @agent:reviewer",
    issue,
    [{ id: "task-1", agent_id: "owner" }],
    "agent-comment",
  );
  assert.equal(summary.label, "发送给 Reviewer");
  assert.deepEqual(summary.unknown, []);
});

test("Reply recipient follows source task; human-only replies do not claim execution", () => {
  assert.equal(
    routingSummary(
      "继续检查",
      issue,
      [{ id: "task-1", agent_id: "reviewer" }],
      "agent-comment",
    ).label,
    "回复给 Reviewer",
  );
  assert.equal(
    routingSummary("继续讨论", issue, [], "human-reply").label,
    "仅保存回复，不触发新的执行",
  );
  assert.equal(routingSummary("继续检查", issue).label, "发送给 Owner");
});

test("Nested human reply follows the original trigger task, matching the API", () => {
  const tasks = [{ agent_id: "reviewer", merged_comment_ids: ["human-root"] }];
  assert.equal(
    routingSummary("继续检查", issue, tasks, "human-reply").label,
    "回复给 Reviewer",
  );
});

test("Unknown names and IDs cannot appear as successfully routed recipients", () => {
  assert.deepEqual(routingSummary("@missing 检查", issue).unknown, ["missing"]);
  assert.deepEqual(routingSummary("@agent:missing 检查", issue).unknown, [
    "agent:missing",
  ]);
  assert.deepEqual(
    routingSummary("[@Missing](mention://agent/missing)", issue).unknown,
    ["agent:missing"],
  );
});

test("Editor serializes atomic mentions with IDs and preserves line breaks", () => {
  globalThis.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };
  const text = (value) => ({ nodeType: 3, textContent: value });
  const element = (tagName, childNodes = [], dataset = {}) => ({
    nodeType: 1,
    tagName,
    childNodes,
    dataset,
  });
  const mention = {
    ...element("SPAN", [], { mentionKind: "agent", mentionId: "reviewer" }),
    textContent: "@Reviewer",
  };
  const line = element("DIV", [text("第二行")]);
  line.previousSibling = mention;
  const root = element("DIV", [mention, text(" 请检查"), line]);
  assert.equal(
    serializeEditor(root),
    "[@Reviewer](mention://agent/reviewer) 请检查\n第二行",
  );
});

test("Registry, board and issue module graphs load without a build step", async () => {
  const [forms, registry, board, detail] = await Promise.all([
    import("../mica/web/lib/forms.js"),
    import("../mica/web/lib/registry.js"),
    import("../mica/web/lib/board.js"),
    import("../mica/web/lib/issue-detail.js"),
  ]);
  assert.equal(typeof forms.showCreateIssue, "function");
  assert.equal(typeof registry.renderAgents, "function");
  assert.equal(typeof board.renderBoard, "function");
  assert.equal(typeof detail.mountIssue, "function");
});

test("Comment threads keep nested replies, missing parents and cycles exactly once", async () => {
  const { commentThreads } = await import("../mica/web/lib/comments.js");
  const comments = [
    { id: "a" },
    { id: "b", parent_comment_id: "a" },
    { id: "c", parent_comment_id: "b" },
    { id: "orphan", parent_comment_id: "missing" },
    { id: "x", parent_comment_id: "y" },
    { id: "y", parent_comment_id: "x" },
  ];
  const threads = commentThreads(comments);
  assert.deepEqual(
    threads.map((t) => [t.root.id, ...t.replies.map((c) => c.id)]),
    [["a", "b", "c"], ["orphan"], ["x", "y"]],
  );
  assert.equal(
    threads.flatMap((t) => [t.root, ...t.replies]).length,
    comments.length,
  );
});

test("Execution stream preserves tool boundaries, separates subagent text and leaves input immutable", async () => {
  const { compactTimelineEvents, eventPresentation } =
    await import("../mica/web/lib/executions.js");
  const events = [
    { event_type: "agent.text", payload: { text: "hello" } },
    { event_type: "agent.text", payload: { text: " world" } },
    {
      event_type: "agent.tool",
      payload: { name: "reasoning", item: { text: "checking" } },
    },
    {
      event_type: "agent.text",
      payload: { text: "child", parent_tool_use_id: "child" },
    },
    { event_type: "agent.text", payload: { text: "parent" } },
  ];
  const compact = compactTimelineEvents(events);
  assert.equal(compact.length, 4);
  assert.equal(compact[0].payload.text, "hello world");
  assert.equal(events[0].payload.text, "hello");
  assert.deepEqual(eventPresentation(events[2]), {
    label: "思考",
    text: "checking",
    kind: "text",
  });
  assert.equal(
    eventPresentation({ event_type: "new.event" }).label,
    "new.event",
  );
});

test("Theme restores before paint, follows system only on request and tolerates unavailable storage", async () => {
  const { readFile } = await import("node:fs/promises");
  const { runInNewContext } = await import("node:vm");
  const source = await readFile(
    new URL("../mica/web/theme.js", import.meta.url),
    "utf8",
  );
  function boot(saved, blocked = false) {
    const listeners = {},
      media = {
        matches: false,
        addEventListener: (name, handler) => (listeners.media = handler),
      };
    const buttons = ["light", "dark", "system"].map((choice) => ({
      dataset: { themeChoice: choice },
      attributes: {},
      setAttribute(name, value) {
        this.attributes[name] = value;
      },
      addEventListener(name, handler) {
        this.click = handler;
      },
    }));
    const document = {
      documentElement: { dataset: {}, style: {} },
      querySelectorAll: () => buttons,
      addEventListener: (name, handler) => (listeners[name] = handler),
    };
    const storage = {
      value: saved,
      getItem() {
        if (blocked) throw Error("blocked");
        return this.value;
      },
      setItem(key, value) {
        if (blocked) throw Error("blocked");
        this.value = value;
      },
    };
    runInNewContext(source, {
      window: {
        matchMedia: () => media,
        addEventListener: (name, handler) => (listeners[name] = handler),
      },
      document,
      localStorage: storage,
    });
    return { document, buttons, media, listeners, storage };
  }
  const saved = boot("light");
  assert.equal(saved.document.documentElement.dataset.theme, "light");
  saved.listeners.DOMContentLoaded();
  assert.equal(saved.buttons[0].attributes["aria-pressed"], "true");
  saved.buttons[2].click();
  saved.media.matches = true;
  saved.listeners.media();
  assert.equal(saved.document.documentElement.dataset.theme, "dark");
  saved.buttons[0].click();
  saved.listeners.media();
  assert.equal(saved.document.documentElement.dataset.theme, "light");
  assert.equal(saved.storage.value, "light");
  saved.listeners.storage({ key: "mica.theme", newValue: "dark" });
  assert.equal(saved.document.documentElement.style.colorScheme, "dark");
  const blocked = boot(null, true);
  blocked.listeners.DOMContentLoaded();
  blocked.buttons[0].click();
  assert.equal(blocked.document.documentElement.dataset.theme, "light");
  assert.equal(boot("invalid").document.documentElement.dataset.theme, "dark");
});
