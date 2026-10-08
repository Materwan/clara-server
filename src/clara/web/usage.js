// The tokens people used (Discord apart from every other surface), the models they prefer, and the history of every
// call: for administrators about everybody (Admin, Usage), for each person about themselves (the Usage page).
// The person's page also holds their own API keys (one per provider) and what they used on them, apart from the
// server's key and the credits.

import { api } from "./api.js";
import { mark } from "./icons.js";
import { ago, avatar, clear, confirmDialog, dateTime, h, icon, pageHead, toast, tokenCount, usageBar } from "./ui.js";

const PERIODS = [[0, "All time"], [1, "24 hours"], [7, "7 days"], [30, "30 days"], [90, "90 days"]];
const GROUPS = [["", "Every surface"], ["discord", "Discord only"], ["other", "Everything but Discord"]];
const KINDS = [["", "Every kind"], ["message", "Messages"], ["scheduled", "Scheduled by Clara"], ["compaction", "Compactions"], ["title", "Titles"]];
const KIND_LABEL = Object.fromEntries(KINDS.filter(([id]) => id));

const fail = (error) => toast(error.detail || String(error), true);
const total = (part) => part.prompt_tokens + part.completion_tokens;
const pair = (part) => `${tokenCount(part.prompt_tokens)} in / ${tokenCount(part.completion_tokens)} out`;
const modelName = (model) => model.model.includes(":") ? model.model : `${model.provider ? model.provider + ":" : ""}${model.model}`;

function select(label, options, value, onchange) {
  return h("select", { "aria-label": label, onchange: (event) => onchange(event.target.value) },
    options.map(([id, text]) => h("option", { value: String(id), selected: String(id) === String(value) }, text)));
}

/** The page of a person about their own use. */
export function mountUsage(container) {
  const box = h("div", {});
  container.append(pageHead("Usage"), h("div", { class: "scroll" }, h("div", { class: "container wide" }, box)));
  usage(box, { mine: true });
  return { destroy() {} };
}

/** `mine`: only the signed-in person's own figures (their endpoints, no list of people). */
export function usage(box, { mine = false } = {}) {
  const base = mine ? "/v1/me/usage" : "/v1/admin/usage";
  let days = 30;
  let users = [];
  const filter = { person: "", group: "", kind: "" };
  const summary = h("div", { class: "usage-split" });
  const keys = mine ? h("div", {}) : null;
  const table = h("div", { class: "panel" });
  const history = h("div", { class: "panel" });
  const rows = h("tbody", {});
  const more = h("button", { onclick: () => loadHistory(false) }, "Load more");
  let next = null;
  const calls = h("span", { class: "count" });

  const period = h("div", { class: "segmented", role: "tablist", "aria-label": "Period" });
  const drawPeriod = () => clear(period).append(...PERIODS.map(([id, label]) =>
    h("button", { role: "tab", "aria-selected": String(id === days), onclick: () => { days = id; drawPeriod(); loadAll(); } }, label)));
  drawPeriod();

  box.append(
    h("div", { class: "toolbar" }, period, h("button", { class: "ghost", onclick: loadAll }, icon("refresh", { size: 18 }), "Refresh")),
    summary, table, ...(keys ? [keys] : []), history);

  const NONE = { answers: 0, prompt_tokens: 0, completion_tokens: 0 };

  /** Your own models and surfaces, from the one entry the server gives you (null: you have not used Clara yet). */
  function mineTable(entry, quota) {
    const surfaces = Object.entries(entry?.surfaces || {}).sort((a, b) => total(b[1]) - total(a[1]));
    const models = entry?.models || [];
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "You"),
        h("p", { class: "muted small" }, "Tokens as the model reported them (estimated when it reported nothing). Compactions and titles count in the tokens, not in the answers."))),
      h("div", { class: "panel-body usage-figures" },
        h("div", {}, h("strong", {}, tokenCount(entry?.prompt_tokens || 0)), h("span", { class: "muted small" }, "tokens in")),
        h("div", {}, h("strong", {}, tokenCount(entry?.completion_tokens || 0)), h("span", { class: "muted small" }, "tokens out")),
        h("div", {}, h("strong", {}, tokenCount(entry?.answers || 0)), h("span", { class: "muted small" }, entry?.answers === 1 ? "answer" : "answers")),
        h("div", {}, usageBar(quota), h("span", { class: "muted small" }, quota.limit ? "credits today" : "credits today, no limit"))),
      entry ? h("table", { class: "grid cards" },
        h("thead", {}, h("tr", {}, ["Where", "Answers", "Tokens in", "Tokens out"].map((t) => h("th", {}, t)))),
        h("tbody", {}, surfaces.map(([name, part]) => h("tr", {},
          h("td", {}, h("span", { class: "badge" + (name === "discord" ? " admin" : "") }, name)),
          h("td", { class: "num", "data-label": "Answers" }, tokenCount(part.answers)),
          h("td", { class: "num", "data-label": "Tokens in" }, tokenCount(part.prompt_tokens)),
          h("td", { class: "num", "data-label": "Tokens out" }, tokenCount(part.completion_tokens)))))) : null,
      models.length ? h("div", { class: "panel-body" }, h("strong", {}, "Your preferred models"),
        h("ul", { class: "plain" }, models.map((m) => h("li", { title: pair(m) }, modelName(m), h("span", { class: "muted small" }, ` ×${tokenCount(m.answers)}`))))) : null,
      !entry && h("div", { class: "empty-state" }, mark(36), h("strong", {}, "Nothing yet"), "Your calls appear here as soon as you talk to Clara."));
  }

  function card(title, part, note) {
    return h("section", { class: "panel usage-card" },
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, title), h("p", { class: "muted small" }, note))),
      h("div", { class: "panel-body usage-figures" },
        h("div", {}, h("strong", {}, tokenCount(part.prompt_tokens)), h("span", { class: "muted small" }, "tokens in")),
        h("div", {}, h("strong", {}, tokenCount(part.completion_tokens)), h("span", { class: "muted small" }, "tokens out")),
        h("div", {}, h("strong", {}, tokenCount(part.answers)), h("span", { class: "muted small" }, part.answers === 1 ? "answer" : "answers"))));
  }

  function userRow(entry) {
    const open = () => { filter.person = String(entry.person.id); drawHistory(); loadHistory(true); history.scrollIntoView({ behavior: "smooth", block: "start" }); };
    const models = entry.models.map((m) => h("div", { title: `${tokenCount(m.answers)} answers, ${pair(m)}` }, modelName(m), h("span", { class: "muted small" }, ` ×${tokenCount(m.answers)}`)));
    return h("tr", {},
      h("td", {}, h("div", { class: "user-cell" }, avatar(entry.person.name), h("div", {},
        h("strong", {}, entry.user || entry.person.name), entry.is_admin && h("span", { class: "badge admin" }, "Admin"),
        !entry.user && h("div", { class: "muted small" }, "no sign-in")))),
      h("td", { class: "num", "data-label": "Answers" }, tokenCount(entry.answers)),
      h("td", { class: "num", "data-label": "Tokens in" }, tokenCount(entry.prompt_tokens)),
      h("td", { class: "num", "data-label": "Tokens out" }, tokenCount(entry.completion_tokens)),
      h("td", { class: "num", "data-label": "Discord", title: `${tokenCount(entry.discord.answers)} answers` }, entry.discord.answers || total(entry.discord) ? pair(entry.discord) : h("span", { class: "muted" }, "—")),
      h("td", { class: "num", "data-label": "Other surfaces", title: Object.entries(entry.surfaces).filter(([name]) => name !== "discord").map(([name, part]) => `${name}: ${pair(part)}`).join("\n") },
        total(entry.other) ? pair(entry.other) : h("span", { class: "muted" }, "—")),
      h("td", { "data-label": "Preferred models" }, models.length ? models : h("span", { class: "muted" }, "—")),
      h("td", { "data-label": "Last used", title: dateTime(entry.last_at) }, ago(entry.last_at)),
      h("td", { class: "end" }, h("button", { class: "ghost icon-btn", title: `Calls of ${entry.person.name}`, "aria-label": `Calls of ${entry.person.name}`, onclick: open }, icon("clock", { size: 18 }))));
  }

  // ---- the person's own API keys (mine only) ---------------------------------------------------------------
  const keyRows = h("tbody", {});
  const keyCalls = h("span", { class: "count" });
  const keyMore = h("button", { onclick: () => loadKeyHistory(false) }, "Load more");
  let keyNext = null;

  async function saveKey(provider, input, button) {
    const value = input.value.trim();
    if (!value) return;
    button.disabled = true;
    try {
      await api.put(`/v1/me/api-keys/${provider.id}`, { api_key: value });
      toast(`${provider.label}: your key is saved. Its models now run on it, with no credits.`);
    } catch (error) { fail(error); button.disabled = false; return; }
    drawKeys(await api.get("/v1/me/api-keys", { days }));
  }

  async function forgetKey(provider) {
    if (!(await confirmDialog(`Remove your ${provider.label} key`, `Clara will answer with ${provider.label} on the server's key again, in credits.`, "Remove", true))) return;
    try { await api.delete(`/v1/me/api-keys/${provider.id}`); } catch (error) { return fail(error); }
    toast(`${provider.label}: your key is removed.`);
    drawKeys(await api.get("/v1/me/api-keys", { days }));
  }

  function keyRow(provider) {
    const input = h("input", { type: "password", autocomplete: "off", spellcheck: false, placeholder: provider.saved ? "Paste a new key to replace it" : "Paste your API key", "aria-label": `${provider.label} API key` });
    const save = h("button", { class: "primary", type: "submit" }, provider.saved ? "Replace" : "Save");
    const used = provider.usage;
    return h("tr", {},
      h("td", { "data-label": "Provider" }, h("strong", {}, provider.label)),
      h("td", { "data-label": "Status" }, provider.saved
        ? h("span", {}, h("span", { class: "badge" }, "Saved"), h("span", { class: "muted small" }, ` ${provider.hint ? "…" + provider.hint : "key hidden"} · ${ago(provider.created_at)}`))
        : h("span", { class: "muted" }, "No key: the server's key and your credits")),
      h("td", { class: "num", "data-label": "Used" }, used && (used.prompt_tokens || used.completion_tokens) ? pair(used) : h("span", { class: "muted" }, "—")),
      h("td", { "data-label": "Key" }, h("form", { class: "toolbar", onsubmit: (event) => { event.preventDefault(); saveKey(provider, input, save); } }, input, save,
        provider.saved && h("button", { type: "button", class: "ghost", onclick: () => forgetKey(provider) }, "Remove"))));
  }

  function drawKeys(found) {
    const usageOnKeys = found.usage;
    clear(keys).append(h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Your API keys"),
        h("p", { class: "muted small" }, "Bring your own key for a provider Clara already uses. Its models, all of them, then run on your key: they cost no credits and your daily limit does not apply. The key is stored encrypted and never shown again. It is not used on Discord."))),
      h("table", { class: "grid cards" },
        h("thead", {}, h("tr", {}, ["Provider", "Status", "Used on your key", "Key"].map((t) => h("th", {}, t)))),
        h("tbody", {}, found.providers.map(keyRow))),
      h("div", { class: "panel-body usage-figures" },
        h("div", {}, h("strong", {}, tokenCount(usageOnKeys?.prompt_tokens || 0)), h("span", { class: "muted small" }, "tokens in on your keys")),
        h("div", {}, h("strong", {}, tokenCount(usageOnKeys?.completion_tokens || 0)), h("span", { class: "muted small" }, "tokens out on your keys")),
        h("div", {}, h("strong", {}, tokenCount(usageOnKeys?.answers || 0)), h("span", { class: "muted small" }, usageOnKeys?.answers === 1 ? "answer" : "answers"))),
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Calls on your keys"),
        h("p", { class: "muted small" }, "Counted apart from the credits and from the figures above. No message text is kept here.")), keyCalls),
      h("table", { class: "grid cards" },
        h("thead", {}, h("tr", {}, ["When", "Surface", "Kind", "Model", "In", "Out"].map((t) => h("th", {}, t)))),
        keyRows),
      h("div", { class: "panel-body" }, keyMore)));
    loadKeyHistory(true);
  }

  async function loadKeyHistory(fresh) {
    if (fresh) { keyNext = null; clear(keyRows); }
    let found;
    try { found = await api.get("/v1/me/api-keys/history", { days, limit: 25, ...(!fresh && keyNext && { before: keyNext }) }); } catch (error) { return fail(error); }
    keyRows.append(...found.calls.map((call) => h("tr", {},
      h("td", { "data-label": "When", title: dateTime(call.at) }, ago(call.at)),
      h("td", { "data-label": "Surface" }, h("span", { class: "badge" }, call.surface)),
      h("td", { "data-label": "Kind" }, KIND_LABEL[call.kind] || call.kind),
      h("td", { "data-label": "Model" }, call.model_ref || call.model || h("span", { class: "muted" }, "—")),
      h("td", { class: "num", "data-label": "In" }, (call.estimated ? "~" : "") + tokenCount(call.prompt_tokens)),
      h("td", { class: "num", "data-label": "Out" }, (call.estimated ? "~" : "") + tokenCount(call.completion_tokens)))));
    keyNext = found.next;
    keyMore.hidden = !keyNext;
    keyCalls.textContent = `${tokenCount(found.totals.calls)} ${found.totals.calls === 1 ? "call" : "calls"} · ${pair(found.totals)}`;
    if (fresh && !found.calls.length) keyRows.append(h("tr", {}, h("td", { colSpan: 6, class: "muted" }, "Nothing yet: save a key, pick one of its models, and talk to Clara.")));
  }

  async function loadKeys() {
    try { drawKeys(await api.get("/v1/me/api-keys", { days })); } catch (error) { fail(error); }
  }

  async function loadAll() {
    let found;
    try { found = await api.get(base, { days }); } catch (error) { return fail(error); }
    if (mine) {
      loadKeys();
      clear(summary).append(card("Discord", found.usage?.discord || NONE, "What the Discord bot answered for you"),
        card("Every other surface", found.usage?.other || NONE, "Web site, app, terminal and the rest"));
      table.className = ""; // the panel is drawn inside
      clear(table).append(mineTable(found.usage, found.quota));
      drawHistory();
      loadHistory(true);
      return;
    }
    users = found.users;
    clear(summary).append(
      card("Discord", found.totals.discord, "Everything the Discord bot answered"),
      card("Every other surface", found.totals.other, "Web site, app, terminal and the rest"));
    clear(table).append(
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Per person"),
        h("p", { class: "muted small" }, "Tokens as the model reported them (estimated when it reported nothing). Compactions and titles count in the tokens, not in the answers.")),
      h("span", { class: "count" }, `${users.length} ${users.length === 1 ? "person" : "people"}`)),
      users.length
        ? h("table", { class: "grid cards" },
          h("thead", {}, h("tr", {}, ["Person", "Answers", "Tokens in", "Tokens out", "Discord", "Other surfaces", "Preferred models", "Last used", ""].map((t) => h("th", {}, t || h("span", { class: "sr-only" }, "History"))))),
          h("tbody", {}, users.map(userRow)))
        : h("div", { class: "empty-state" }, mark(36), h("strong", {}, "Nothing yet"), "Calls appear here as soon as somebody talks to Clara."));
    drawHistory();
    loadHistory(true);
  }

  function drawHistory() {
    const people = [["", "Everybody"], ...users.map((u) => [String(u.person.id), u.user || u.person.name])];
    clear(history).append(
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "History"), h("p", { class: "muted small" }, mine ? "Every call you made, newest first. No message text is kept here." : "Every call, newest first. No message text is kept here.")), calls),
      h("div", { class: "panel-body toolbar" },
        !mine && select("Person", people, filter.person, (v) => { filter.person = v; loadHistory(true); }),
        select("Surface", GROUPS, filter.group, (v) => { filter.group = v; loadHistory(true); }),
        select("Kind", KINDS, filter.kind, (v) => { filter.kind = v; loadHistory(true); })),
      h("table", { class: "grid cards" },
        h("thead", {}, h("tr", {}, ["When", !mine && "Person", "Surface", "Kind", "Model", "In", "Out", "Credits"].filter(Boolean).map((t) => h("th", {}, t)))),
        rows),
      h("div", { class: "panel-body" }, more));
  }

  function callRow(call) {
    const model = call.model_ref || call.model;
    return h("tr", {},
      h("td", { "data-label": "When", title: dateTime(call.at) }, ago(call.at)),
      !mine && h("td", { "data-label": "Person" }, call.person ? (call.user || call.person.name) : h("span", { class: "muted" }, "several people")),
      h("td", { "data-label": "Surface" }, h("span", { class: "badge" + (call.group === "discord" ? " admin" : "") }, call.surface)),
      h("td", { "data-label": "Kind", title: call.conversation }, KIND_LABEL[call.kind] || call.kind, call.rounds > 1 && h("span", { class: "muted small" }, ` · ${call.rounds} rounds`)),
      h("td", { "data-label": "Model" }, model || h("span", { class: "muted" }, "—")),
      h("td", { class: "num", "data-label": "In", title: call.estimated ? "Estimated: the model reported nothing" : "" }, (call.estimated ? "~" : "") + tokenCount(call.prompt_tokens)),
      h("td", { class: "num", "data-label": "Out", title: call.estimated ? "Estimated: the model reported nothing" : "" }, (call.estimated ? "~" : "") + tokenCount(call.completion_tokens)),
      h("td", { class: "num", "data-label": "Credits" }, call.credits ? tokenCount(call.credits) : h("span", { class: "muted" }, "—")));
  }

  async function loadHistory(fresh) {
    if (fresh) { next = null; clear(rows); }
    const query = { days, limit: 50, ...(filter.person && { person: filter.person }), ...(filter.group && { group: filter.group }), ...(filter.kind && { kind: filter.kind }), ...(!fresh && next && { before: next }) };
    let found;
    try { found = await api.get(`${base}/history`, query); } catch (error) { return fail(error); }
    rows.append(...found.calls.map(callRow));
    next = found.next;
    more.hidden = !next;
    calls.textContent = `${tokenCount(found.totals.calls)} ${found.totals.calls === 1 ? "call" : "calls"} · ${pair(found.totals)}`;
    if (fresh && !found.calls.length) rows.append(h("tr", {}, h("td", { colSpan: 8, class: "muted" }, "No call matches.")));
  }

  loadAll();
}
