// Your player: the player of this PC in Music Assistant, with your own Music Assistant token, or this browser. Only the
// player you choose is ever controlled, and the token is kept on the server, encrypted, and never shown again.

import { api } from "/api.js";
import { clear, h, pageHead, toast } from "/ui.js";
import { browser, diagnostics, onChange, soundNeedsTap, turnOff, turnOn, unlockSound } from "./browser-player.js";
import { changes as nowChanges, failure, player as nowPlaying } from "./player.js";

/** `music` is /v1/me/music as the app last read it; `changed()` runs once a player is chosen or forgotten. */
export function mountSetup(container, { music, changed }) {
  let open = true;
  const body = h("div", { class: "container" });
  container.append(pageHead("Your player"), h("div", { class: "scroll" }, body));

  if (!music.server) {
    body.append(h("div", { class: "notice" }, "Music Assistant is not set up on this server."));
    return { destroy() { open = false; } };
  }

  const removers = [];
  body.append(browserPanel(music, changed, removers), musicPanel(music, changed));
  return {
    destroy() {
      open = false;
      for (const remove of removers) remove();
    },
  };

  /** This browser: turned on, it plays what Music Assistant streams to it, while this page is open. */
  function browserPanel(current, done, removers) {
    const status = h("p", { class: "small", role: "status" });
    const on = h("button", { class: "primary", type: "button", onclick: () => start(on, true) }, "Turn on in this browser");
    const off = h("button", { class: "ghost danger", type: "button", onclick: () => { turnOff(); } }, "Turn off");
    const sound = h("button", { class: "ghost", type: "button", onclick: () => unlockSound() }, "Turn sound on");
    const use = h("button", { class: "primary", type: "button", onclick: () => useBrowser(done) }, "Use this browser as my player");
    const playsOn = h("p", { class: "small", role: "status" });
    const diag = h("p", { class: "small muted" });
    removers.push(onChange(draw));
    nowChanges.addEventListener("change", draw);
    removers.push(() => nowChanges.removeEventListener("change", draw));
    draw();

    async function start(button, withSound) {
      button.disabled = true;
      try {
        await turnOn({ withSound });
      } catch {
        /* the status says why */
      }
      button.disabled = false;
      if (open) draw();
    }

    function draw() {
      const running = browser.status !== "off";
      on.hidden = running;
      off.hidden = !running;
      sound.hidden = !running;
      use.hidden = browser.status !== "connected";
      const chosen = current.player && browser.clientId && current.player.includes(browser.clientId);
      const text = {
        off: "This browser is not a player. Turn it on to hear the music here, while this page is open.",
        connecting: browser.pairing || browser.detail,
        connected: chosen
          ? (soundNeedsTap() ? "This browser is your player. Tap the page once to hear the music here." : "This browser is your player.")
          : "Connected. Choose it as your player to hear the music here.",
        error: `Could not turn on this browser: ${browser.detail}`,
      }[browser.status];
      status.textContent = text || "";
      status.classList.toggle("error", browser.status === "error");
      const name = nowPlaying.now?.name;
      playsOn.textContent = current.player ? `Music plays on: ${name ?? "your player"}.` : "";
      playsOn.classList.toggle("warn", Boolean(browser.status === "connected" && current.player && !chosen));
      playsOn.textContent += browser.status === "connected" && !chosen
        ? " It is not this browser: choose this browser below to hear the music here." : "";
      const info = diagnostics();
      diag.textContent = info
        ? `Browser player: ${info.connected ? "connected" : "not connected"}, ${info.synced ? "clock synced" : "clock not synced yet"}, ` +
          `${info.playing ? "playing" : "not playing"}${info.muted ? ", muted" : ""}, volume ${info.volume}%, ` +
          `${info.format ?? "no stream yet"}, sound ${info.soundReady ? "allowed" : "waiting for a tap"}.`
        : "";
    }

    return h("section", { class: "panel" },
      h("header", { class: "panel-head" }, h("h3", {}, "This browser")),
      h("div", { class: "panel-body stack" },
        status,
        playsOn,
        h("div", { class: "row wrap" }, on, use, sound, off),
        diag));
  }

  /** Music Assistant's players, and the one to use: this PC's, or this browser's once Music Assistant lists it. */
  function musicPanel(current, done) {
    const token = h("input", {
      type: "password", autocomplete: "off", spellcheck: false, "aria-label": "Music Assistant token",
      placeholder: current.player ? "Token (blank: the one you saved)" : "Your Music Assistant token",
    });
    const choices = h("div", { class: "row wrap" });
    const load = h("button", { type: "submit", class: "ghost" }, "Load players");
    const forget = h("button", { class: "ghost danger", type: "button", onclick: async () => {
      try { await api.delete("/v1/me/music"); } catch (error) { return toast(error.detail || String(error), true); }
      toast("Your player is forgotten.");
      done();
    } }, "Forget my player");

    return h("section", { class: "panel" },
      h("header", { class: "panel-head" }, h("h3", {}, "Music Assistant")),
      h("div", { class: "panel-body stack" },
        h("p", { class: "muted small" }, current.player
          ? "Your player is chosen. Load the players again to choose another one."
          : "Choose the player of this PC. Clara controls only that player, with your own Music Assistant token."),
        h("form", { class: "toolbar", onsubmit: (event) => { event.preventDefault(); loadPlayers(token.value.trim(), choices, load); } }, token, load),
        choices,
        current.player && h("div", { class: "row" }, forget)));
  }

  async function loadPlayers(text, box, button) {
    button.disabled = true;
    let found;
    try {
      found = await api.post("/v1/me/music/players", { token: text || null });
    } catch (error) {
      button.disabled = false;
      return clear(box).append(failure(error));
    }
    button.disabled = false;
    if (!open) return;
    if (!found.players.length) {
      clear(box).append(h("span", { class: "muted small" }, "No player is online. Open Music Assistant's player on this PC, then load again."));
      return;
    }
    const select = h("select", { "aria-label": "Player" },
      found.players.map((player) => h("option", { value: player.id }, player.available ? player.name : `${player.name} (offline)`)));
    const use = h("button", { class: "primary", type: "button" }, "Use this player");
    use.onclick = () => choose(select.value, text, use);
    clear(box).append(select, use);
  }

  async function choose(player, text, button) {
    button.disabled = true;
    try {
      await api.put("/v1/me/music", { player, token: text || null });
    } catch (error) {
      button.disabled = false;
      return toast(error.detail || String(error), true);
    }
    toast("Your player is saved.");
    changed();
  }

  /** Choose this browser as the player, once Music Assistant lists it (it is named as this browser says). */
  async function useBrowser(done) {
    let mine = [];
    for (let attempt = 0; attempt < 10 && mine.length !== 1; attempt += 1) {
      let found;
      try {
        found = await api.post("/v1/me/music/players", { token: null });
      } catch (error) {
        return toast(error.detail || String(error), true);
      }
      mine = found.players.filter((player) => player.id === browser.clientId || player.id.endsWith(browser.clientId) || player.name === browser.name);
      if (mine.length !== 1) await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    if (mine.length !== 1) {
      return toast("Music Assistant does not list this browser yet. Wait a moment, then try again.", true);
    }
    try {
      await api.put("/v1/me/music", { player: mine[0].id, token: null });
    } catch (error) {
      return toast(error.detail || String(error), true);
    }
    toast("This browser is your player.");
    done();
  }
}
