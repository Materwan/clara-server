// Requests for permission: when Clara wants to do something that needs your say (replace a file, delete, commit to the
// main branch…) she goes on with something else and the request waits. It shows as a card in the conversation, and as a
// count in the navigation rail, so that it can be answered from any page. Answering runs the action on the server.

import { api } from "./api.js";
import { clear, h, icon, openDialog, popupMenu, toast } from "./ui.js";

const SURFACE = "web";
const POLL = 10000;
export const CHANGED = "clara:approvals"; // fired on `window` after a request was answered

const LEVEL_WORDS = { read: "Look", write: "Add or change", destructive: "Replace or delete" };
const who = (user) => ({ surface: SURFACE, user_id: user.name });

/** What kind of action, in words a person would use. */
export const levelWord = (level) => LEVEL_WORDS[level] || level;

/** The card of one request. `onDecided(approval)` is called once it was answered here or found answered elsewhere. */
export function approvalCard(approval, user, { onDecided } = {}) {
  const status = h("p", { class: "approval-status", role: "status", hidden: true });
  const approve = h("button", { class: "primary", onclick: () => decide(true) }, icon("check", { size: 17 }), "Approve");
  const deny = h("button", { onclick: () => decide(false) }, "Deny");
  const more = h("button", { class: "ghost icon-btn", "aria-label": "More ways to approve", title: "More ways to approve",
    onclick: (event) => {
      event.stopPropagation();
      popupMenu(more, [
        { label: "Approve, and do not ask again in this conversation", icon: "check", run: () => decide(true, "conversation") },
        { label: "Approve, and do not ask again for this resource", icon: "check", run: () => decide(true, "resource") },
      ]);
    } }, icon("more", { size: 18 }));
  const card = h("article", { class: "approval", "aria-label": "Clara asks for your permission" },
    h("div", { class: "approval-head" }, icon("shield", { size: 19 }),
      h("strong", {}, "Clara asks for your permission"),
      h("span", { class: "badge level-" + approval.level }, levelWord(approval.level))),
    h("p", { class: "approval-summary" }, approval.summary),
    h("p", { class: "muted small" }, `On ${approval.resource || "a resource"}. Nothing is done until you approve.`),
    approval.reason && h("p", { class: "approval-reason small" }, "Clara says: “", approval.reason, "”"),
    h("div", { class: "approval-actions" }, approve, deny, more),
    status);

  async function decide(yes, remember = "") {
    for (const button of [approve, deny, more]) button.disabled = true;
    try {
      const done = await api.post(`/v1/approvals/${approval.id}/decide`, { ...who(user), approve: yes, remember });
      settle(done);
      window.dispatchEvent(new Event(CHANGED));
      onDecided?.(done);
    } catch (error) {
      if (error.status === 409) { // answered somewhere else a moment ago
        toast(error.detail || "This request was already answered.");
        onDecided?.({ ...approval, status: "answered" });
      } else {
        for (const button of [approve, deny, more]) button.disabled = false;
        if (error.status !== 401) toast(error.detail || String(error), true);
      }
    }
  }

  function settle(done) {
    clear(card.querySelector(".approval-actions"));
    status.hidden = false;
    card.classList.add("settled", done.status);
    status.textContent = done.status === "denied" ? "Denied. Nothing was done."
      : done.status === "failed" ? `Approved, but it failed: ${done.result}`
      : `Approved and done. ${done.result || ""}`.trim();
  }
  return card;
}

/** The pending requests of this person, oldest first. */
export async function pendingApprovals(user, conversation) {
  const found = await api.get("/v1/approvals", { ...who(user), status: "pending", conversation });
  return found.approvals.slice().reverse();
}

/** A button for the rail that says how many requests wait for an answer and opens them; hidden when none do. */
export function mountApprovalBadge(user, container) {
  const count = h("span", { class: "count" });
  const button = h("button", { class: "approvals-badge", hidden: true, onclick: open }, icon("shield", { size: 18 }), h("span", { class: "text" }, "Waiting for you"), count);
  container.append(button);
  let pending = [];
  let timer = null;

  async function refresh() {
    if (document.hidden) return;
    try {
      pending = await pendingApprovals(user);
    } catch { return; }
    button.hidden = !pending.length;
        count.textContent = String(pending.length);
    button.title = `${pending.length} request${pending.length === 1 ? "" : "s"} from Clara waiting for your permission`;
  }

  function open() {
    openDialog((close) => {
      const list = h("div", { class: "approval-list" });
      const draw = () => {
        clear(list).append(...(pending.length ? pending.map((item) => h("div", { class: "approval-item" },
          h("p", { class: "muted small" }, `In ${item.conversation}`),
          approvalCard(item, user, { onDecided: () => { pending = pending.filter((p) => p.id !== item.id); refresh(); if (!pending.length) close(); } })))
          : [h("p", { class: "muted" }, "Nothing is waiting for you.")]));
      };
      draw();
      return h("div", { class: "approval-dialog" }, h("h3", {}, "Waiting for your permission"), list,
        h("div", { class: "actions" }, h("button", { onclick: () => close(null) }, "Close")));
    });
  }

  window.addEventListener(CHANGED, refresh);
  document.addEventListener("visibilitychange", refresh);
  timer = setInterval(refresh, POLL);
  refresh();
  return {
    refresh,
    destroy() {
      clearInterval(timer);
      window.removeEventListener(CHANGED, refresh);
      document.removeEventListener("visibilitychange", refresh);
      button.remove();
    },
  };
}
