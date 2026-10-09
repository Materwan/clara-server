// Discover: the rows of Music Assistant's discover page, one after another, as its own web page shows them. Each row
// names the provider it comes from, and its items load on their own, so a slow provider does not hold up the others.

import { api } from "/api.js";
import { icon } from "/icons.js";
import { clear, h, pageHead, popupMenu } from "/ui.js";
import { cover, failure, playUri } from "./player.js";

export function mountDiscover(container) {
  let open = true;
  const rows = h("div", { class: "rows" });
  const refresh = h("button", { class: "ghost icon-btn", type: "button", "aria-label": "Refresh", title: "Refresh", onclick: load }, icon("refresh"));
  container.append(pageHead("Discover", refresh), h("div", { class: "scroll" }, h("div", { class: "container wide" }, rows)));

  async function load() {
    clear(rows).append(h("p", { class: "muted" }, "Loading…"));
    let found;
    try {
      found = await api.get("/v1/me/music/discover");
    } catch (error) {
      if (open) clear(rows).append(failure(error));
      return;
    }
    if (!open) return;
    clear(rows);
    if (!found.rows.length) {
      rows.append(h("p", { class: "intro" }, "Music Assistant has nothing to suggest yet. Search for something to start."));
      return;
    }
    for (const row of found.rows) {
      const strip = h("div", { class: "strip" }, h("p", { class: "muted small" }, "Loading…"));
      rows.append(h("section", { class: "row-section" },
        h("header", { class: "row-head" }, h("h2", { class: "section-title" }, row.name), h("span", { class: "badge" }, row.provider)),
        strip));
      fill(row, strip);
    }
  }

  async function fill(row, strip) {
    let items;
    try {
      items = (await api.get("/v1/me/music/discover/items", { provider: row.provider, item_id: row.item_id })).items;
    } catch (error) {
      if (open) strip.replaceChildren(failure(error));
      return;
    }
    if (!open) return;
    strip.replaceChildren(...(items.length ? items.map(tile) : [h("p", { class: "muted small" }, "Nothing in this row yet.")]));
  }

  load();
  return { destroy() { open = false; } };
}

/** One item of a row: its picture plays it; the menu puts it next or at the end of the queue instead. */
function tile(item) {
  const more = h("button", {
    class: "ghost icon-btn", type: "button", "aria-label": `More for ${item.title}`, title: "More",
    onclick: (event) => itemMenu(event.currentTarget, item),
  }, icon("more", { size: 18 }));
  return h("article", { class: "tile" },
    h("button", { class: "tile-play", type: "button", "aria-label": `Play ${item.title}`, title: `Play ${item.title}`, onclick: () => playUri(item.uri) },
      cover(item.image, "lg"),
      h("span", { class: "play-mark", "aria-hidden": "true" }, icon("play", { size: 18 }))),
    h("div", { class: "tile-foot" },
      h("div", { class: "tile-text" }, h("strong", {}, item.title), h("span", { class: "muted small" }, item.subtitle)),
      more));
}

/** The menu of an item, shared with the search: play it, put it next, or add it at the end. */
export function itemMenu(anchor, item) {
  popupMenu(anchor, [
    { label: "Play now", icon: "play", run: () => playUri(item.uri) },
    { label: "Play next", icon: "next", run: () => playUri(item.uri, "next") },
    { label: "Add to end of queue", icon: "plus", run: () => playUri(item.uri, "end") },
  ]);
}
