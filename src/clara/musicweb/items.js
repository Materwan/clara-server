// The things that Discover, Search and the album, artist and playlist pages all show: a cover that plays, a title that opens
// its page, a menu. One idiom for each, so that the same thing looks and acts the same everywhere.

import { icon } from "/icons.js";
import { h, popupMenu } from "/ui.js";
import { cover, playUri } from "./player.js";

const OPENS = new Set(["album", "artist", "playlist"]);

export const href = (uri) => `#/item?uri=${encodeURIComponent(uri)}`;
export const playlistHref = (uri) => `#/playlist?uri=${encodeURIComponent(uri)}`;

/** The uri of the page an item opens: its own for an album, an artist or a playlist, its album for a track. */
export const opens = (item) => (OPENS.has(item.type) ? item.uri : item.type === "track" ? item.album_uri || "" : "");

/** The address of that page: a playlist has its own page, the other things their item page. */
const pageOf = (item) => (item.type === "playlist" ? playlistHref : href)(opens(item));

/** The title of an item: a link to its page when it has one. */
export function titleOf(item) {
  const target = opens(item);
  return target
    ? h("a", { class: "item-title", href: pageOf(item), title: item.title }, item.title)
    : h("strong", { class: "item-title", title: item.title }, item.title);
}

/** What the item is by: a link to the artist when the item knows it. */
export function subtitleOf(item) {
  if (!item.subtitle) return null;
  return item.artist_uri
    ? h("a", { class: "item-sub small", href: href(item.artist_uri), title: item.subtitle }, item.subtitle)
    : h("span", { class: "item-sub muted small", title: item.subtitle }, item.subtitle);
}

/** The cover of an item, as the button that plays it. The mark shows on hover and focus, and always on a touch screen.
 *  A playlist's cover opens its page instead. */
export function playCover(item, size) {
  if (item.type === "playlist") {
    return h("a", { class: `cover-play ${size}`, href: playlistHref(item.uri), "aria-label": `Open ${item.title}`, title: `Open ${item.title}` },
      cover(item.image, size));
  }
  return h("button", {
    class: `cover-play ${size}`, type: "button", "aria-label": `Play ${item.title}`, title: `Play ${item.title}`,
    onclick: () => playUri(item.uri, "replace", item.title),
  }, cover(item.image, size), h("span", { class: "play-mark", "aria-hidden": "true" }, icon("play", { size: size === "lg" ? 18 : 16 })));
}

/** The menu of an item: play it, put it next, add it at the end, or go to its album or its artist. */
export function itemMenu(anchor, item) {
  popupMenu(anchor, [
    { label: "Play now", icon: "play", run: () => playUri(item.uri, "replace", item.title) },
    { label: "Play next", icon: "next", run: () => playUri(item.uri, "next", item.title) },
    { label: "Add to end of queue", icon: "plus", run: () => playUri(item.uri, "end", item.title) },
    (item.album_uri || item.artist_uri) && "-",
    item.album_uri && { label: "Go to album", icon: "disc", run: () => { location.hash = href(item.album_uri); } },
    item.artist_uri && { label: "Go to artist", icon: "user", run: () => { location.hash = href(item.artist_uri); } },
  ]);
}

export function moreButton(item) {
  return h("button", {
    class: "ghost icon-btn", type: "button", "aria-label": `More for ${item.title}`, title: "More",
    onclick: (event) => itemMenu(event.currentTarget, item),
  }, icon("more", { size: 18 }));
}

/** One item of a strip: its cover plays it, its title opens it, the menu does the rest. */
export function tile(item) {
  return h("article", { class: "tile" },
    playCover(item, "lg"),
    h("div", { class: "tile-foot" },
      h("div", { class: "tile-text" }, titleOf(item), subtitleOf(item)),
      moreButton(item)));
}

/** One item of a list (the search, an album's albums): cover, title, subtitle, menu. */
export function listRow(item) {
  return h("li", { class: "item-row" },
    playCover(item, "md"),
    h("div", { class: "text" }, titleOf(item), subtitleOf(item)),
    h("div", { class: "actions" }, moreButton(item)));
}

/** Tiles of nothing yet, the size of what comes: the page does not jump when the covers arrive. */
export function skeletonTiles(count = 7) {
  return Array.from({ length: count }, () => h("div", { class: "tile skeleton", "aria-hidden": "true" },
    h("span", { class: "cover lg" }), h("span", { class: "bar-line" }), h("span", { class: "bar-line short" })));
}
