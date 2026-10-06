// Restarting the server from the web site: the server pulls the code, updates, stops the careful way and starts again.
// The page that asked waits for /health to say `restarted: <id>` (the new server reads the id from data/restart.json),
// then tells the person it worked and reloads itself to load the new pages. Every administrator sees a badge when a
// restart is worth it (a changed .env, new code on disk or on the remote), and the pages that tell you to restart
// carry the button.

import { api } from "./api.js";
import { clear, h, icon, openDialog, toast } from "./ui.js";

const KEY = "clara-restart"; // {id, at}: a restart this browser asked for and is waiting for
const POLL = 60000;
const WAIT_STEP = 1500;
const WAIT_MAX = 180000;
const STALE = 600000; // a marker older than this is forgotten
export const CHECK = "clara:restart-check"; // fired on `window`: look again whether a restart is needed

const remember = (id) => { try { localStorage.setItem(KEY, JSON.stringify({ id, at: Date.now() })); } catch { /* private mode */ } };
const forget = () => { try { localStorage.removeItem(KEY); } catch { /* private mode */ } };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export const restartStatus = (refresh = false) => api.get("/v1/admin/restart", refresh ? { refresh: true } : undefined);

/** The banner shown while the server is away. */
function banner(text, { bad = false, dismiss = null } = {}) {
  let node = document.getElementById("restart-banner");
  if (!node) {
    node = h("div", { id: "restart-banner", class: "restart-banner", role: "status" });
    document.body.append(node);
  }
  node.className = "restart-banner" + (bad ? " bad" : "");
  clear(node).append(icon("refresh", { size: 18 }), h("span", {}, text),
    dismiss && h("button", { onclick: dismiss }, "Close"));
  return node;
}

const hideBanner = () => document.getElementById("restart-banner")?.remove();

/** Wait until the server that answers is the one that restarted for `id`; then say so and reload. */
async function waitForReturn(id) {
  banner("Restarting the server: it finishes the answers that are running first…");
  const since = Date.now();
  while (Date.now() - since < WAIT_MAX) {
    await sleep(WAIT_STEP);
    try {
      const info = await (await fetch("/health", { cache: "no-store" })).json();
      if (info.restarted === id) {
        forget();
        banner("The server restarted successfully.");
        toast("The server restarted successfully.");
        await sleep(1200);
        location.reload();
        return true;
      }
    } catch { /* it is down: that is expected for a moment */ }
  }
  forget();
  banner("The server did not come back after 3 minutes. Look at its log (data/logs/clara-server.log) or start it by hand.",
    { bad: true, dismiss: hideBanner });
  return false;
}

/** At start: a restart this browser asked for may still be under way (the page was reloaded meanwhile). */
export function resumeRestart() {
  let found = null;
  try { found = JSON.parse(localStorage.getItem(KEY) || "null"); } catch { /* damaged: forgotten */ }
  if (!found) return;
  if (!found.id || Date.now() - found.at > STALE) return forget();
  waitForReturn(found.id);
}

/** What a failed step printed, in a dialog. */
function failedDialog(error) {
  return openDialog((close) => h("div", {}, h("h3", {}, "The server was not restarted"),
    h("p", {}, "It is still running on the code it had."),
    h("pre", { class: "pre restart-output" }, error.detail || String(error)),
    h("div", { class: "actions" }, h("button", { class: "primary", onclick: () => close(true) }, "Close"))));
}

/** Ask, then restart; resolves once the new server is up (or the wait gave up). `reasons`: why it is proposed. */
export async function askRestart(reasons = []) {
  const ok = await openDialog((close) => h("div", {}, h("h3", {}, "Restart the server"),
    h("p", {}, "Clara pulls the latest code, updates what it needs, finishes the answers that are running, then starts again. Nobody can use her for a minute or so."),
    reasons.length > 0 && h("ul", { class: "restart-reasons" }, reasons.map((reason) => h("li", {}, reason.text || reason))),
    h("div", { class: "actions" },
      h("button", { onclick: () => close(false) }, "Cancel"),
      h("button", { class: "primary", onclick: () => close(true) }, icon("refresh", { size: 18 }), "Update and restart"))));
  if (ok !== true) return false;
  banner("Updating…");
  let done;
  try {
    done = await api.post("/v1/admin/restart", { now: false });
  } catch (error) {
    hideBanner();
    if (error.status !== 401) await failedDialog(error);
    return false;
  }
  remember(done.id);
  return waitForReturn(done.id);
}

/** A notice for a page that says "then restart the server": the sentence and the button. */
export function restartNotice(text) {
  return h("div", { class: "notice warn small restart-notice" }, h("span", {}, text),
    h("button", { onclick: () => askRestart() }, icon("refresh", { size: 16 }), "Restart…"));
}

/** For the rail: a button that shows only when a restart is worth it, and why. Administrators only. */
export function mountRestartBadge(user, container) {
  if (!user.is_admin) return { refresh() {}, destroy() {} };
  const button = h("button", { class: "approvals-badge restart-badge", hidden: true, onclick: () => askRestart(reasons) },
    icon("refresh", { size: 18 }), h("span", { class: "text" }, "Restart needed"));
  container.append(button);
  let reasons = [];

  async function refresh() {
    if (document.hidden) return;
    try {
      const status = await restartStatus();
      reasons = status.reasons;
      button.hidden = !status.needed || status.in_progress;
      button.title = reasons.map((reason) => reason.text).join("\n");
    } catch { /* not an administrator any more, or the server is away */ }
  }

  const timer = setInterval(refresh, POLL);
  window.addEventListener(CHECK, refresh);
  document.addEventListener("visibilitychange", refresh);
  refresh();
  return {
    refresh,
    destroy() {
      clearInterval(timer);
      window.removeEventListener(CHECK, refresh);
      document.removeEventListener("visibilitychange", refresh);
      button.remove();
    },
  };
}
