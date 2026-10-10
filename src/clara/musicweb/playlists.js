// Playlists: the playlists of your library in Music Assistant, the favorites first (the ones you marked as favorite), then
// all the others. A playlist plays from its picture, and its menu puts it next or at the end of the queue instead.

import { api } from "/api.js";
import { icon } from "/icons.js";
import { clear, h, pageHead } from "/ui.js";
import { failure } from "./player.js";
import { tile } from "./discover.js";

export function mountPlaylists(container) {
  let open = true;
  const list = h("div", { class: "playlists" });
  const refresh = h("button", { class: "ghost icon-btn", type: "button", "aria-label": "Refresh", title: "Refresh", onclick: load }, icon("refresh"));
  container.append(pageHead("Playlists", refresh), h("div", { class: "scroll" }, h("div", { class: "container wide" }, list)));

  async function load() {
    clear(list).append(h("p", { class: "muted" }, "Loading…"));
    let found;
    try {
      found = (await api.get("/v1/me/music/playlists")).playlists;
    } catch (error) {
      if (open) clear(list).append(failure(error));
      return;
    }
    if (!open) return;
    clear(list);
    if (!found.length) {
      list.append(h("p", { class: "intro" }, "Music Assistant has no playlist in your library yet."));
      return;
    }
    list.append(
      group("Favorites", found.filter((playlist) => playlist.favorite)),
      group("All other playlists", found.filter((playlist) => !playlist.favorite)));
  }

  /** One group of playlists under its name; nothing when the group is empty. */
  function group(name, playlists) {
    if (!playlists.length) return null;
    return h("section", { class: "row-section" },
      h("header", { class: "row-head" }, h("h2", { class: "section-title" }, name), h("span", { class: "muted small" }, String(playlists.length))),
      h("div", { class: "playlist-grid" }, playlists.map(tile)));
  }

  load();
  return { destroy() { open = false; } };
}
