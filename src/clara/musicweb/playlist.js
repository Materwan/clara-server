// A playlist, opened: its picture, its name and who made it, the buttons that play it, and its tracks in the playlist's
// order. A track plays from its title, and its menu puts it next or at the end of the queue. Opened from a playlist's cover
// or title, at #/playlist?uri=…

import { api } from "/api.js";
import { icon } from "/icons.js";
import { clear, h, pageHead } from "/ui.js";
import { moreButton } from "./items.js";
import { clock, cover, failure, playUri } from "./player.js";

export function mountPlaylist(container, params) {
  let open = true;
  const uri = params.get("uri") || "";
  const body = h("div", { class: "playlist" });
  const back = h("button", { class: "ghost back", type: "button", onclick: goBack }, icon("back", { size: 18 }), "Playlists");
  container.append(pageHead("Playlist"), h("div", { class: "scroll" }, h("div", { class: "container wide" }, back, body)));

  function goBack() {
    if (history.length > 1) history.back();
    else location.hash = "#/playlists";
  }

  async function load() {
    if (!uri) {
      body.append(h("p", { class: "notice warn" }, "No playlist was named."));
      return;
    }
    body.append(h("p", { class: "muted" }, "Loading…"));
    let found;
    try {
      found = await api.get("/v1/me/music/playlist", { uri });
    } catch (error) {
      if (open) clear(body).append(failure(error));
      return;
    }
    if (!open) return;
    draw(found);
  }

  function draw({ playlist, tracks }) {
    document.title = `${playlist.title} – Music – Clara`;
    const total = tracks.reduce((sum, track) => sum + (track.duration || 0), 0);
    clear(body).append(hero(playlist, tracks.length, total),
      tracks.length
        ? h("ol", { class: "queue tracklist", "aria-label": "Tracks" }, tracks.map(trackRow))
        : h("p", { class: "intro" }, "This playlist has no tracks yet."));
  }

  /** The head of the page: the picture, the name, who made it and how long it is, and the three ways to play it. */
  function hero(playlist, count, total) {
    const play = (where, label, glyph, primary) => h("button", {
      class: primary ? "primary" : "ghost", type: "button", onclick: () => playUri(playlist.uri, where, playlist.title),
    }, icon(glyph, { size: 18 }), label);
    const facts = [playlist.subtitle === "Playlist" ? "" : playlist.subtitle, count ?`${count} ${count === 1 ? "track" : "tracks"}` : "", total ? clock(total) : ""];
    return h("header", { class: "hero" },
      cover(playlist.image, "lg"),
      h("div", { class: "hero-text" },
        h("p", { class: "hero-kind small muted" }, playlist.favorite ? "Favorite playlist" : "Playlist"),
        h("h2", { class: "hero-title" }, playlist.title),
        h("p", { class: "hero-sub muted small" }, facts.filter(Boolean).join(" · ")),
        h("div", { class: "hero-actions" },
          play("replace", "Play", "play", true), play("next", "Play next", "next", false), play("end", "Add to queue", "plus", false))));
  }

  /** One track: its number, its picture, its title (which plays it), its length and its menu. */
  function trackRow(track, at) {
    return h("li", { class: "qrow" },
      h("span", { class: "qnum" }, String(at + 1)),
      cover(track.image, "sm"),
      h("button", { class: "qtext", type: "button", title: `Play ${track.title}`, onclick: () => playUri(track.uri, "replace", track.title) },
        h("strong", {}, track.title), h("span", { class: "muted small" }, track.subtitle)),
      h("span", { class: "qtime muted small" }, track.duration ? clock(track.duration) : ""),
      h("div", { class: "actions" }, moreButton(track)));
  }

  load();
  return { destroy() { open = false; } };
}
