// An album, an artist or a playlist, opened: its picture, what it is by, the buttons that play it, and its tracks (for an
// artist, its albums too). Opened from a title or a subtitle anywhere on the site, at #/item?uri=…

import { api } from "/api.js";
import { icon } from "/icons.js";
import { clear, h, pageHead } from "/ui.js";
import { moreButton, playCover, skeletonTiles, subtitleOf, tile, titleOf } from "./items.js";
import { clock, cover, failure, playUri } from "./player.js";

const KINDS = { album: "Album", artist: "Artist", playlist: "Playlist" };

export function mountDetail(container, params) {
  let open = true;
  const uri = params.get("uri") || "";
  const kind = uri.split("://")[1]?.split("/")[0] || "";
  const heading = h("h1", { class: "title grow" }, KINDS[kind] || "Music");
  const body = h("div", { class: "detail" });
  const back = h("button", { class: "ghost back", type: "button", onclick: goBack }, icon("back", { size: 18 }), "Back");
  container.append(pageHead(heading), h("div", { class: "scroll" }, h("div", { class: "container wide" }, back, body)));

  function goBack() {
    if (history.length > 1) history.back();
    else location.hash = "#/discover";
  }

  async function load() {
    if (!KINDS[kind]) {
      body.append(h("p", { class: "notice warn" }, "That is not something that has a page."));
      return;
    }
    body.append(h("div", { class: "hero skeleton", "aria-hidden": "true" }, h("span", { class: "cover xl" }), h("span", { class: "bar-line" })),
      h("div", { class: "strip" }, skeletonTiles(4)));
    let found;
    try {
      found = await api.get("/v1/me/music/detail", { uri });
    } catch (error) {
      if (open) clear(body).append(failure(error));
      return;
    }
    if (!open) return;
    draw(found);
  }

  function draw({ item, sections }) {
    document.title = `${item.title} – Music – Clara`;
    clear(body).append(hero(item));
    if (!sections.length) body.append(h("p", { class: "intro" }, "Music Assistant lists nothing here yet."));
    for (const section of sections) {
      body.append(h("section", { class: "row-section" },
        h("header", { class: "row-head" }, h("h2", { class: "section-title" }, section.title), h("span", { class: "muted small" }, String(section.items.length))),
        section.type === "album"
          ? h("div", { class: "strip" }, section.items.map(tile))
          : h("ol", { class: "list tracklist" }, section.items.map((track, at) => trackRow(track, at, item)))));
    }
  }

  /** The head of the page: the picture, the name, what it is by, and the three ways to play it. */
  function hero(item) {
    const play = (where, label, glyph, primary) => h("button", {
      class: primary ? "primary" : "", type: "button", onclick: () => playUri(item.uri, where, item.title),
    }, icon(glyph, { size: 18 }), label);
    return h("header", { class: "hero" },
      cover(item.image, "xl"),
      h("div", { class: "hero-text" },
        h("p", { class: "hero-kind small muted" }, KINDS[kind]),
        h("h2", { class: "hero-title" }, item.title),
        item.subtitle && kind !== "artist" ? h("p", { class: "hero-sub" }, subtitleOf(item)) : null),
      h("div", { class: "hero-actions" },
        play("replace", "Play", "play", true), play("next", "Play next", "next", false), play("end", "Add to queue", "plus", false)));
  }

  /** One track: its number (which plays it) or its cover, its title, its length and its menu. */
  function trackRow(track, at, parent) {
    const lead = kind === "album"
      ? h("button", { class: "num-play", type: "button", "aria-label": `Play ${track.title}`, title: `Play ${track.title}`, onclick: () => playUri(track.uri, "replace", track.title) },
        h("span", { class: "num" }, String(at + 1)), h("span", { class: "mark", "aria-hidden": "true" }, icon("play", { size: 16 })))
      : playCover(track, "md");
    return h("li", { class: "track-row" },
      lead,
      h("div", { class: "text" }, titleOf(track), track.subtitle && track.subtitle !== parent.subtitle ? subtitleOf(track) : null),
      h("span", { class: "qtime muted small" }, track.duration ? clock(track.duration) : ""),
      h("div", { class: "actions" }, moreButton(track)));
  }

  load();
  return { destroy() { open = false; } };
}
