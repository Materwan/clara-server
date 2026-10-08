// Clara's web site: sign in, then a chat, your tasks, projects, what Clara remembers, your account, and (for administrators)
// the administration. Everything talks to the same HTTP API as the other clients, as the surface "web".

import { mountAccount, mountMemory } from "./account.js";
import { mountAdmin } from "./admin.js";
import { ApiError, api, events } from "./api.js";
import { mountApprovalBadge } from "./approvals.js";
import { mountChat } from "./chat.js";
import { mountDiscord } from "./discord.js";
import { mountFiles } from "./files.js";
import { mountHistory } from "./history.js";
import { icon, mark } from "./icons.js";
import { mountIntegrations } from "./integrations.js";
import { mountProjects } from "./projects.js";
import { mountRestartBadge, resumeRestart } from "./restart.js";
import { mountSchedule } from "./schedule.js";
import { mountTasks } from "./tasks.js";
import { mountUsage } from "./usage.js";
import { avatar, clear, h, themeSwitch, toast, toggleRail } from "./ui.js";

const app = document.getElementById("app");
let user = null; // who is signed in: the answer of /v1/auth/me
let page = null; // what is mounted: {destroy, newChat?}
let pageName = "";
let healthTimer = null;
let railHistory = null; // the list of conversations on the work pages other than the chat
let approvalBadge = null; // "waiting for you": requests for permission, in the rail
let restartBadge = null; // "restart needed", for administrators
let wantNewChat = false;

// ---- signing in ----------------------------------------------------------------------------------------

/** The sign-in screens: the mark and name on the left (above the form on a phone), the form on the right. */
function authFrame(...main) {
  clear(app).append(h("div", { class: "login-wrap" },
    h("aside", { class: "login-art" },
      mark(36),
      h("span", { class: "wordmark" }, "Clara"),
      h("p", { class: "tagline" }, "Your conversations, and what she remembers about you.")),
    h("main", { class: "login" }, ...main)));
}

function authPage(title, lede, form, foot) {
  authFrame(h("h1", {}, title), h("p", { class: "lede" }, lede), form, foot, themeSwitch());
}

/** Whether this server lets people make their own account (and what it does with their words); a server that cannot say is taken as closed. */
async function signupInfo() {
  try {
    const info = await api.get("/v1/auth/signup");
    return { open: info.open === true, privacy: info.privacy || null };
  } catch { return { open: false, privacy: null }; }
}

/** What a person agrees to by writing to this server, said before they make an account. */
function privacyNotice(privacy) {
  if (!privacy) return null;
  const hosts = privacy.model_hosts.length ? privacy.model_hosts.join(", ") : "the language model the server uses";
  const items = [
    `What you write is stored on this server${privacy.kept_until_deleted ? " until you delete it" : ""}, and sent to the language model: ${hosts}. Those services have their own policies.`,
    privacy.web ? "When Clara searches the web or reads a page, the search or the address goes to ollama.com." : null,
    privacy.log_days ? `The server also logs requests and prompts, in clear, for ${privacy.log_days} days.` : null,
    "The person who runs the server can read all of this. Do not write passwords or anything you would not tell them.",
    "Under Account, Your data, you can download everything or erase it.",
  ].filter(Boolean);
  return h("div", { class: "notice small", role: "note" }, h("strong", {}, "Before you start"), h("ul", {}, items.map((item) => h("li", {}, item))));
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
  signupInfo().then(({ open }) => {
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
  const privacy = h("div", {});
  signupInfo().then((info) => { const notice = privacyNotice(info.privacy); if (notice && privacy.isConnected) privacy.append(notice); });
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
  privacy,
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

const PAGES = { chat: ["Chat", "chat"], projects: ["Projects", "folder"], tasks: ["Tasks", "tasks"], schedule: ["Schedule", "clock"], files: ["Files", "file"], memory: ["Memory", "memory"], account: ["Account", "user"], usage: ["Usage", "bolt"], integrations: ["Integrations", "plug"], discord: ["Discord", "bot"], admin: ["Admin", "admin"] };
const WORK_PAGES = ["projects", "tasks", "schedule", "files"]; // what you do with Clara: the links of the rail (the chat is the conversations listed below them)
const SETTINGS_PAGES = ["memory", "account", "usage", "integrations", "discord", "admin"]; // reached by your avatar, they share a bar at the top
const ADMIN_PAGES = new Set(["discord", "admin"]);

/** The bar at the top of the settings pages. */
function settingsBar(current) {
  return h("nav", { class: "settings-bar", "aria-label": "Settings" },
    SETTINGS_PAGES.filter((id) => !ADMIN_PAGES.has(id) || user.is_admin).map((id) =>
      h("a", { href: `#/${id}`, "aria-current": id === current ? "page" : null }, icon(PAGES[id][1], { size: 17 }), PAGES[id][0])));
}

function shell() {
  const status = h("div", { class: "status", title: "Server status" }, h("span", { class: "dot" }), h("span", { class: "text" }, "Checking…"));
  const pages = h("nav", { class: "pages", "aria-label": "Pages" },
    WORK_PAGES.map((id) => h("a", { href: `#/${id}`, "data-page": id }, icon(PAGES[id][1], { size: 19 }), PAGES[id][0])));
  const displayName = user.person?.name || user.name;
  const rail = h("aside", { class: "rail", id: "rail", "aria-label": "Navigation" },
    h("div", { class: "rail-head" },
      h("a", { class: "brand", href: "#/chat" }, mark(28), "Clara"),
      h("button", { class: "ghost icon-btn close-rail", "aria-label": "Close navigation", onclick: () => toggleRail(false) }, icon("close"))),
    h("button", { class: "primary new-chat", onclick: newChat }, icon("plus", { size: 18 }), "New chat"),
    pages,
    h("div", { class: "rail-slot", id: "rail-slot" }),
    h("div", { class: "rail-foot" },
      h("div", { class: "approvals-slot", id: "approvals-slot" }),
      h("a", { class: "me", href: "#/account", title: "Your settings: memory, account, integrations" + (user.is_admin ? ", Discord, administration" : "") },
        avatar(displayName),
        h("div", { class: "who" }, h("strong", {}, displayName), status),
        icon("chevron", { size: 17 }))));
  const body = h("main", { class: "page", id: "page" });
  clear(app).append(h("div", { class: "shell" },
    rail,
    h("div", { class: "scrim", onclick: () => toggleRail(false) }),
    h("div", { class: "content" }, body)));
  healthTimer = setInterval(() => health(status), 20000);
  health(status);
  approvalBadge = mountApprovalBadge(user, rail.querySelector("#approvals-slot"));
  restartBadge = mountRestartBadge(user, rail.querySelector("#approvals-slot"));
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
  railHistory?.destroy();
  railHistory = null;
  approvalBadge?.destroy();
  approvalBadge = null;
  restartBadge?.destroy();
  restartBadge = null;
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
  const me = document.querySelector(".me");
  me?.classList.toggle("active", SETTINGS_PAGES.includes(name));
  if (SETTINGS_PAGES.includes(name)) me?.setAttribute("aria-current", "page");
  else me?.removeAttribute("aria-current");
  const body = document.getElementById("page");
  const slot = document.getElementById("rail-slot");
  if (!body) return;
  toggleRail(false);
  page?.destroy();
  railHistory?.destroy();
  railHistory = null;
  clear(body);
  clear(slot);
  if (name !== "chat") railHistory = mountHistory(slot, user); // the chat page draws its own
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
    : name === "schedule" ? mountSchedule(body, user)
    : name === "projects" ? mountProjects(body, user, sub)
    : name === "files" ? mountFiles(body, user)
    : name === "memory" ? mountMemory(body, user)
    : name === "account" ? mountAccount(body, user, signOut)
    : name === "usage" ? mountUsage(body)
    : name === "integrations" ? mountIntegrations(body, user)
    : name === "discord" ? mountDiscord(body)
    : mountAdmin(body, user, sub);
  if (SETTINGS_PAGES.includes(name)) body.querySelector(".page-head")?.after(settingsBar(name));
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
    authFrame(
      h("h1", {}, "Clara can't be reached"),
      h("p", { class: "lede" }, error.detail || String(error)),
      h("div", { class: "row" }, h("button", { class: "primary grow", onclick: boot }, "Try again")));
    return;
  }
  stop();
  shell();
  if (!location.hash) history.replaceState(null, "", "#/chat"); // no hashchange: the page is drawn once
  route();
  if (user.is_admin) resumeRestart();
}

boot().catch((error) => toast(String(error), true));
