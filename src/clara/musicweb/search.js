// Search: the library of Music Assistant, grouped by kind as its own search page groups it. The words and the kind are
// kept in the address, so a search can be opened again or shared.

import { api } from "/api.js";
import { icon } from "/icons.js";
import { clear, h, pageHead } from "/ui.js";
import { cover, failure, playUri } from "./player.js";
import { itemMenu } from "./discover.js";

const KINDS = [["", "All"], ["track", "Tracks"], ["album", "Albums"], ["artist", "Artists"], ["playlist", "Playlists"], ["radio", "Radio"]];
const TITLES = { track: "Tracks", album: "Albums", artist: "Artists", playlist: "Playlists", radio: "Radio", audiobook: "Audiobooks", podcast: "Podcasts" };

export function mountSearch(container, params) {
  let open = true;
  let kind = KINDS.some(([id]) => id === params.get("type")) ? params.get("type") : "";
  const input = h("input", {
    type: "search", value: params.get("q") || "", placeholder: "Songs, albums, artists, playlists…",
    "aria-label": "Search the library", autocomplete: "off", enterkeyhint: "search", required: true, autofocus: true,
  });
  const kinds = h("div", { class: "segmented", role: "group", "aria-label": "Kind of item" });
  const results = h("div", { class: "results", "aria-live": "polite" });
  const form = h("form", { class: "search-form", role: "search", onsubmit: (event) => { event.preventDefault(); run(); } },
    input, h("button", { class: "primary", type: "submit" }, icon("search", { size: 18 }), "Search"));

  function drawKinds() {
    clear(kinds).append(...KINDS.map(([id, label]) => h("button", {
      type: "button", "aria-pressed": String(id === kind),
      onclick: () => { kind = id; drawKinds(); if (input.value.trim()) run(); else remember(); },
    }, label)));
  }

  /** Keep the words and the kind in the address, without a page change. */
  function remember() {
    const query = new URLSearchParams();
    if (input.value.trim()) query.set("q", input.value.trim());
    if (kind) query.set("type", kind);
    const rest = query.toString();
    history.replaceState(null, "", `#/search${rest ? "?" + rest : ""}`);
  }

  async function run() {
    const text = input.value.trim();
    remember();
    if (!text) {
      clear(results);
      return;
    }
    clear(results).append(h("p", { class: "muted" }, "Searching…"));
    let found;
    try {
      found = await api.get("/v1/me/music/lookup", { q: text, media_type: kind });
    } catch (error) {
      if (open) clear(results).append(failure(error));
      return;
    }
    if (!open) return;
    clear(results);
    if (!found.groups.length) {
      results.append(h("p", { class: "intro" }, `No result for “${text}”.`));
      return;
    }
    for (const group of found.groups) {
      results.append(h("section", { class: "row-section" },
        h("header", { class: "row-head" },
          h("h2", { class: "section-title" }, TITLES[group.type] || group.type),
          h("span", { class: "muted small" }, String(group.items.length))),
        h("ul", { class: "list results-list" }, group.items.map(resultRow))));
    }
  }

  function resultRow(item) {
    return h("li", {},
      cover(item.image, "md"),
      h("div", { class: "text" }, h("strong", {}, item.title), h("div", { class: "muted small" }, item.subtitle)),
      h("div", { class: "actions" },
        h("button", { class: "ghost", type: "button", onclick: () => playUri(item.uri) }, icon("play", { size: 16 }), "Play"),
        h("button", { class: "ghost icon-btn", type: "button", "aria-label": `More for ${item.title}`, title: "More",
          onclick: (event) => itemMenu(event.currentTarget, item) }, icon("more", { size: 18 }))));
  }

  drawKinds();
  container.append(pageHead("Search"), h("div", { class: "scroll" },
    h("div", { class: "container wide" }, form, kinds, results)));
  if (input.value.trim()) run();
  return { destroy() { open = false; } };
}
