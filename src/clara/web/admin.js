// Administration (for users flagged administrator): users, the server, what Clara knows about people, a console.

import { api } from "./api.js";
import { discordPicker, signInDiscordDialog } from "./discord.js";
import { mark } from "./icons.js";
import {
  ago, avatar, clear, confirmDialog, dateTime, duration, h, icon, openDialog, pageHead, parseTokens, popupMenu, promptDialog, secretDialog,
  toast, tokenCount, usageBar,
} from "./ui.js";

const TABS = [["users", "Users"], ["server", "Server"], ["people", "People & memory"], ["console", "Console"]];

export function mountAdmin(container, me, tab = "users") {
  const body = h("div", {});
  let current = tab;
  let timer = null;

  const tabs = h("div", { class: "segmented admin-tabs", role: "tablist", "aria-label": "Administration sections" });
  const show = (name) => {
    current = name;
    history.replaceState(null, "", `#/admin/${name}`);
    clearInterval(timer);
    timer = null;
    clear(tabs).append(...TABS.map(([id, label]) =>
      h("button", { role: "tab", "aria-selected": String(id === current), onclick: () => show(id) }, label)));
    clear(body);
    ({ users, server, people, console: consoleTab })[current](body, me, (interval) => { timer = interval; });
  };

  container.append(pageHead("Administration"), h("div", { class: "scroll" }, h("div", { class: "container wide" }, tabs, body)));
  show(TABS.some(([id]) => id === tab) ? tab : "users");
  return { destroy() { clearInterval(timer); } };
}

const fail = (error) => toast(error.detail || String(error), true);

/** Run a server console command (`/provider cloud`...) and give its text. */
async function command(line) {
  return (await api.post("/v1/admin/command", { line })).output;
}

// ---- users ------------------------------------------------------------------------------------------------

function users(box, me) {
  const list = h("div", { class: "panel" });
  const count = h("span", { class: "count" });
  let defaultLimit = null; // tokens a day for users with no limit of their own (null: none)
  const defaultButton = h("button", { title: "Tokens a day for users who have no limit of their own", onclick: setDefaultLimit });
  const drawDefault = () => clear(defaultButton).append(icon("edit", { size: 18 }), defaultLimit ? `Default limit: ${tokenCount(defaultLimit)} a day` : "Default limit: none");
  drawDefault();

  async function load() {
    try { defaultLimit = (await api.get("/v1/admin/limits")).default; drawDefault(); } catch { /* the list below says what is wrong */ }
    let found;
    try { found = (await api.get("/v1/admin/users")).users; } catch (error) { return fail(error); }
    count.textContent = `${found.length} ${found.length === 1 ? "person" : "people"} can sign in`;
    clear(list).append(h("table", { class: "grid cards" },
      h("thead", {}, h("tr", {}, ["User", "Role", "Person", "Devices", "Tokens today", "Last sign-in", ""].map((t) => h("th", {}, t ? t : h("span", { class: "sr-only" }, "Actions"))))),
      h("tbody", {}, found.map(row))));
  }

  const discordLabel = (account) => account.discord_name || `Discord id ${account.user_id}`;

  function row(user) {
    const more = h("button", { class: "ghost icon-btn", "aria-label": `Actions for ${user.name}`, title: "Actions" }, icon("more"));
    more.onclick = (event) => {
      event.stopPropagation();
      popupMenu(more, [
        { label: "Generate a new password", icon: "key", run: () => reset(user, true) },
        { label: "Set a password…", icon: "edit", run: () => reset(user, false) },
        { label: user.is_admin ? "Remove administrator rights" : "Make administrator", icon: "admin", run: () => edit(user, { admin: !user.is_admin }) },
        { label: "Set the daily token limit…", icon: "edit", run: () => setLimit(user) },
        user.usage.own_limit !== null && { label: "Use the default token limit", icon: "refresh", run: () => edit(user, { follow_default_limit: true }) },
        { label: user.disabled ? "Enable" : "Disable", icon: "power", run: () => edit(user, { disabled: !user.disabled }) },
        "-",
        !user.disabled && { label: "Sign in a Discord account…", icon: "link", run: async () => { if (await signInDiscordDialog(user.name)) load(); } },
        ...user.discord_accounts.map((account) => ({ label: `Sign out of Discord: ${discordLabel(account)}`, icon: "logout", run: () => signOutDiscord(user, account) })),
        { label: "Sign out everywhere", icon: "logout", run: () => signOut(user) },
        "-",
        { label: "Remove user", icon: "trash", danger: true, run: () => remove(user) },
      ]);
    };
    const discord = user.discord_accounts.length > 0 && h("div", { class: "muted small", title: user.discord_accounts.map((a) => `discord:${a.user_id}`).join(", ") },
      "Discord: ", user.discord_accounts.map(discordLabel).join(", "));
    return h("tr", {},
      h("td", {}, h("div", { class: "user-cell" }, avatar(user.name), h("div", {}, h("strong", {}, user.name), user.name === me.name && h("span", { class: "muted" }, " (you)"), discord))),
      h("td", {}, user.disabled ? h("span", { class: "badge off" }, "Disabled") : user.is_admin ? h("span", { class: "badge admin" }, "Administrator") : h("span", { class: "badge" }, "User")),
      h("td", { "data-label": "Person" }, user.person ? `${user.person.name} (#${user.person.id})` : "None"),
      h("td", { class: "num", "data-label": "Devices", title: [...user.surfaces, ...user.signed_in_accounts].join(", ") },
        String(user.sessions + user.signed_in_accounts.length)),
      h("td", { "data-label": "Tokens today", title: limitNote(user) }, usageBar(user.usage), user.usage.limit && user.usage.own_limit === null && h("span", { class: "muted small" }, " default")),
      h("td", { "data-label": "Last sign-in", title: dateTime(user.last_login_at) }, ago(user.last_login_at)),
      h("td", { class: "end" }, more));
  }

  async function edit(user, change) {
    try { await api.patch(`/v1/admin/users/${encodeURIComponent(user.name)}`, change); toast("Saved."); load(); } catch (error) { fail(error); }
  }

  const limitNote = (user) => user.is_admin ? "Administrators have no limit"
    : user.usage.own_limit === 0 ? "No limit for this user"
    : user.usage.own_limit === null ? (user.usage.limit ? `The default: ${tokenCount(user.usage.limit)} tokens a day` : "No limit (the default)")
    : `${tokenCount(user.usage.own_limit)} tokens a day, set for this user`;

  async function setLimit(user) {
    const current = user.usage.own_limit === null ? "" : user.usage.own_limit === 0 ? "off" : String(user.usage.own_limit);
    const value = await promptDialog(`Daily token limit for ${user.name}`, "Tokens a day (500000, 500k, 2m, or off)", current, "Save", {
      hint: user.is_admin ? "They are an administrator: they have no limit whatever is set here."
        : "Prompt and answer tokens, counted over a day that ends at midnight UTC. “off” means no limit for this user.",
    });
    if (value === null) return;
    if (!value.trim()) return edit(user, { follow_default_limit: true });
    const tokens = parseTokens(value);
    if (tokens === null) return toast("Not a number of tokens: try 500000, 500k, 2m or off.", true);
    edit(user, { token_limit: tokens });
  }

  async function setDefaultLimit() {
    const value = await promptDialog("Default daily token limit", "Tokens a day (500000, 500k, 2m, or off)", defaultLimit ? String(defaultLimit) : "off", "Save", {
      hint: "For every user who has no limit of their own. Administrators never have one.",
    });
    if (value === null) return;
    const tokens = parseTokens(value);
    if (tokens === null) return toast("Not a number of tokens: try 500000, 500k, 2m or off.", true);
    try {
      defaultLimit = (await api.put("/v1/admin/limits/default", { tokens })).default;
      toast("Saved.");
      drawDefault();
      load();
    } catch (error) { fail(error); }
  }

  async function reset(user, generate) {
    let change;
    if (generate) {
      if (!await confirmDialog("New password", `Generate a new password for ${user.name}? They are signed out everywhere.`, "Generate")) return;
      change = { generate_password: true };
    } else {
      const value = await promptDialog(`Password for ${user.name}`, "New password (10 characters or more)", "", "Set password", { type: "password", hint: "They are signed out everywhere." });
      if (!value) return;
      change = { password: value };
    }
    try {
      const done = await api.patch(`/v1/admin/users/${encodeURIComponent(user.name)}`, change);
      if (generate) await secretDialog(`Password for ${user.name}`, "Give it to them; they can change it on the Account page.", done.password);
      else toast("Password set.");
      load();
    } catch (error) { fail(error); }
  }

  async function signOut(user) {
    try {
      const done = await api.post(`/v1/admin/users/${encodeURIComponent(user.name)}/sign-out`, {});
      toast(`${done.signed_out} device(s) signed out.`);
      load();
    } catch (error) { fail(error); }
  }

  async function signOutDiscord(user, account) {
    if (!await confirmDialog("Sign out of Discord", `Sign ${discordLabel(account)} out of ${user.name}? Clara stops answering them on Discord until they sign in again.`, "Sign out", true)) return;
    try { await api.delete(`/v1/admin/discord/accounts/${encodeURIComponent(account.user_id)}`); toast("Signed out."); load(); } catch (error) { fail(error); }
  }

  async function remove(user) {
    if (!await confirmDialog("Remove user", `${user.name} will no longer be able to sign in. Their memories and conversations are kept (erase them under People & memory).`, "Remove", true)) return;
    try { await api.delete(`/v1/admin/users/${encodeURIComponent(user.name)}`); toast("User removed."); load(); } catch (error) { fail(error); }
  }

  async function add() {
    const result = await openDialog((close) => {
      let named = false; // the name was typed: a Discord account chosen afterwards does not replace it
      const name = h("input", { type: "text", autocomplete: "off", autocapitalize: "none", required: true, pattern: "[a-zA-Z0-9][a-zA-Z0-9_.\\-]{0,31}", autofocus: true, oninput: () => { named = name.value !== ""; } });
      const password = h("input", { type: "text", autocomplete: "off", placeholder: "Leave empty to generate one" });
      const admin = h("input", { type: "checkbox" });
      const discord = discordPicker({ browse: false, onPick: (member) => {
        if (!named && member.name) name.value = member.name.toLowerCase().replace(/[^a-z0-9_.-]/g, "").replace(/^[^a-z0-9]+/, "").slice(0, 32);
      } });
      return h("form", { onsubmit: (event) => {
        event.preventDefault();
        if (discord.pending) return discord.complain("Pick someone in the list, paste a Discord user id, or clear the field.");
        close({ name: name.value, password: password.value, admin: admin.checked, discord_id: discord.value || null });
      } },
        h("h3", {}, "Add a user"),
        h("div", { class: "stack" },
          h("label", { class: "field" }, "User name", name, h("span", { class: "hint" }, "Letters, digits, dots, dashes and underscores")),
          h("label", { class: "field" }, "Password", password, h("span", { class: "hint" }, "10 characters or more")),
          h("label", { class: "check" }, admin, "Administrator"),
          h("label", { class: "field" }, "Discord account (optional)", discord.node)),
        h("p", { class: "muted small" }, "With a Discord account, Clara answers them there right away (no /login) and the user starts with what she already knows from it. Otherwise, if Clara knows an account with this name (cli:name, app:name…), the user takes it over with its memories."),
        h("div", { class: "actions" }, h("button", { type: "button", onclick: () => close(null) }, "Cancel"), h("button", { class: "primary", type: "submit" }, "Add user")));
    });
    if (!result) return;
    try {
      const done = await api.post("/v1/admin/users", { ...result, password: result.password || null });
      await secretDialog(`${done.user.name} was added`, `Give them this password${result.discord_id ? " (for the web site and the app; on Discord they are already signed in)" : ""}; they can change it on the Account page.`, done.password);
      load();
    } catch (error) { fail(error); }
  }

  box.append(h("div", { class: "toolbar" }, h("span", { class: "grow count" }, count), defaultButton, h("button", { class: "primary", onclick: add }, icon("plus", { size: 18 }), "Add user")), list);
  load();
}

// ---- server -----------------------------------------------------------------------------------------------

function server(box, me, setTimer) {
  const controls = h("section", { class: "panel" });
  const stats = h("dl", { class: "kv" });
  box.append(
    controls,
    h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, "Status"), h("p", { class: "muted small" }, "Refreshed every 5 seconds."))), stats),
    h("section", { class: "panel danger-zone" },
      h("div", { class: "panel-head" },
        h("div", { class: "grow" }, h("h3", {}, "Stop the server"),
          h("p", { class: "muted small" }, "Running answers finish first and every client is told. If a service manager restarts Clara, it comes back.")),
        h("button", { class: "danger", onclick: stop }, icon("power", { size: 18 }), "Stop…"))));

  const stat = (label, value) => h("div", {}, h("dt", {}, label), h("dd", {}, value));

  async function load() {
    let status;
    try { status = await api.get("/v1/admin/status"); } catch { return; }
    clear(stats).append(
      stat("Running for", status.stopping ? "Stopping…" : duration(status.uptime_seconds)),
      stat("Answers", `${status.turns.running} running, ${status.turns.since_start} since start`),
      stat("Tokens read", status.tokens.prompt.toLocaleString()),
      stat("Tokens written", status.tokens.completion.toLocaleString()),
      stat("Memory", `${status.people} people, ${status.facts} facts`),
      stat("Listening on", status.listen),
      stat("Tailscale", status.tailscale.mode === "off" ? "Off" : status.tailscale.url || `Unavailable: ${status.tailscale.problem || "starting…"}`));
    drawControls(status);
  }

  let drawn = "";
  async function drawControls(status) {
    const key = status.provider.id + status.model;
    if (key === drawn) return;
    drawn = key;
    let models = { models: [], model: status.model };
    try { models = await api.get("/v1/admin/models"); } catch { /* the select stays empty */ }
    const provider = h("select", { onchange: async () => { await run(`/provider ${provider.value}`); } },
      status.providers.map((p) => h("option", { value: p.id, selected: p.id === status.provider.id, disabled: !p.usable }, `${p.label} (${p.id})${p.usable ? "" : ", no key"}`)));
    const names = models.models.includes(status.model) ? models.models : [status.model, ...models.models];
    const model = h("select", { onchange: async () => { await run(`/model ${model.value}`); } },
      names.map((name) => h("option", { value: name, selected: name === status.model }, name)));
    clear(controls).append(
      h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, "Language model"),
        h("p", { class: "muted small" }, "Changes apply to every client at once, without a restart, and everybody is told."))),
      h("div", { class: "panel-body stack" },
        h("div", { class: "model-pick" }, h("label", { class: "field" }, "Provider", provider), h("label", { class: "field" }, "Model", model)),
        models.error && h("p", { class: "error small" }, models.error)));
  }

  async function run(line) {
    try {
      const output = await command(line);
      toast(output.split("\n")[0]);
    } catch (error) { fail(error); }
    drawn = "";
    load();
  }

  async function stop() {
    if (!await confirmDialog("Stop the server", "Running answers finish first; new questions are refused. Nobody can use Clara until it is started again.", "Stop the server", true)) return;
    try { toast((await command("/stop")).split("\n")[0]); } catch (error) { fail(error); }
  }

  load();
  setTimer(setInterval(load, 5000));
}

// ---- people and memory -------------------------------------------------------------------------------------

function people(box) {
  const listBox = h("div", { class: "person-list", role: "list" });
  const detail = h("div", { class: "person-detail" });
  box.append(h("div", { class: "people" },
    h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", {}, "People")), listBox),
    detail));
  let selected = null;

  const placeholder = () => clear(detail).append(h("section", { class: "panel" }, h("div", { class: "empty-state" }, mark(36),
    h("strong", {}, "Pick someone"), "Their facts, accounts and the tools to link or erase them appear here.")));
  placeholder();

  async function load() {
    let found;
    try { found = (await api.get("/v1/admin/people")).people; } catch (error) { return fail(error); }
    const choose = (p) => { selected = p.id; load(); show(p); };
    clear(listBox).append(...found.map((p) => h("div", {
      class: "person" + (p.id === selected ? " active" : ""), role: "listitem", tabindex: 0,
      onclick: () => choose(p), onkeydown: (e) => { if (e.key === "Enter") choose(p); },
    },
    avatar(p.name),
    h("div", { class: "info" },
      h("div", {}, h("strong", {}, p.name), " ", p.user && h("span", { class: "badge admin" }, "Can sign in")),
      h("div", { class: "sub" }, `${p.facts} ${p.facts === 1 ? "fact" : "facts"}, ${p.relation === null ? "no relationship" : `relationship ${p.relation}/100`}, ${p.accounts.join(", ") || "no account"}`)))));
    if (!found.length) listBox.append(h("p", { class: "rail-note" }, "Nobody yet."));
    if (selected !== null) {
      const again = found.find((p) => p.id === selected);
      if (!again) { selected = null; placeholder(); }
    }
  }

  async function show(person) {
    let body;
    try { body = await api.get(`/v1/admin/people/${person.id}/facts`); } catch (error) { return fail(error); }
    const text = h("input", { type: "text", placeholder: "Add a fact", maxLength: 300, "aria-label": "New fact" });
    clear(detail).append(h("section", { class: "panel" },
      h("div", { class: "panel-head" }, avatar(person.name),
        h("div", { class: "grow" }, h("h3", {}, person.name, " ", h("span", { class: "muted small" }, `#${person.id}`)),
          h("p", { class: "muted small" }, "Accounts: " + (person.accounts.join(", ") || "none")))),
      h("ul", { class: "list" }, body.facts.length ? body.facts.map((fact) => h("li", {}, h("span", { class: "fact-dot" }), h("span", { class: "text" }, fact.text),
        h("button", { class: "ghost icon-btn danger forget", title: "Delete this fact", "aria-label": `Delete fact: ${fact.text}`, onclick: async () => {
          try { await api.delete(`/v1/admin/people/${person.id}/facts/${fact.id}`); show(person); load(); } catch (error) { fail(error); }
        } }, icon("trash", { size: 18 })))) : h("li", { class: "empty-row" }, "No facts yet.")),
      h("form", { class: "panel-body add-form", onsubmit: async (event) => {
        event.preventDefault();
        if (!text.value.trim()) return;
        try { await api.post(`/v1/admin/people/${person.id}/facts`, { text: text.value }); show(person); load(); } catch (error) { fail(error); }
      } }, text, h("button", { type: "submit" }, icon("plus", { size: 18 }), "Add fact"))),
    relationPanel(person),
    h("section", { class: "panel" }, h("div", { class: "panel-body row wrap" },
      h("button", { onclick: () => link(person) }, icon("link", { size: 18 }), "Link an account…"),
      h("span", { class: "grow" }),
      h("button", { class: "danger", onclick: () => erase(person) }, icon("trash", { size: 18 }), "Erase this person…"))));
  }

  function relationPanel(person) {
    const value = h("input", { type: "number", min: 0, max: 100, step: 1, value: person.relation ?? "", placeholder: "none", "aria-label": "Relationship, 0 to 100", style: "width: 7rem" });
    const save = async (relation) => {
      try {
        const done = await api.patch(`/v1/admin/people/${person.id}`, { relation });
        person.relation = done.relation;
        value.value = done.relation ?? "";
        toast(done.relation === null ? "Relationship reset." : `Relationship: ${done.relation}/100 (${done.relation_label}).`);
        load();
      } catch (error) { fail(error); }
    };
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, "Relationship"),
        h("p", { class: "muted small" }, "0 to 100. It sets Clara's tone with them on every surface, and she moves it herself when they are friendly or rude."))),
      h("form", { class: "panel-body add-form", onsubmit: (event) => {
        event.preventDefault();
        const number = Number(value.value);
        if (value.value === "" || !Number.isInteger(number) || number < 0 || number > 100) return toast("A whole number from 0 to 100.", true);
        save(number);
      } }, value, h("button", { type: "submit" }, "Save"), h("button", { type: "button", onclick: () => save(null) }, "Reset")));
  }

  async function link(person) {
    const value = await promptDialog(`Link an account to ${person.name}`, "Account (surface:user, e.g. discord:1234)", "", "Link", {
      hint: "If that account already has its own memories they are merged into this person, which cannot be undone." });
    if (!value) return;
    try {
      toast((await command(`/link ${value.trim()} ${person.id}`)).split("\n")[0]);
      selected = person.id;
      load();
    } catch (error) { fail(error); }
  }

  async function erase(person) {
    let found;
    try { found = await api.get(`/v1/admin/people/${person.id}/footprint`); } catch (error) { return fail(error); }
    const detailText = `${found.accounts} account(s), ${found.facts} fact(s), ${found.messages} message(s) in ${found.conversations} conversation(s)`;
    if (!await confirmDialog(`Erase ${person.name}?`, `This erases ${detailText}, and their login if any. There is no undo. (The traffic log is not erased.)`, "Erase for good", true)) return;
    try {
      toast((await command(`/forget-person ${person.id} confirm`)).split("\n")[0]);
      selected = null;
      placeholder();
      load();
    } catch (error) { fail(error); }
  }

  load();
}

// ---- console -----------------------------------------------------------------------------------------------

function consoleTab(box) {
  const out = h("div", { class: "console-out", role: "log", tabindex: 0, "aria-label": "Console output" });
  const input = h("input", { type: "text", placeholder: "/help", autocomplete: "off", autocapitalize: "none", spellcheck: false, list: "console-commands", "aria-label": "Command" });
  const datalist = h("datalist", { id: "console-commands" });
  const history = [];
  let at = 0;

  const print = (text, kind = "") => { out.append(h("div", { class: kind }, text)); out.scrollTop = out.scrollHeight; };

  async function run(event) {
    event.preventDefault();
    const line = input.value.trim();
    if (!line) return;
    history.push(line);
    at = history.length;
    input.value = "";
    print("> " + line, "cmd");
    try {
      const result = await api.post("/v1/admin/command", { line });
      if (result.output) print(result.output, result.output.startsWith("!") ? "err" : "");
      if (result.quit) print("(this console closes only the page; use /stop to stop the server)", "note");
    } catch (error) { print(error.detail || String(error), "err"); }
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowUp" && history.length) { at = Math.max(0, at - 1); input.value = history[at]; event.preventDefault(); }
    if (event.key === "ArrowDown" && history.length) { at = Math.min(history.length, at + 1); input.value = history[at] || ""; event.preventDefault(); }
  });

  box.append(
    h("p", { class: "intro" }, "The same commands as the server's own console and clara-admin. Passwords made by /user are shown here only once."),
    h("section", { class: "console" }, out,
      h("form", { onsubmit: run }, h("span", { class: "prompt", "aria-hidden": "true" }, ">"), input, datalist, h("button", { class: "primary", type: "submit" }, "Run"))));
  api.get("/v1/admin/commands").then((commands) => {
    datalist.append(...commands.map((c) => h("option", { value: "/" + c.name }, c.summary)));
  }).catch(() => {});
  print("Type /help to see the commands.", "note");
  input.focus();
}
