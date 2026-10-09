// The Music page: the player of your PC in Music Assistant. You choose it with your own Music Assistant token, see what
// it plays, search the library, and play, queue or stop. Only your player is ever controlled, and the token stays on the
// server.

import { api } from "./api.js";
import { clear, h, pageHead, toast } from "./ui.js";

const TYPES = [["", "Everything"], ["track", "Tracks"], ["album", "Albums"], ["artist", "Artists"], ["playlist", "Playlists"], ["radio", "Radio"]];
const fail = (error) => toast(error.detail || String(error), true);

/** The page. `setup` (your player), `now` (what it plays) and `search` (the library) are drawn from the server's state. */
export function mountMusic(container) {
  const setup = h("section", { class: "panel" });
  const now = h("section", { class: "panel" });
  const search = h("section", { class: "panel" });
  let open = true;
  container.append(pageHead("Music"), h("div", { class: "scroll" }, h("div", { class: "container" }, setup, now, search)));

  async function load() {
    let state;
    try { state = await api.get("/v1/me/music"); } catch (error) { return fail(error); }
    if (!open) return;
    if (!state.server) {
      clear(setup).append(h("div", { class: "panel-body muted" }, "Music Assistant is not set up on this server."));
      clear(now);
      clear(search);
      return;
    }
    drawSetup(state);
    if (state.player) { drawNow(); drawSearch(); } else { clear(now); clear(search); }
  }

  function drawSetup(state) {
    const token = h("input", {
      type: "password", autocomplete: "off", spellcheck: false, "aria-label": "Music Assistant token",
      placeholder: state.player ? "Token (blank: the one you saved)" : "Your Music Assistant token",
    });
    const choices = h("div", { class: "row wrap" });
    const load = h("button", { type: "submit", class: "ghost" }, "Load players");
    clear(setup).append(
      h("header", { class: "panel-head" }, h("h3", {}, "Your player")),
      h("div", { class: "panel-body" },
        h("p", { class: "muted small" }, state.player
          ? "Your player is chosen. Load the players again to choose another one."
          : "Choose the player of this PC. Clara controls only that player, with your own Music Assistant token."),
        h("form", { class: "toolbar", onsubmit: (event) => { event.preventDefault(); loadPlayers(token.value.trim(), choices, load); } }, token, load),
        choices,
        state.player && h("div", { class: "row" }, h("button", { class: "ghost", onclick: forget }, "Forget my player"))));
  }

  async function loadPlayers(token, choices, button) {
    button.disabled = true;
    let found;
    try { found = await api.post("/v1/me/music/players", { token: token || null }); } catch (error) { button.disabled = false; return fail(error); }
    button.disabled = false;
    if (!open) return;
    if (!found.players.length) {
      clear(choices).append(h("span", { class: "muted small" }, "No player is online. Open Music Assistant's player on this PC, then load again."));
      return;
    }
    const select = h("select", { "aria-label": "Player" },
      found.players.map((player) => h("option", { value: player.id }, player.available ? player.name : `${player.name} (offline)`)));
    const use = h("button", { class: "primary", type: "button" }, "Use this player");
    use.onclick = () => choose(select.value, token, use);
    clear(choices).append(select, use);
  }

  async function choose(player, token, button) {
    button.disabled = true;
    try {
      await api.put("/v1/me/music", { player, token: token || null });
    } catch (error) { button.disabled = false; return fail(error); }
    toast("Your player is saved.");
    load();
  }

  async function forget() {
    try { await api.delete("/v1/me/music"); } catch (error) { return fail(error); }
    toast("Your player is forgotten.");
    load();
  }

  function drawNow() {
    const text = h("p", { class: "now-text" }, "Checking…");
    const refresh = async () => {
      try {
        const current = await api.get("/v1/me/music/status");
        text.textContent = describe(current);
      } catch (error) { text.textContent = error.detail || String(error); }
    };
    const stop = h("button", { class: "ghost", type: "button", onclick: async () => {
      try { toast((await api.post("/v1/me/music/stop")).message); } catch (error) { return fail(error); }
      refresh();
    } }, "Stop");
    clear(now).append(
      h("header", { class: "panel-head" }, h("h3", { class: "grow" }, "Now playing"),
        h("button", { class: "ghost", type: "button", onclick: refresh }, "Refresh")),
      h("div", { class: "panel-body" }, text, h("div", { class: "row" }, stop)));
    refresh();
  }

  function describe(current) {
    if (current.state === "stopped") return `${current.name} is stopped.`;
    if (current.state === "unknown") return `${current.name}'s state is unknown.`;
    if (!current.title) return `${current.name} is ${current.state}.`;
    const by = current.artist ? ` by ${current.artist}` : "";
    const album = current.album ? ` (album ${current.album})` : "";
    return `${current.name} is ${current.state}: ${current.title}${by}${album}`;
  }

  function drawSearch() {
    const query = h("input", { type: "search", placeholder: "A title, an artist, an album…", "aria-label": "Search the library", required: true });
    const kind = h("select", { "aria-label": "Kind of item" }, TYPES.map(([id, text]) => h("option", { value: id }, text)));
    const results = h("ul", { class: "list" });
    clear(search).append(
      h("header", { class: "panel-head" }, h("h3", {}, "Search the library")),
      h("div", { class: "panel-body" },
        h("form", { class: "toolbar", onsubmit: (event) => { event.preventDefault(); runSearch(query.value.trim(), kind.value, results); } },
          query, kind, h("button", { class: "primary", type: "submit" }, "Search"))),
      results);
  }

  async function runSearch(text, type, results) {
    if (!text) return;
    let found;
    try { found = await api.get("/v1/me/music/search", { q: text, media_type: type }); } catch (error) { return fail(error); }
    if (!open) return;
    clear(results);
    if (!found.results.length) {
      results.append(h("li", { class: "empty-row" }, `No result for “${text}”.`));
      return;
    }
    for (const item of found.results) {
      const detail = [item.type, item.artist !== "-" ? item.artist : "", item.album !== "-" ? item.album : ""].filter(Boolean).join(" · ");
      results.append(h("li", {},
        h("div", { class: "text" }, h("strong", {}, item.title), h("div", { class: "muted small" }, detail)),
        h("div", { class: "actions" },
          h("button", { class: "ghost", type: "button", onclick: () => play("/v1/me/music/play", { uri: item.uri }) }, "Play"),
          h("button", { class: "ghost", type: "button", onclick: () => play("/v1/me/music/queue", { uri: item.uri, position: "next" }) }, "Play next"),
          h("button", { class: "ghost", type: "button", onclick: () => play("/v1/me/music/queue", { uri: item.uri, position: "end" }) }, "Add to end"))));
    }
  }

  async function play(path, body) {
    try { toast((await api.post(path, body)).message); } catch (error) { return fail(error); }
    drawNow();
  }

  load();
  return {
    destroy() { open = false; },
  };
}
