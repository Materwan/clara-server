// Clara's music site, at /music/: Discover, Playlists, Search and the Queue of your player in Music Assistant, with the player bar
// at the foot of every page. It is the web site's shell, and talks to the same HTTP API, as the surface "web".

import { api, events } from "/api.js";
import { icon, mark } from "/icons.js";
import { clear, h, themeSwitch, toggleRail } from "/ui.js";
import { mountDiscover } from "./discover.js";
import { resume } from "./browser-player.js";
import { mountBar, refreshNow, startPolling } from "./player.js";
import { mountPlaylist } from "./playlist.js";
import { mountPlaylists } from "./playlists.js";
import { mountQueue } from "./queue.js";
import { mountSearch } from "./search.js";
import { mountSetup } from "./setup.js";

const app = document.getElementById("app");
const PAGES = {
  discover: ["Discover", "globe"], playlists: ["Playlists", "playlist"], playlist: ["Playlist", "playlist"],
  search: ["Search", "search"], queue: ["Queue", "queue"], setup: ["Your player", "laptop"],
};
const SIGN_IN = "/#/music"; // the web site signs the person in, then comes back here

let music = null; // /v1/me/music: whether the server has Music Assistant, and the player this person chose
let page = null; // what is mounted: {destroy}
let bar = null;
let pageEl = null;

events.addEventListener("signed-out", () => location.replace(SIGN_IN));

async function loadMusic() {
  music = await api.get("/v1/me/music");
}

function shell() {
  const pages = h("nav", { class: "pages", "aria-label": "Pages" },
    ["discover", "playlists", "search", "queue"].map((id) => h("a", { href: `#/${id}`, "data-page": id }, icon(PAGES[id][1], { size: 19 }), PAGES[id][0])));
  const rail = h("aside", { class: "rail", id: "rail", "aria-label": "Navigation" },
    h("div", { class: "rail-head" },
      h("a", { class: "brand", href: "#/discover" }, mark(28), "Music"),
      h("button", { class: "ghost icon-btn close-rail", type: "button", "aria-label": "Close navigation", onclick: () => toggleRail(false) }, icon("close"))),
    pages,
    h("div", { class: "rail-slot" }), // empty: it takes the room, so the foot (Back to Clara, your player) sits at the bottom
    h("div", { class: "rail-foot" },
      h("a", { class: "back-link", href: "/#/chat" }, icon("back", { size: 17 }), "Back to Clara"),
      h("hr", { class: "rail-divider" }),
      h("div", { class: "settings" },
        h("a", { class: "me", href: "#/setup", "data-page": "setup", title: "Your player and your Music Assistant token" },
          icon("laptop", { size: 19 }),
          h("div", { class: "who" }, h("strong", {}, "Your player"), h("span", { class: "muted small" }, "Player and token"))),
        themeSwitch())));
  pageEl = h("div", { class: "page", id: "page" });
  const barSlot = h("div", { id: "bar-slot" });
  const content = h("main", { class: "content" }, pageEl, barSlot);
  clear(app).append(h("div", { class: "shell" },
    rail,
    h("div", { class: "scrim", onclick: () => toggleRail(false) }),
    content));
  bar = mountBar(barSlot);
}

/** The page the address names. Without a player chosen, every page is the setup: there is nothing to play yet. */
function route() {
  if (!music) return;
  const [path = "", search = ""] = location.hash.slice(2).split("?");
  let name = path.split("/")[0] || "discover";
  if (name === "music") name = "discover";
  if (!Object.hasOwn(PAGES, name)) name = "discover";
  if (!music.player) name = "setup";
  const tab = name === "playlist" ? "playlists" : name; // a playlist opened keeps the Playlists tab lit
  for (const link of document.querySelectorAll(".pages a, .me")) {
    const active = link.dataset.page === tab;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  toggleRail(false);
  page?.destroy();
  clear(pageEl);
  document.title = `${PAGES[name][0]} – Music – Clara`;
  const params = new URLSearchParams(search);
  page = name === "discover" ? mountDiscover(pageEl)
    : name === "playlists" ? mountPlaylists(pageEl)
    : name === "playlist" ? mountPlaylist(pageEl, params)
    : name === "search" ? mountSearch(pageEl, params)
    : name === "queue" ? mountQueue(pageEl)
    : mountSetup(pageEl, { music, changed });
}

/** After a player is chosen or forgotten: the app reads the choice again and goes on from there. */
async function changed() {
  await loadMusic();
  refreshNow();
  if (music.player) location.hash = "#/discover";
  else route();
}

window.addEventListener("hashchange", route);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("rail-open")) toggleRail(false);
});

async function boot() {
  try {
    await api.get("/v1/auth/me", undefined, { quiet401: true });
  } catch (error) {
    if (error.status === 401) return location.replace(SIGN_IN);
    return clear(app).append(h("div", { class: "login-wrap" }, h("main", { class: "login" },
      h("h1", {}, "Clara can't be reached"), h("p", { class: "lede" }, error.detail || String(error)),
      h("button", { class: "primary", type: "button", onclick: () => location.reload() }, "Try again"))));
  }
  try {
    await loadMusic();
  } catch (error) {
    return clear(app).append(h("main", { class: "login" }, h("h1", {}, "Music is not available"), h("p", { class: "lede" }, error.detail || String(error))));
  }
  shell();
  if (!location.hash || location.hash === "#") history.replaceState(null, "", "#/discover"); // no hashchange: drawn once below
  startPolling();
  route();
  resume(); // this browser plays again, if it was turned on
}

boot();
