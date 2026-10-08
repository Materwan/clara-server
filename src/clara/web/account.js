// What Clara remembers about you, and your account: password, devices, linking accounts.

import { api } from "./api.js";
import { mark } from "./icons.js";
import { SURFACE_NAMES, anyModel, chooseModel, costText, findModel, loadModels, modelSelect } from "./models.js";

/** The surfaces in the order people know them: the known ones first, the others by name. */
const surfaceOrder = (surfaces) => [...surfaces].sort((a, b) => {
  const known = Object.keys(SURFACE_NAMES);
  const rank = (name) => (known.includes(name) ? known.indexOf(name) : known.length);
  return rank(a) - rank(b) || a.localeCompare(b);
});
import { ago, avatar, clear, confirmDialog, dateTime, h, icon, pageHead, promptDialog, secretDialog, themeSwitch, toast, tokenCount, usageBar } from "./ui.js";

const who = (user) => ({ surface: "web", user_id: user.name });

// ---- memory ------------------------------------------------------------------------------------------------

export function mountMemory(container, user) {
  const list = h("ul", { class: "list", "aria-label": "What Clara remembers" });
  const count = h("span", { class: "count" });
  const filter = h("input", { type: "search", placeholder: "Search what Clara remembers", "aria-label": "Search facts" });
  const text = h("input", { type: "text", placeholder: "Something Clara should remember about you", maxLength: 300, "aria-label": "New fact" });
  let facts = [];
  let loaded = false;

  const draw = () => {
    clear(list);
    const needle = filter.value.trim().toLowerCase();
    const shown = facts.filter((fact) => fact.text.toLowerCase().includes(needle));
    count.textContent = !loaded ? "" : needle ? `${shown.length} of ${facts.length}` : `${facts.length} ${facts.length === 1 ? "thing" : "things"}`;
    if (!loaded) return;
    if (!facts.length) {
      list.append(h("li", { class: "empty-row" }, h("div", { class: "empty-state" }, mark(36),
        h("strong", {}, "Clara doesn't know anything about you yet"),
        "Tell her about yourself in a chat, or add something above.")));
      return;
    }
    if (!shown.length) list.append(h("li", { class: "empty-row" }, "Nothing matches your search."));
    for (const fact of shown) {
      list.append(h("li", {}, h("span", { class: "fact-dot" }), h("span", { class: "text" }, fact.text),
        h("button", { class: "ghost icon-btn danger forget", title: "Forget this", "aria-label": `Forget: ${fact.text}`, onclick: async () => {
          try {
            await api.delete(`/v1/memory/facts/${fact.id}`, who(user));
            facts = facts.filter((f) => f.id !== fact.id);
            draw();
            toast("Forgotten.");
          } catch (error) { toast(error.detail, true); }
        } }, icon("trash", { size: 18 }))));
    }
  };
  filter.addEventListener("input", draw);

  const add = async (event) => {
    event.preventDefault();
    const value = text.value.trim();
    if (!value) return;
    try {
      const body = await api.post("/v1/memory/facts", { ...who(user), text: value });
      text.value = "";
      toast(body.stored === false ? "Clara already knows that." : "Clara will remember that.");
      await load();
    } catch (error) { toast(error.detail, true); }
  };

  async function load() {
    try {
      facts = (await api.get("/v1/memory/facts", who(user))).facts;
    } catch (error) {
      if (error.status !== 404) toast(error.detail, true);
      facts = [];
    }
    loaded = true;
    draw();
  }

  container.append(pageHead("Memory"), h("div", { class: "scroll" }, h("div", { class: "container" },
    h("p", { class: "intro" }, "Clara picks up facts about you as you talk, and uses them in every conversation, on all your devices. Add what she should know, or remove what she shouldn't."),
    h("form", { class: "panel panel-body add-form", onsubmit: add }, text, h("button", { class: "primary", type: "submit" }, icon("plus", { size: 18 }), "Remember")),
    h("div", { class: "toolbar" }, h("div", { class: "search-field" }, icon("search", { size: 17 }), filter), count),
    h("div", { class: "panel" }, list))));
  draw();
  load();
  return { destroy() {} };
}

// ---- account -----------------------------------------------------------------------------------------------

function deviceGlyph(session) {
  const text = `${session.surface} ${session.device || ""}`.toLowerCase();
  if (/cli|terminal|admin/.test(session.surface)) return "terminal";
  if (/iphone|android|mobile|ipad/.test(text)) return "phone";
  if (session.surface === "web") return "globe";
  return "laptop";
}

/** A browser's user agent, in words a person recognises. */
function deviceName(text) {
  if (!text) return "";
  const browser = /Edg\//.test(text) ? "Edge" : /OPR\//.test(text) ? "Opera" : /Firefox\//.test(text) ? "Firefox"
    : /Chrome\//.test(text) ? "Chrome" : /Safari\//.test(text) ? "Safari" : "";
  const system = /iPhone/.test(text) ? "iPhone" : /iPad/.test(text) ? "iPad" : /Android/.test(text) ? "Android"
    : /Windows/.test(text) ? "Windows" : /Mac OS X|Macintosh/.test(text) ? "macOS" : /Linux/.test(text) ? "Linux" : "";
  if (browser && system) return `${browser} on ${system}`;
  return browser || system || text.slice(0, 60);
}

export function mountAccount(container, user, onSignOut) {
  const page = h("div", { class: "container" });
  container.append(pageHead("Account"), h("div", { class: "scroll" }, page));

  async function draw() {
    let me, devices, prefs, models;
    try {
      me = await api.get("/v1/auth/me");
      [devices, prefs, models] = await Promise.all([api.get("/v1/auth/sessions"), api.get("/v1/settings", who(me)), loadModels(who(me))]);
    } catch (error) {
      return void toast(error.detail, true);
    }
    clear(page);
    page.append(
      profileCard(me),
      appearanceCard(),
      usageCard(me),
      modelsCard(me, models),
      notifyCard(me, prefs),
      passwordCard(),
      devicesCard(devices.sessions),
      linkCard(me),
      dataCard(me),
    );
  }

  const profileCard = (me) => h("section", { class: "panel panel-body profile" },
    avatar(me.person?.name || me.name, "lg"),
    h("div", { class: "who" },
      h("h2", {}, me.person?.name || me.name, me.is_admin && h("span", { class: "badge admin" }, "Administrator")),
      h("p", { class: "muted small" }, `Signed in as ${me.name}. Clara knows you on these accounts:`),
      h("div", { class: "accounts" }, me.accounts.length ? me.accounts.map((a) => h("span", { class: "badge" }, a)) : h("span", { class: "muted small" }, "none yet"))),
    h("button", { onclick: onSignOut }, icon("logout", { size: 18 }), "Sign out"));

  /** Credits used today against the daily limit an administrator set (or none). */
  function usageCard(me) {
    const usage = me.usage;
    const note = !usage.limit
      ? (me.is_admin ? "As an administrator you have no limit." : "You have no daily limit.")
      : usage.used >= usage.limit
        ? `You used all of today's credits. You can talk to Clara again at ${dateTime(usage.resets_at)}, or ask an administrator to raise your limit.`
        : `${tokenCount(usage.remaining)} credits left today. The day starts again at ${dateTime(usage.resets_at)}.`;
    return h("section", { class: "panel panel-body usage-card" },
      h("h3", {}, "Usage today"),
      h("div", { class: "usage-big" }, usageBar(usage)),
      h("p", { class: "muted small" }, note));
  }

  /** The model Clara answers you with, on each surface: one of those an administrator offers, each at its own cost. */
  function modelsCard(me, models) {
    const head = h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, "Model"),
      h("p", { class: "muted small" }, "Every token Clara reads or writes costs credits: bigger models cost more per token, so a small one lets you talk longer within your daily limit.")));
    if (!anyModel(models)) {
      return h("section", { class: "panel" }, head, h("div", { class: "panel-body" },
        h("p", { class: "muted small" }, `Clara answers you with ${models.default.name} (${costText(models.default.weight)}). An administrator has not offered other models yet.`)));
    }
    const rows = surfaceOrder(models.surfaces).filter((surface) => surface in SURFACE_NAMES || surface in models.choices).map((surface) => {
      const select = modelSelect(models, models.choices[surface] ?? null, async (ref) => {
        select.disabled = true;
        try {
          await chooseModel(who(me), surface, ref);
          toast(ref ? `${SURFACE_NAMES[surface] || surface}: ${findModel(models, ref).name}.` : `${SURFACE_NAMES[surface] || surface}: the server's model.`);
        } catch (error) { toast(error.detail, true); }
        draw();
      }, `Model on ${SURFACE_NAMES[surface] || surface}`);
      return h("label", { class: "field" }, SURFACE_NAMES[surface] || surface, select);
    });
    return h("section", { class: "panel" }, head, h("div", { class: "panel-body stack" },
      h("div", { class: "model-grid" }, rows),
      h("p", { class: "muted small" }, "Discord has one model for everybody, chosen by an administrator.")));
  }

  /** How long a task takes before you are notified when it is done: the server's delay, never, or your own. */
  function notifyCard(me, prefs) {
    const own = prefs.notify_after;
    const fallback = prefs.notify_after_default;
    const mode = h("select", { "aria-label": "Notify when a task is done" },
      h("option", { value: "default" }, `Like the server (${fallback ? `after ${fallback} s` : "never"})`),
      h("option", { value: "never" }, "Never"),
      h("option", { value: "after" }, "After a delay of my own"));
    const seconds = h("input", { type: "number", min: 1, max: 604800, step: 1, required: true, "aria-label": "Seconds", value: own || 120 });
    const row = h("label", { class: "field" }, "Seconds of work before I am notified", seconds);
    mode.value = own === null ? "default" : own === 0 ? "never" : "after";
    const sync = () => { row.hidden = mode.value !== "after"; };
    mode.addEventListener("change", sync);
    sync();
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, "Notifications"),
        h("p", { class: "muted small" }, "A task that takes at least this long (an answer, a long job in the console or the app) notifies you on your devices when it is done."))),
      h("form", { class: "panel-body stack", onsubmit: async (event) => {
        event.preventDefault();
        const value = mode.value === "default" ? null : mode.value === "never" ? 0 : Number(seconds.value);
        try {
          await api.patch("/v1/settings", { ...who(me), notify_after: value });
          toast("Saved.");
          draw();
        } catch (error) { toast(error.detail, true); }
      } },
      h("div", { class: "form-grid" }, h("label", { class: "field" }, "Notify me when a task is done", mode), row),
      h("div", {}, h("button", { class: "primary", type: "submit" }, "Save"))));
  }

  function appearanceCard() {
    const switcher = themeSwitch();
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, "Appearance"), h("p", { class: "muted small" }, "Light, dark, or the system's choice. Remembered in this browser."))),
      switcher && h("div", { class: "panel-body" }, switcher));
  }

  function passwordCard() {
    const current = h("input", { type: "password", autocomplete: "current-password", required: true });
    const next = h("input", { type: "password", autocomplete: "new-password", required: true, minLength: 10 });
    const again = h("input", { type: "password", autocomplete: "new-password", required: true });
    const note = h("p", { class: "small", hidden: true, role: "alert" });
    const fail = (text) => { note.hidden = false; note.className = "small error"; note.textContent = text; };
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, "Password"), h("p", { class: "muted small" }, "Changing it signs you out on your other devices."))),
      h("form", { class: "panel-body stack", onsubmit: async (event) => {
        event.preventDefault();
        note.hidden = true;
        if (next.value !== again.value) return fail("The two new passwords are different.");
        try {
          const done = await api.post("/v1/auth/password", { current_password: current.value, new_password: next.value });
          current.value = next.value = again.value = "";
          toast(done.signed_out_elsewhere ? `Password changed. ${done.signed_out_elsewhere} other device(s) signed out.` : "Password changed.");
          draw();
        } catch (error) { fail(error.detail); }
      } },
      h("div", { class: "form-grid" },
        h("label", { class: "field" }, "Current password", current),
        h("label", { class: "field" }, "New password", next, h("span", { class: "hint" }, "10 characters or more")),
        h("label", { class: "field" }, "New password again", again)),
      note,
      h("div", {}, h("button", { class: "primary", type: "submit" }, "Change password"))));
  }

  function devicesCard(sessions) {
    const others = sessions.filter((s) => !s.current);
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" },
        h("div", { class: "grow" }, h("h3", {}, "Your devices"), h("p", { class: "muted small" }, "Where you are signed in. A device unused for 90 days is signed out.")),
        others.length > 0 && h("button", { class: "danger sm", onclick: async () => {
          if (await confirmDialog("Sign out other devices", `Sign out ${others.length} other device(s)? This one stays signed in.`, "Sign out", true)) {
            for (const s of others) await signOut(s.id, true);
            toast("Other devices signed out.");
          }
          draw();
        } }, "Sign out the others")),
      h("ul", { class: "list" }, sessions.map((s) => h("li", { class: "device" },
        h("span", { class: "glyph" }, icon(deviceGlyph(s))),
        h("div", { class: "meta" },
          h("div", {}, h("strong", {}, deviceName(s.device) || s.surface), " ", s.current && h("span", { class: "badge ok" }, "This device")),
          h("div", { class: "sub", title: s.device || "" }, `${s.surface}${s.address ? `, from ${s.address}` : ""}. Signed in ${dateTime(s.created_at)}`)),
        h("div", { class: "when", title: `Last used ${dateTime(s.last_used_at)}` }, ago(s.last_used_at)),
        !s.current && h("button", { class: "ghost icon-btn", title: "Sign out this device", "aria-label": "Sign out this device", onclick: () => signOut(s.id) }, icon("logout", { size: 18 }))))));
  }

  async function signOut(id, quiet) {
    try { await api.delete(`/v1/auth/sessions/${id}`); } catch (error) { toast(error.detail, true); }
    if (!quiet) { toast("Device signed out."); draw(); }
  }

  /** What the server keeps about you: take it with you, or have it erased. */
  function dataCard(me) {
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, "Your data"),
        h("p", { class: "muted small" }, "Your conversations, what Clara remembers, your files, tasks and projects are kept on this server. You can download all of it, or erase it."))),
      h("div", { class: "panel-body stack" },
        h("div", {}, h("button", { onclick: async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          try {
            const data = await api.get("/v1/auth/export");
            const link = h("a", { href: URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })), download: `clara-${me.name}.json` });
            document.body.append(link);
            link.click();
            setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
          } catch (error) { toast(error.detail, true); }
          button.disabled = false;
        } }, icon("copy", { size: 18 }), "Download my data")),
        h("div", {},
          h("p", { class: "muted small" }, "Erasing removes your user, your memories, conversations, files, tasks, projects and connected accounts, and the server's log of what you said. There is no undo."),
          h("button", { class: "danger sm", onclick: async () => {
            if (!(await confirmDialog("Erase my account", "Everything Clara keeps about you will be erased for good. Download your data first if you want a copy.", "Continue", true))) return;
            const password = await promptDialog("Erase my account", "Your password", "", "Erase everything", { type: "password" });
            if (!password) return;
            try {
              await api.post("/v1/auth/delete-account", { password });
              toast("Your account was erased.");
              onSignOut();
            } catch (error) { toast(error.detail, true); }
          } }, icon("trash", { size: 18 }), "Erase my account"))));
  }

  function linkCard(me) {
    const code = h("div", {});
    const surface = h("input", { type: "text", placeholder: "Surface, e.g. discord", pattern: "[a-z0-9_-]{1,32}", "aria-label": "Surface", required: true, autocapitalize: "none" });
    const account = h("input", { type: "text", placeholder: "Account id, e.g. 1234", "aria-label": "Account id", required: true, autocapitalize: "none" });
    const secret = h("input", { type: "text", placeholder: "Link code", "aria-label": "Link code", required: true, autocomplete: "off", autocapitalize: "none" });
    return h("section", { class: "panel" },
      h("div", { class: "panel-head" }, h("div", {}, h("h3", {}, "Linked accounts"),
        h("p", { class: "muted small" }, "Signing in already makes the web, the desktop app and the terminal one person. Linking is for accounts without a password, such as Discord."))),
      h("div", { class: "panel-body link-grid" },
        h("div", {},
          h("h4", { class: "section-title" }, "Bring an account to you"),
          h("p", { class: "muted small" }, "In the other account's client, ask for its link code (for example /linkcode), then enter it here."),
          h("form", { class: "link-form", onsubmit: async (event) => {
            event.preventDefault();
            try {
              const done = await api.post("/v1/accounts/link", {
                surface: surface.value.trim().toLowerCase(), user_id: account.value.trim(), code: secret.value.trim(),
                to_surface: "web", to_user_id: me.name,
              });
              toast(`Linked. Your accounts: ${done.accounts.join(", ")}`);
              surface.value = account.value = secret.value = "";
              draw();
            } catch (error) { toast(error.detail, true); }
          } }, surface, account, h("div", { class: "full" }, secret), h("div", { class: "full" }, h("button", { class: "primary", type: "submit" }, icon("link", { size: 18 }), "Link account")))),
        h("div", {},
          h("h4", { class: "section-title" }, "Join from the other account"),
          h("p", { class: "muted small" }, `Get a code for ${me.name}, valid 10 minutes and usable once. Then send /link web ${me.name} <code> from the other client.`),
          h("button", { onclick: async () => {
            try {
              const done = await api.post("/v1/accounts/link-code", who(me));
              clear(code).append(h("div", { class: "secret-box" }, done.code), h("p", { class: "muted small" }, `Valid for ${Math.round(done.expires_in / 60)} minutes.`));
            } catch (error) { toast(error.detail, true); }
          } }, icon("key", { size: 18 }), "Get a link code"), code)));
  }

  draw();
  return { destroy() {} };
}

export { secretDialog };
