// The Discord page (administrators): the bot built into the server, the Discord servers it is in (and where Clara
// may chime in), and the Discord accounts signed in.

import { api } from "./api.js";
import { mark } from "./icons.js";
import { clear, confirmDialog, duration, h, icon, pageHead, toast } from "./ui.js";

const STATES = {
  running: ["Running", "ok"],
  starting: ["Connecting…", ""],
  stopped: ["Stopped", ""],
  error: ["Stopped after an error", "off"],
  "no-token": ["No token", "off"],
  unavailable: ["Not installed", "off"],
};

const fail = (error) => toast(error.detail || String(error), true);

export function mountDiscord(container) {
  const botPanel = h("section", { class: "panel" });
  const serversPanel = h("section", { class: "panel" });
  const accountsPanel = h("section", { class: "panel" });
  container.append(pageHead("Discord"), h("div", { class: "scroll" }, h("div", { class: "container wide" },
    h("p", { class: "intro" }, "Clara's Discord bot runs inside this server. People talk to her once they have an account (/register or /login on Discord), and get their reminders as private messages."),
    botPanel, serversPanel, accountsPanel)));

  let busy = false;
  let drawnLists = "";

  async function load() {
    let found;
    try { found = await api.get("/v1/admin/discord"); } catch (error) { return fail(error); }
    drawBot(found.bot);
    const key = JSON.stringify([found.default_chime, found.spaces, found.accounts]);
    if (key !== drawnLists) { // the lists are only redrawn when they change: a select being used is not reset
      drawnLists = key;
      drawServers(found);
      drawAccounts(found.accounts);
    }
  }

  // ---- the bot ----

  async function act(action) {
    if (action === "stop" && !await confirmDialog("Stop the bot", "Clara stops answering on Discord until the bot is started again (or the server restarts with AUTO_START_DISCORD_BOT on).", "Stop the bot", true)) return;
    busy = true;
    load();
    try {
      const done = await api.post(`/v1/admin/discord/${action}`, {});
      toast(done.output.replace(/^Discord bot: /, ""));
    } catch (error) { fail(error); }
    busy = false;
    load();
  }

  function drawBot(bot) {
    const [label, tone] = STATES[bot.state] || [bot.state, ""];
    const stat = (name, value) => h("div", {}, h("dt", {}, name), h("dd", {}, value));
    const live = bot.state === "running" || bot.state === "starting";
    const usable = bot.available && bot.token_set;
    const invite = bot.invite_url && h("button", { onclick: () => window.open(bot.invite_url, "_blank", "noopener") }, icon("link", { size: 18 }), "Invite to a server");
    const copy = bot.invite_url && h("button", { class: "ghost", title: "Copy the invite link", onclick: async () => {
      try { await navigator.clipboard.writeText(bot.invite_url); toast("Invite link copied."); } catch { toast(bot.invite_url); }
    } }, icon("copy", { size: 18 }), "Copy link");
    clear(botPanel).append(
      h("div", { class: "panel-head" },
        h("div", { class: "grow" }, h("h3", {}, "Bot ", h("span", { class: `badge ${tone}` }, label)),
          h("p", { class: "muted small" }, bot.auto_start ? "Starts with the server (AUTO_START_DISCORD_BOT=true)." : "Does not start with the server (AUTO_START_DISCORD_BOT is off): start it here or with /discord start.")),
        h("div", { class: "row wrap" },
          !live && h("button", { class: "primary", disabled: busy || !usable, onclick: () => act("start") }, icon("bolt", { size: 18 }), "Start"),
          live && h("button", { disabled: busy, onclick: () => act("restart") }, "Restart"),
          live && h("button", { class: "danger", disabled: busy, onclick: () => act("stop") }, icon("power", { size: 18 }), "Stop"))),
      !bot.available && h("div", { class: "panel-body" }, h("p", { class: "notice warn small" }, "discord.py is not installed on this server: pip install clara-server[discord], then restart it.")),
      bot.available && !bot.token_set && h("div", { class: "panel-body" }, h("p", { class: "notice warn small" }, "Put the bot's token in the server's .env (DISCORD_BOT_TOKEN), then restart the server. It is never shown here.")),
      bot.last_error && h("div", { class: "panel-body" }, h("p", { class: "notice warn small", role: "alert" }, bot.last_error)),
      h("dl", { class: "kv" },
        stat("Discord account", bot.user || "–"),
        stat("Servers", bot.state === "running" ? String(bot.guilds) : "–"),
        stat("Latency", bot.latency_ms != null ? `${bot.latency_ms} ms` : "–"),
        stat("Running for", bot.uptime_seconds != null ? duration(bot.uptime_seconds) : "–")),
      invite && h("div", { class: "panel-body row wrap" }, invite, copy));
  }

  // ---- the servers ----

  function drawServers(found) {
    const toggle = h("input", { type: "checkbox", checked: found.default_chime, onchange: async () => {
      try { await api.patch("/v1/admin/spaces", { default_chime: toggle.checked }); toast("Saved."); load(); } catch (error) { fail(error); }
    } });
    const head = h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Servers"),
      h("p", { class: "muted small" }, "Clara always answers when she is mentioned or replied to. With chime in she may also answer a message that was not for her, when she has something to add (a model call for every message).")));
    const defaults = h("div", { class: "panel-body" }, h("label", { class: "check" }, toggle, "Chime in by default (servers with no choice of their own)"));
    if (!found.spaces.length) {
      clear(serversPanel).append(head, defaults, h("div", { class: "empty-state" }, mark(36), h("strong", {}, "No server yet"), "Invite the bot to a server: it appears here once the bot is running."));
      return;
    }
    const choice = (value) => value === null ? "default" : value ? "on" : "off";
    clear(serversPanel).append(head, defaults, h("table", { class: "grid cards" },
      h("thead", {}, h("tr", {}, ["Server", "Chime in", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, found.spaces.map((space) => {
        const select = h("select", { "aria-label": `Chime in for ${space.name || space.id}`, onchange: async () => {
          const chime = { default: null, on: true, off: false }[select.value];
          try { await api.patch(`/v1/admin/spaces/${encodeURIComponent(space.id)}`, { chime }); toast("Saved."); load(); } catch (error) { fail(error); }
        } }, [["default", `Default (${found.default_chime ? "on" : "off"})`], ["on", "On"], ["off", "Off"]].map(([value, text]) =>
          h("option", { value, selected: choice(space.chime) === value }, text)));
        return h("tr", {},
          h("td", {}, h("strong", {}, space.name || space.id), h("div", { class: "muted small" }, space.id)),
          h("td", { "data-label": "Chime in" }, select),
          h("td", { class: "end" }, space.present ? h("span", { class: "badge ok" }, "Bot present") : h("span", { class: "badge off" }, "Bot gone")));
      }))));
  }

  // ---- the accounts ----

  function drawAccounts(accounts) {
    const head = h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Signed-in accounts"),
      h("p", { class: "muted small" }, "Discord accounts that may talk to Clara, and the Clara user each one is signed in as.")));
    if (!accounts.length) {
      clear(accountsPanel).append(head, h("div", { class: "empty-state" }, mark(36), h("strong", {}, "Nobody yet"), "People sign in on Discord with /register or /login."));
      return;
    }
    clear(accountsPanel).append(head, h("table", { class: "grid cards" },
      h("thead", {}, h("tr", {}, ["Discord account", "Clara user", "Person", ""].map((t) => h("th", {}, t ? t : h("span", { class: "sr-only" }, "Actions"))))),
      h("tbody", {}, accounts.map((account) => h("tr", {},
        h("td", {}, h("strong", {}, account.discord_name || "Unknown name"), h("div", { class: "muted small" }, account.user_id)),
        h("td", { "data-label": "Clara user" }, account.user),
        h("td", { "data-label": "Person" }, account.person || "–"),
        h("td", { class: "end" }, h("button", { class: "danger sm", onclick: async () => {
          if (!await confirmDialog("Sign out", `Sign ${account.discord_name || account.user_id} out of Clara on Discord? They can sign in again with /login.`, "Sign out", true)) return;
          try { await api.delete(`/v1/admin/discord/accounts/${encodeURIComponent(account.user_id)}`); toast("Signed out."); load(); } catch (error) { fail(error); }
        } }, "Sign out")))))));
  }

  load();
  const timer = setInterval(() => { if (!busy) load(); }, 5000);
  return { destroy() { clearInterval(timer); } };
}
