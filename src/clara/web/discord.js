// The Discord page (administrators): the bot built into the server, the Discord servers it is in (and where Clara
// may chime in), and the Discord accounts signed in.

import { api } from "./api.js";
import { mark } from "./icons.js";
import { avatar, clear, confirmDialog, duration, h, icon, openDialog, pageHead, toast } from "./ui.js";

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
      h("p", { class: "muted small" }, "Discord accounts that may talk to Clara, and the Clara user each one is signed in as.")),
      h("button", { onclick: async () => { if (await signInDiscordDialog()) load(); } }, icon("link", { size: 18 }), "Sign in an account"));
    if (!accounts.length) {
      clear(accountsPanel).append(head, h("div", { class: "empty-state" }, mark(36), h("strong", {}, "Nobody yet"), "People sign in on Discord with /register or /login, or you sign them in here."));
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

// ---- picking a Discord account (the Discord page and the Users page) -------------------------------------------

const DISCORD_ID = /^(?:discord:|<@!?)?(\d{1,20})>?$/;

/** A search among the people in the running bot's servers; a pasted Discord id is taken as it is (the bot may be
 * stopped, or not share a server with them). `onPick(member)` is told each choice: `{user_id, name?, display_name?, user?}`
 * (`user`: the Clara user the account is signed in as now). With `browse` off, nobody is listed before a name is typed. */
export function discordPicker({ onPick = () => {}, browse = true } = {}) {
  let chosen = null;
  let timer = null;
  let asked = 0;
  const input = h("input", { type: "search", autocomplete: "off", autocapitalize: "none", spellcheck: false, placeholder: "Name, or Discord user id", "aria-label": "Discord account" });
  const status = h("span", { class: "hint", "aria-live": "polite" });
  const list = h("div", { class: "pick-list", role: "group", "aria-label": "People in the bot's servers" });

  const label = (member) => member.display_name ? `${member.display_name} (@${member.name})` : `Discord id ${member.user_id}`;

  function choose(member) {
    chosen = member;
    input.value = member.display_name || member.user_id;
    clear(list);
    status.className = "hint";
    status.textContent = `${label(member)}${member.display_name ? `, id ${member.user_id}` : ""}${member.user ? ` · signed in as ${member.user} now` : ""}`;
    onPick(member);
  }

  async function search() {
    const text = input.value.trim();
    const id = text.match(DISCORD_ID);
    chosen = null;
    const ticket = ++asked;
    if (!text && !browse) {
      clear(list);
      status.className = "hint";
      status.textContent = "Search the bot's servers by name, or paste a Discord user id.";
      return;
    }
    let found;
    try { found = await api.get(`/v1/admin/discord/members?q=${encodeURIComponent(id ? id[1] : text)}`); } catch (error) {
      status.textContent = error.detail || String(error);
      return;
    }
    if (ticket !== asked) return; // an older answer, after a newer question
    clear(list);
    status.className = "hint";
    const exact = id && found.members.find((member) => member.user_id === id[1]);
    if (id) return choose(exact || { user_id: id[1] });
    if (!found.running) status.textContent = "The bot is not running: paste their Discord user id (Discord settings → Advanced → Developer Mode, then right-click them → Copy User ID).";
    else if (!found.members.length) status.textContent = text ? "Nobody by that name in the bot's servers. A Discord user id works too." : "Nobody in the bot's servers yet. A Discord user id works too.";
    else status.textContent = "";
    list.append(...found.members.map((member) => h("button", { type: "button", class: "pick", onclick: () => choose(member) },
      avatar(member.display_name),
      h("span", { class: "grow" }, h("strong", {}, member.display_name), h("span", { class: "muted small" }, ` @${member.name}`)),
      member.user && h("span", { class: "badge", title: "Signed in as" }, member.user))));
  }

  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 200); });
  input.addEventListener("keydown", (event) => { // Enter picks the only match instead of sending the form
    if (event.key !== "Enter" || chosen) return;
    event.preventDefault();
    const options = list.querySelectorAll("button");
    if (options.length === 1) options[0].click();
  });
  search();

  return {
    node: h("div", { class: "picker" }, input, status, list),
    get value() { return chosen ? chosen.user_id : ""; },
    get chosen() { return chosen; },
    /** Text typed but nobody chosen: the form must not go on as if the field were empty. */
    get pending() { return !chosen && input.value.trim() !== ""; },
    complain(text) {
      status.className = "hint error";
      status.textContent = text;
      input.focus();
    },
  };
}

/** Sign a Discord account in as a Clara user, with no password; `user` given, or chosen in the dialog. True when done. */
export async function signInDiscordDialog(user = null) {
  let users = [];
  if (!user) {
    try { users = (await api.get("/v1/admin/users")).users.filter((u) => !u.disabled); } catch (error) { fail(error); return false; }
    if (!users.length) { toast("Add a user first (Administration → Users).", true); return false; }
  }
  const result = await openDialog((close) => {
    const warning = h("p", { class: "notice warn small", hidden: true });
    const target = () => user || select.value;
    const explain = () => {
      const now = picker.chosen && picker.chosen.user;
      warning.hidden = !now || now === target();
      warning.textContent = now ? `This account is signed in as ${now} now: it will be ${target()}'s instead (${now} keeps their memories).` : "";
    };
    const picker = discordPicker({ onPick: explain });
    const select = !user && h("select", { onchange: explain }, users.map((u) => h("option", { value: u.name }, u.person ? `${u.name} (${u.person.name})` : u.name)));
    return h("form", { onsubmit: (event) => {
      event.preventDefault();
      if (!picker.value) return picker.complain(picker.pending ? "Pick someone in the list, or paste a Discord user id." : "Choose the Discord account.");
      close({ user_id: picker.value, user: target(), who: picker.chosen.display_name || `Discord id ${picker.value}` });
    } },
      h("h3", {}, user ? `Sign in a Discord account as ${user}` : "Sign in a Discord account"),
      h("div", { class: "stack" },
        h("label", { class: "field" }, "Discord account", picker.node),
        select && h("label", { class: "field" }, "Clara user", select),
        warning),
      h("p", { class: "muted small" }, "No password is asked: Clara answers them on Discord right away. What she already knows from this Discord account joins the user's memories."),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: () => close(null) }, "Cancel"), h("button", { class: "primary", type: "submit" }, "Sign in")));
  });
  if (!result) return false;
  try {
    await api.post("/v1/admin/discord/accounts", { user_id: result.user_id, user: result.user });
    toast(`${result.who} can talk to Clara as ${result.user}.`);
    return true;
  } catch (error) { fail(error); return false; }
}
