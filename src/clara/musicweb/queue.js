// The queue: the player's queue in Music Assistant's order, with the track playing marked. A row plays from there, moves
// one place up or down, or leaves the queue; the head sets shuffle and repeat and clears it.

import { api } from "/api.js";
import { icon } from "/icons.js";
import { clear, confirmDialog, h, pageHead, toast } from "/ui.js";
import { changes, clock, control, cover, failure, player, refreshNow } from "./player.js";

const REFRESH_MS = 10000;

export function mountQueue(container) {
  let open = true;
  let shownUri = null;
  const list = h("ol", { class: "queue", "aria-label": "Queue" });
  const shuffle = h("button", { class: "ghost", type: "button", title: "Shuffle", onclick: () => control("shuffle", !player.now?.shuffle) },
    icon("shuffle", { size: 18 }), h("span", { class: "lbl" }, "Shuffle"));
  const repeat = h("button", { class: "ghost", type: "button", onclick: () => control("repeat", NEXT_REPEAT[player.now?.repeat || "off"]) });
  const clearAll = h("button", { class: "ghost danger", type: "button", "aria-label": "Clear queue", title: "Clear queue", onclick: clearQueue },
    icon("trash", { size: 18 }), h("span", { class: "lbl" }, "Clear"));
  container.append(pageHead("Queue", shuffle, repeat, clearAll), h("div", { class: "scroll" }, h("div", { class: "container wide" }, list)));

  async function load() {
    let queue;
    try {
      queue = await api.get("/v1/me/music/queue");
    } catch (error) {
      if (open) clear(list).append(h("li", {}, failure(error)));
      return;
    }
    if (!open) return;
    clear(list);
    if (!queue.items.length) {
      list.append(h("li", { class: "empty-row" }, "The queue is empty. Play something from Discover or Search."));
      return;
    }
    queue.items.forEach((item, position) => list.append(row(item, position, queue.items.length)));
  }

  function row(item, position, count) {
    const edit = (label, glyph, run, disabled = false) => h("button", {
      class: "ghost icon-btn", type: "button", "aria-label": `${label} ${item.title}`, title: label, disabled,
      onclick: async () => { if (await action(run)) load(); },
    }, icon(glyph, { size: 18 }));
    return h("li", { class: "qrow" + (item.current ? " current" : "") + (item.available ? "" : " unavailable"), "aria-current": item.current ? "true" : null },
      h("span", { class: "qnum" }, item.current ? icon("play", { size: 14 }) : String(position + 1)),
      cover(item.image, "sm"),
      h("button", { class: "qtext", type: "button", title: `Play ${item.title}`, disabled: !item.available,
        onclick: async () => { if (await action(() => api.post("/v1/me/music/queue/jump", { index: item.index }))) load(); } },
      h("strong", {}, item.title), h("span", { class: "muted small" }, item.subtitle)),
      h("span", { class: "qtime muted small" }, clock(item.duration)),
      h("div", { class: "actions" },
        edit("Move up", "up", () => api.post("/v1/me/music/queue/move", { queue_item_id: item.queue_item_id, shift: -1 }), position === 0),
        edit("Move down", "down", () => api.post("/v1/me/music/queue/move", { queue_item_id: item.queue_item_id, shift: 1 }), position === count - 1),
        edit("Remove", "close", () => api.post("/v1/me/music/queue/remove", { index: item.index }))));
  }

  /** Run one change of the queue; a failure is said in a toast. True when it went through. */
  async function action(run) {
    try {
      await run();
    } catch (error) {
      toast(error.detail || String(error), true);
      return false;
    }
    refreshNow();
    return true;
  }

  async function clearQueue() {
    const sure = await confirmDialog("Clear the queue?", "The music waiting to play is taken out. What plays now goes on.", "Clear", true);
    if (sure && await action(() => api.post("/v1/me/music/queue/clear"))) load();
  }

  function drawHead() {
    const now = player.now;
    const on = Boolean(now?.shuffle);
    shuffle.setAttribute("aria-pressed", String(on));
    shuffle.classList.toggle("on", on);
    const mode = now?.repeat || "off";
    repeat.replaceChildren(icon(mode === "one" ? "repeat-one" : "repeat", { size: 18 }), h("span", { class: "lbl" }, `Repeat ${mode}`));
    repeat.setAttribute("aria-label", `Repeat ${mode}`);
    repeat.title = `Repeat ${mode}`;
    repeat.setAttribute("aria-pressed", String(mode !== "off"));
    repeat.classList.toggle("on", mode !== "off");
  }

  /** The queue is read again when the track changes, and every so often. */
  function onChange() {
    drawHead();
    const uri = player.now?.track?.uri ?? null;
    if (uri !== shownUri) {
      shownUri = uri;
      load();
    }
  }

  changes.addEventListener("change", onChange);
  const timer = setInterval(load, REFRESH_MS);
  drawHead();
  load();
  return {
    destroy() {
      open = false;
      clearInterval(timer);
      changes.removeEventListener("change", onChange);
    },
  };
}

const NEXT_REPEAT = { off: "all", all: "one", one: "off" };
