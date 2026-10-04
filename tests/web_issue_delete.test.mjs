import test from "node:test";
import assert from "node:assert/strict";
import { showDeleteIssue, closeModal, configureForms } from "../mica/web/lib/forms.js";
import { state } from "../mica/web/lib/state.js";

function fixture() {
  const elements = new Map();
  const element = () => ({
    dataset: {}, hidden: true, disabled: false, textContent: "", isConnected: true,
    classList: { toggle() {} }, setAttribute() {}, removeAttribute() {}, focus() {},
    remove() {}, append() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  });
  for (const selector of [".modal", "#modal-content", "#modal-backdrop", "[data-close-modal]",
    "#cancel-delete", "#confirm-delete", "#delete-error", "#toast-region"]) elements.set(selector, element());
  const buttons = [elements.get("[data-close-modal]"), elements.get("#cancel-delete"), elements.get("#confirm-delete")];
  elements.get("#modal-content").querySelectorAll = (selector) => selector === "button" ? buttons : [];
  globalThis.document = {
    activeElement: element(), querySelector: (selector) => elements.get(selector) || null,
    querySelectorAll: () => [], createElement: element,
  };
  globalThis.requestAnimationFrame = (callback) => callback();
  // Toast timers are irrelevant to the delete request state machine.
  const originalTimeout = globalThis.setTimeout;
  globalThis.setTimeout = () => 0;
  state.issues = [{ id: "delete-test", title: "<test>" }, { id: "keep", title: "Keep" }];
  state.tasks = [{ id: "task", issue_id: "delete-test", status: "running" }];
  return { elements, buttons, restore: () => { globalThis.setTimeout = originalTimeout; } };
}

test("delete confirmation blocks close and duplicate submits until success", async () => {
  const { elements, buttons, restore } = fixture();
  try {
    let complete, calls = 0, deleted;
    configureForms({ onIssueDeleted: async (id) => { deleted = id; } });
    globalThis.fetch = async (url, options) => {
      calls++;
      assert.equal(url, "/api/issues/delete-test");
      assert.equal(options.method, "DELETE");
      return await new Promise((resolve) => complete = resolve);
    };
    showDeleteIssue(state.issues[0]);
    assert.match(elements.get("#modal-content").innerHTML, /&lt;test&gt;/);
    const pending = elements.get("#confirm-delete").onclick();
    assert.ok(buttons.every((button) => button.disabled));
    closeModal();
    assert.equal(elements.get("#modal-backdrop").hidden, false);
    await elements.get("#confirm-delete").onclick();
    assert.equal(calls, 1);
    complete({ ok: true, status: 204, text: async () => "" });
    await pending;
    assert.equal(elements.get("#modal-backdrop").hidden, true);
    assert.equal(deleted, "delete-test");
    assert.deepEqual(state.issues.map((item) => item.id), ["keep"]);
    assert.deepEqual(state.tasks, []);
  } finally { restore(); }
});

test("failed delete keeps history visible and enables cancel and retry", async () => {
  const { elements, buttons, restore } = fixture();
  try {
    let calls = 0;
    configureForms({ onIssueDeleted: async () => {} });
    globalThis.fetch = async () => {
      calls++;
      return calls === 1 ? { ok: false, status: 500, text: async () => JSON.stringify({ detail: "删除失败" }) }
        : { ok: true, status: 204, text: async () => "" };
    };
    showDeleteIssue(state.issues[0]);
    await elements.get("#confirm-delete").onclick();
    assert.equal(elements.get("#modal-backdrop").hidden, false);
    assert.equal(elements.get("#delete-error").textContent, "删除失败");
    assert.ok(buttons.every((button) => !button.disabled));
    assert.equal(state.issues.length, 2);
    await elements.get("#confirm-delete").onclick();
    assert.equal(calls, 2);
    assert.equal(state.issues.length, 1);
  } finally { restore(); }
});

test("refresh failure after successful delete cannot offer another delete", async () => {
  const { elements, restore } = fixture();
  try {
    configureForms({ onIssueDeleted: async () => { throw new Error("refresh failed"); } });
    globalThis.fetch = async () => ({ ok: true, status: 204, text: async () => "" });
    showDeleteIssue(state.issues[0]);
    await elements.get("#confirm-delete").onclick();
    assert.equal(elements.get("#modal-backdrop").hidden, true);
    assert.equal(state.issues.length, 1);
  } finally { restore(); }
});
