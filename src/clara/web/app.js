// Clara's web site: sign in, then a chat, your tasks, projects, what Clara remembers, your account, and (for administrators)
// the administration. Everything talks to the same HTTP API as the other clients, as the surface "web".

import { mountAccount, mountMemory } from "./account.js";
import { mountAdmin } from "./admin.js";
import { ApiError, api, events } from "./api.js";
import { mountChat } from "./chat.js";
import { mountDiscord } from "./discord.js";
import { mountFiles } from "./files.js";
import { icon, mark } from "./icons.js";
import { mountProjects } from "./projects.js";
import { mountTasks } from "./tasks.js";
import { avatar, clear, h, toast, toggleRail } from "./ui.js";

const app = document.getElementById("app");
let user = null; // who is signed in: the answer of /v1/auth/me
let page = null; // what is mounted: {destroy, newChat?}
let pageName = "";
let healthTimer = null;
let wantNewChat = false;

// ---- the theme switch --------------------------------------------------------------------------------------

const THEMES = [["auto", "Auto", "auto"], ["light", "Light", "sun"], ["dark", "Dark", "moon"]];

function themeSwitch() {
  const theme = window.claraTheme;
  if (!theme) return null; // theme.js did not load: the system's colours apply
  const box = h("div", { class: "theme-switch", role: "group", "aria-label": "Colour theme" });
  const draw = () => clear(box).append(...THEMES.map(([id, label, glyph]) =>
    h("button", { type: "button", "aria-pressed": String(theme.get() === id), title: id === "auto" ? "Follow the system" : `${label} theme`,
      onclick: () => { theme.set(id); for (const other of document.querySelectorAll(".theme-switch")) other.redraw?.(); } },
    icon(glyph, { size: 16 }), label)));
  box.redraw = draw;
  draw();
  return box;
}

// ---- signing in ----------------------------------------------------------------------------------------

function authPage(title, lede, form, foot) {
  clear(app).append(h("div", { class: "login-wrap" }, h("main", { class: "login" },
    h("div", { class: "halo" }, mark(60)),
    h("h1", {}, title),
    h("p", { class: "lede" }, lede),
    form,
    foot,
    themeSwitch())));
}

/** Whether this server lets people make their own account; a server that cannot say is taken as closed. */
async function signupOpen() {
  try { return (await api.get("/v1/auth/signup")).open === true; } catch { return false; }
}

function showLogin(message = "") {
  stop();
  user = null;
  document.title = "Sign in – Clara";
  const name = h("input", { type: "text", autocomplete: "username", autocapitalize: "none", spellcheck: false, required: true, autofocus: true });
  const password = h("input", { type: "password", autocomplete: "current-password", required: true });
  const note = h("p", { class: message ? "notice small" : "", hidden: !message, role: "alert" }, message);
  const button = h("button", { class: "primary", type: "submit" }, "Sign in");
  const form = h("form", { class: "stack", onsubmit: async (event) => {
    event.preventDefault();
    button.disabled = true;
    button.textContent = "Signing in…";
    note.hidden = true;
    try {
      await api.post("/v1/auth/login", { username: name.value.trim(), password: password.value, surface: "web" }, { quiet401: true });
      password.value = "";
      await boot();
    } catch (error) {
      note.hidden = false;
      note.className = "notice warn small";
      note.textContent = error.detail || String(error);
      button.disabled = false;
      button.textContent = "Sign in";
      password.select();
    }
  } },
  h("label", { class: "field" }, "User name", name),
  h("label", { class: "field" }, "Password", password),
  note,
  button);
  const foot = h("p", { class: "foot" }, "No account yet? Ask the person who runs this server.");
  authPage("Sign in to Clara", "Your conversations, and what Clara remembers about you.", form, foot);
  name.focus();
  signupOpen().then((open) => {
    if (open && foot.isConnected) clear(foot).append("No account yet? ", h("a", { href: "/", onclick: (event) => { event.preventDefault(); showSignup(); } }, "Create one"), ".");
  });
}

function showSignup() {
  stop();
  user = null;
  document.title = "Create an account – Clara";
  const name = h("input", { type: "text", autocomplete: "username", autocapitalize: "none", spellcheck: false, required: true, autofocus: true, maxlength: 32 });
  const password = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const again = h("input", { type: "password", autocomplete: "new-password", required: true });
  const note = h("p", { hidden: true, role: "alert" });
  const button = h("button", { class: "primary", type: "submit" }, "Create my account");
  const warn = (text) => { note.hidden = false; note.className = "notice warn small"; note.textContent = text; };
  const form = h("form", { class: "stack", onsubmit: async (event) => {
    event.preventDefault();
    note.hidden = true;
    if (password.value !== again.value) {
      warn("The two passwords are not the same.");
      again.select();
      return;
    }
    button.disabled = true;
    button.textContent = "Creating…";
    try {
      await api.post("/v1/auth/register", { username: name.value.trim(), password: password.value }, { quiet401: true });
      password.value = again.value = "";
      await boot(); // the server signed this account in
    } catch (error) {
      warn(error.detail || String(error));
      button.disabled = false;
      button.textContent = "Create my account";
    }
  } },
  h("label", { class: "field" }, "User name", name,
    h("span", { class: "hint" }, "Letters a–z, digits, '.', '_' or '-' (up to 32). It cannot be changed later.")),
  h("label", { class: "field" }, "Password", password, h("span", { class: "hint" }, "At least 10 characters.")),
  h("label", { class: "field" }, "Repeat the password", again),
  note,
  button);
  const foot = h("p", { class: "foot" }, "Already have an account? ",
    h("a", { href: "/", onclick: (event) => { event.preventDefault(); showLogin(); } }, "Sign in"), ".");
  authPage("Create your account", "Then Clara can know you, and keep your conversations.", form, foot);
  name.focus();
}

async function signOut() {
  try { await api.post("/v1/auth/logout", {}); } catch { /* the cookie is dropped anyway */ }
  showLogin();
}

events.addEventListener("signed-out", () => { if (user) showLogin("Your session ended. Sign in again to go on."); });

// ---- the shell and the pages -----------------------------------------------------------------------------

const PAGES = { chat: ["Chat", "chat"], tasks: ["Tasks", "tasks"], projects: ["Projects", "folder"], files: ["Files", "file"], memory: ["Memory", "memory"], account: ["Account", "user"], discord: ["Discord", "bot"], admin: ["Admin", "admin"] };
const ADMIN_PAGES = new Set(["discord", "admin"]);

function shell() {
  const status = h("div", { class: "status", title: "Server status" }, h("span", { class: "dot" }), h("span", { class: "text" }, "Checking…"));
  const pages = h("nav", { class: "pages", "aria-label": "Pages" },
    Object.entries(PAGES).filter(([id]) => !ADMIN_PAGES.has(id) || user.is_admin).map(([id, [label, glyph]]) =>
      h("a", { href: `#/${id}`, "data-page": id }, icon(glyph, { size: 19 }), label)));
  const displayName = user.person?.name || user.name;
  const rail = h("aside", { class: "rail", id: "rail", "aria-label": "Navigation" },
    h("div", { class: "rail-head" },
      h("a", { class: "brand", href: "#/chat" }, mark(28), "Clara"),
      h("button", { class: "ghost icon-btn close-rail", "aria-label": "Close navigation", onclick: () => toggleRail(false) }, icon("close"))),
    h("button", { class: "primary new-chat", onclick: newChat }, icon("plus", { size: 18 }), "New chat"),
    pages,
    h("div", { class: "rail-slot", id: "rail-slot" }),
    h("div", { class: "rail-foot" },
      themeSwitch(),
      h("div", { class: "me" },
        avatar(displayName),
        h("div", { class: "who" }, h("strong", { title: user.is_admin ? `${user.name}, administrator` : user.name }, displayName), status),
        h("button", { class: "ghost icon-btn", title: "Sign out", "aria-label": "Sign out", onclick: signOut }, icon("logout", { size: 19 })))));
  const body = h("main", { class: "page", id: "page" });
  clear(app).append(h("div", { class: "shell" },
    rail,
    h("div", { class: "scrim", onclick: () => toggleRail(false) }),
    h("div", { class: "content" }, body)));
  healthTimer = setInterval(() => health(status), 20000);
  health(status);
  return body;
}

function newChat() {
  toggleRail(false);
  if (pageName === "chat" && page?.newChat) return page.newChat();
  wantNewChat = true;
  location.hash = "#/chat";
}

async function health(status) {
  const dot = status.querySelector(".dot");
  const text = status.querySelector(".text");
  try {
    const info = await (await fetch("/health", { cache: "no-store" })).json();
    dot.className = "dot up";
    text.textContent = `${info.model} (${info.provider})`;
    status.title = `Clara is running: ${info.model} on the ${info.provider} provider`;
  } catch {
    dot.className = "dot down";
    text.textContent = "Server unreachable";
    status.title = "The Clara server does not answer";
  }
}

function stop() {
  clearInterval(healthTimer);
  healthTimer = null;
  page?.destroy();
  page = null;
  pageName = "";
  toggleRail(false);
}

function route() {
  if (!user) return;
  const [, id = "chat", sub, arg] = location.hash.split("/");
  const name = Object.hasOwn(PAGES, id) && (!ADMIN_PAGES.has(id) || user.is_admin) ? id : "chat";
  for (const link of document.querySelectorAll(".pages a")) {
    const active = link.dataset.page === name;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const body = document.getElementById("page");
  const slot = document.getElementById("rail-slot");
  if (!body) return;
  toggleRail(false);
  page?.destroy();
  clear(body);
  clear(slot);
  pageName = name;
  document.title = name === "chat" ? "Clara" : `${PAGES[name][0]} – Clara`;
  const fresh = wantNewChat || (name === "chat" && sub === "new");
  wantNewChat = false;
  // #/chat/new/<project>: a new chat in a project; #/chat/open/<conversation>: that conversation
  const project = name === "chat" && sub === "new" && Number(arg) > 0 ? Number(arg) : null;
  const openId = name === "chat" && sub === "open" && arg ? decodeURIComponent(arg) : null;
  if (name === "chat" && sub) history.replaceState(null, "", "#/chat"); // no hashchange: nothing is drawn twice
  page = name === "chat" ? mountChat(body, user, { slot, fresh, project, open: openId })
    : name === "tasks" ? mountTasks(body, user)
    : name === "projects" ? mountProjects(body, user, sub)
    : name === "files" ? mountFiles(body, user)
    : name === "memory" ? mountMemory(body, user)
    : name === "account" ? mountAccount(body, user, signOut)
    : name === "discord" ? mountDiscord(body)
    : mountAdmin(body, user, sub);
}

window.addEventListener("hashchange", route); // the admin page's own tabs change the address without this event

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("rail-open")) toggleRail(false);
});

// ---- start ------------------------------------------------------------------------------------------------------

async function boot() {
  try {
    user = await api.get("/v1/auth/me", undefined, { quiet401: true });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return showLogin();
    clear(app).append(h("div", { class: "login-wrap" }, h("main", { class: "login" },
      h("div", { class: "halo" }, mark(60)),
      h("h1", {}, "Clara can't be reached"),
      h("p", { class: "lede" }, error.detail || String(error)),
      h("div", { class: "row", }, h("button", { class: "primary grow", onclick: boot }, "Try again")))));
    return;
  }
  stop();
  shell();
  if (!location.hash) history.replaceState(null, "", "#/chat"); // no hashchange: the page is drawn once
  route();
}

boot().catch((error) => toast(String(error), true));
