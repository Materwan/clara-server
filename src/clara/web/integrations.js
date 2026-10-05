// Integrations: the accounts you connect (GitHub, Google Drive), the repositories, Drive folders and folders you add,
// and what Clara may do with each (look, add or change, replace or delete: allow, ask you, or never). The same
// "Connections" panel attaches them to a project or to one conversation. The administrator's switches, the folders of
// the server and the log are at the bottom of the page.

import { api } from "./api.js";
import { approvalCard, levelWord, pendingApprovals } from "./approvals.js";
import { mark } from "./icons.js";
import { add, ago, clear, confirmDialog, h, icon, openDialog, pageHead, toast } from "./ui.js";

const SURFACE = "web";
const who = (user) => ({ surface: SURFACE, user_id: user.name });
const fail = (error) => { if (error.status !== 401) toast(error.detail || String(error), true); };
/** Empty a node and fill it; what is false or missing is left out (a bare `append` would write the word "false"). */
const fill = (node, ...children) => add(clear(node), children);

const LEVELS = [
  ["read", "Look", "List, read and search."],
  ["write", "Add or change", "New files, a commit on a branch, a pull request, an issue."],
  ["destructive", "Replace or delete", "Overwrite, delete, move, commit to the main branch, merge, close."],
];
const DECISIONS = [["allow", "Allow"], ["ask", "Ask me"], ["deny", "Never"]];
const OPENNESS = { deny: 0, ask: 1, allow: 2 };
const DEFAULT_LEVELS = { read: "allow", write: "ask", destructive: "ask" };
const SHORT = { read: "Look", write: "Change", destructive: "Replace" };
const KINDS = {
  github_repo: ["branch", "GitHub repository"],
  drive_folder: ["cloud", "Drive folder"],
  drive_file: ["cloud", "Drive file"],
  server_path: ["server", "Folder on the server"],
  computer_path: ["laptop", "Folder on a computer"],
};
const ACCOUNTS = { github: ["branch", "GitHub"], gdrive: ["cloud", "Google Drive"] };
const NOTIFY_CHOICES = [["", "the server's default"], ["0", "never"], ["30", "30 seconds"], ["60", "1 minute"], ["300", "5 minutes"], ["900", "15 minutes"], ["3600", "1 hour"]];

// ---- permissions ----------------------------------------------------------------------------------------

/** A line of badges: what Clara may do, level by level. */
export function levelBadges(effective) {
  return h("span", { class: "perm-badges" }, LEVELS.map(([level]) =>
    h("span", { class: `badge perm-${effective[level]}`, title: `${levelWord(level)}: ${DECISIONS.find(([v]) => v === effective[level])?.[1] || effective[level]}` },
      `${SHORT[level]} · ${(DECISIONS.find(([v]) => v === effective[level])?.[1] || effective[level]).toLowerCase()}`)));
}

/** Three selects, one per level. `inherit`: the text of the choice that leaves a level to the layer above (none: every
 *  level has a value, `defaults` when it has not). `ceiling`: the most the administrator lets through. */
export function levelEditor(levels, { inherit = "", ceiling = {}, defaults = DEFAULT_LEVELS } = {}) {
  const selects = {};
  const rows = LEVELS.map(([level, name, help]) => {
    const limit = ceiling[level];
    const select = h("select", { "aria-label": name }, [
      inherit && h("option", { value: "" }, inherit),
      ...DECISIONS.map(([value, text]) => {
        const blocked = limit !== undefined && OPENNESS[value] > OPENNESS[limit];
        return h("option", { value, disabled: blocked }, blocked ? `${text} (the administrator does not allow it)` : text);
      }),
    ]);
    select.value = levels[level] || (inherit ? "" : defaults[level]);
    selects[level] = select;
    return h("div", { class: "perm-row" }, h("div", { class: "perm-name" }, h("strong", {}, name), h("div", { class: "muted small" }, help)), select);
  });
  const node = h("div", { class: "perm-editor" }, rows);
  node.value = () => Object.fromEntries(Object.entries(selects).filter(([, s]) => s.value).map(([level, s]) => [level, s.value]));
  return node;
}

/** A dialog that edits levels: resolves with the new levels, or null. */
function editLevels(title, intro, levels, options) {
  return openDialog((close) => {
    const editor = levelEditor(levels, options);
    return h("div", {}, h("h3", {}, title), intro && h("p", { class: "muted small" }, intro), editor,
      h("div", { class: "actions" }, h("button", { onclick: () => close(null) }, "Cancel"),
        h("button", { class: "primary", onclick: () => close(editor.value()) }, "Save")));
  });
}

const typeState = (data, id) => data.types.find((t) => t.id === id) || { enabled: false, available: false, ceiling: {} };
const ceilingOf = (data, resource) => typeState(data, resource.type).ceiling || {};

// ---- the page ---------------------------------------------------------------------------------------------

export function mountIntegrations(container, user) {
  let data = null;
  const timers = new Set();
  const approvalsPanel = h("section", { class: "panel", hidden: true });
  const accountsPanel = h("section", { class: "panel" });
  const resourcesPanel = h("section", { class: "panel" });
  const settingsPanel = h("section", { class: "panel" });
  const adminPanel = user.is_admin ? h("section", { class: "panel" }) : null;
  container.append(pageHead("Integrations"), h("div", { class: "scroll" }, h("div", { class: "container wide" },
    h("p", { class: "intro" }, "Connect your GitHub and Google Drive accounts, add folders, then attach them to a project or a conversation: Clara can then read and work on them. You decide what she may do. She asks you before anything that replaces or deletes, and goes on with something else while she waits."),
    approvalsPanel, accountsPanel, resourcesPanel, settingsPanel, adminPanel)));

  async function load() {
    try {
      data = await api.get("/v1/integrations", who(user));
      const pending = await pendingApprovals(user);
      drawApprovals(pending);
    } catch (error) { return fail(error); }
    drawAccounts();
    drawResources();
    drawSettings();
    if (adminPanel) loadAdmin();
  }

  // ---- waiting requests ----
  function drawApprovals(pending) {
    approvalsPanel.hidden = !pending.length;
    fill(approvalsPanel, 
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Waiting for your permission"),
        h("p", { class: "muted small" }, "Clara asked, and carries on with other work until you answer."))),
      h("div", { class: "panel-body stack" }, pending.map((item) => h("div", { class: "approval-item" },
        h("p", { class: "muted small" }, `In ${item.conversation}`), approvalCard(item, user, { onDecided: load })))));
  }

  // ---- accounts ----
  function drawAccounts() {
    const github = typeState(data, "github");
    const drive = typeState(data, "gdrive");
    const buttons = h("div", { class: "row wrap" },
      github.enabled && github.available && h("button", { onclick: connectGitHub }, icon("branch", { size: 18 }), "Connect GitHub"),
      drive.enabled && drive.available && h("button", { onclick: connectGoogle }, icon("cloud", { size: 18 }), "Connect Google Drive"));
    const rows = data.accounts.map((account) => {
      const [glyph, name] = ACCOUNTS[account.kind] || ["link", account.kind];
      const needs = account.status !== "ok";
      return h("li", {},
        h("span", { class: "glyph" }, icon(glyph, { size: 18 })),
        h("div", { class: "text" }, h("strong", {}, account.label), " ", h("span", { class: "muted small" }, name),
          " ", needs ? h("span", { class: "badge off" }, "Connect it again") : h("span", { class: "badge ok" }, "Connected"),
          h("div", { class: "muted small" }, "Default for its repositories and folders: ", levelBadges({ ...DEFAULT_LEVELS, ...account.levels }))),
        h("div", { class: "actions" },
          needs && h("button", { class: "sm", onclick: () => (account.kind === "github" ? connectGitHub() : connectGoogle()) }, "Reconnect"),
          h("button", { class: "ghost icon-btn", title: "Permissions", "aria-label": `Permissions of ${account.label}`, onclick: () => accountLevels(account) }, icon("shield", { size: 18 })),
          h("button", { class: "ghost icon-btn danger", title: "Disconnect", "aria-label": `Disconnect ${account.label}`, onclick: () => disconnect(account) }, icon("trash", { size: 18 }))));
    });
    fill(accountsPanel, 
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Accounts"),
        h("p", { class: "muted small" }, "What you connected is kept encrypted on the server and is never shown again.")), buttons),
      !drive.available && drive.enabled && h("div", { class: "panel-body" }, h("p", { class: "notice small" }, "Google Drive is not set up on this server: the administrator has to put a Google client in the server's .env (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET).")),
      rows.length ? h("ul", { class: "list" }, rows) : h("div", { class: "empty-state" }, mark(36), h("strong", {}, "No account yet"), "Connect GitHub with a token, or Google Drive with your Google account."));
  }

  function connectGitHub() {
    return openDialog((close) => {
      const token = h("input", { type: "password", autocomplete: "off", autofocus: true, placeholder: "github_pat_…" });
      const button = h("button", { class: "primary", type: "submit" }, "Connect");
      return h("form", { onsubmit: async (event) => {
        event.preventDefault();
        button.disabled = true;
        try {
          const done = await api.post("/v1/integrations/github", { ...who(user), token: token.value.trim() });
          toast(`GitHub connected as ${done.label}.`);
          close(true);
          load();
        } catch (error) { button.disabled = false; fail(error); }
      } },
      h("h3", {}, "Connect GitHub"),
      h("div", { class: "stack" },
        h("label", { class: "field" }, "Personal access token", token),
        h("p", { class: "muted small" }, "Make a fine-grained token at ",
          h("a", { href: "https://github.com/settings/personal-access-tokens/new", target: "_blank", rel: "noopener noreferrer" }, "github.com/settings/personal-access-tokens"),
          ", with access to the repositories Clara may use. “Contents: read and write” lets her commit; add “Pull requests” and “Issues” if you want those too. You can remove the token on GitHub at any time.")),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: () => close(null) }, "Cancel"), button));
    });
  }

  async function connectGoogle() {
    const win = window.open("about:blank", "_blank"); // opened now, while the click still counts, then sent to Google
    try { if (win) win.opener = null; } catch { /* a window that is not ours any more */ }
    let start;
    try {
      start = await api.post("/v1/integrations/google/start", who(user));
    } catch (error) {
      win?.close();
      return fail(error);
    }
    if (win) win.location.href = start.url;
    else {
      await openDialog((close) => h("div", {}, h("h3", {}, "Connect Google Drive"),
        h("p", {}, "Open this link to allow Clara to use your Drive, then come back here."),
        h("p", {}, h("a", { href: start.url, target: "_blank", rel: "noopener noreferrer" }, "Allow access in Google")),
        h("div", { class: "actions" }, h("button", { class: "primary", onclick: () => close(true) }, "Done"))));
    }
    toast("Finish in the Google window: this page updates when you are back.");
    const before = JSON.stringify(data.accounts.map((a) => [a.id, a.status]));
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      await load();
      if (JSON.stringify(data.accounts.map((a) => [a.id, a.status])) !== before || tries > 70) { clearInterval(timer); timers.delete(timer); }
    }, 2500);
    timers.add(timer);
  }

  async function accountLevels(account) {
    const levels = await editLevels(`Permissions of ${account.label}`,
      "What Clara may do with the repositories and folders you add from this account, unless one of them says otherwise.", account.levels,
      { ceiling: typeState(data, account.kind === "github" ? "github" : "gdrive").ceiling });
    if (!levels) return;
    try { await api.patch(`/v1/integrations/accounts/${account.id}`, { ...who(user), levels }); toast("Saved."); load(); } catch (error) { fail(error); }
  }

  async function disconnect(account) {
    if (!await confirmDialog("Disconnect", `Disconnect ${account.label}? The repositories and folders you added from it are removed from your projects and conversations. Nothing is deleted on ${ACCOUNTS[account.kind]?.[1] || "the other side"}.`, "Disconnect", true)) return;
    try { await api.delete(`/v1/integrations/accounts/${account.id}`, who(user)); toast("Disconnected."); load(); } catch (error) { fail(error); }
  }

  // ---- resources ----
  function drawResources() {
    const rows = data.resources.map((resource) => {
      const [glyph, kind] = KINDS[resource.kind] || ["file", resource.kind];
      const account = data.accounts.find((a) => a.id === resource.account);
      return h("li", {},
        h("span", { class: "glyph" }, icon(glyph, { size: 18 })),
        h("div", { class: "text" }, h("strong", {}, resource.label),
          h("div", { class: "muted small" }, kind + (account ? ` · ${account.label}` : "") + ` · ${resource.attachments ? `used in ${resource.attachments} place${resource.attachments === 1 ? "" : "s"}` : "not used yet"}`),
          h("div", {}, levelBadges(resource.effective))),
        h("div", { class: "actions" },
          h("button", { class: "ghost icon-btn", title: "Permissions", "aria-label": `Permissions of ${resource.label}`, onclick: () => resourceLevels(resource) }, icon("shield", { size: 18 })),
          h("button", { class: "ghost icon-btn danger", title: "Remove", "aria-label": `Remove ${resource.label}`, onclick: () => removeResource(resource) }, icon("trash", { size: 18 }))));
    });
    const anyType = data.types.some((t) => t.enabled && t.available && t.id !== "computer");
    fill(resourcesPanel, 
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Repositories and folders"),
        h("p", { class: "muted small" }, "What you can attach to a project or a conversation. Folders on your own computer are added from the Clara desktop app.")),
        h("button", { class: "primary", disabled: !anyType, onclick: addResource }, icon("plus", { size: 18 }), "Add")),
      rows.length ? h("ul", { class: "list" }, rows)
        : h("div", { class: "empty-state" }, mark(36), h("strong", {}, "Nothing added yet"), "Add a repository, a Drive folder or a folder, then attach it where you want Clara to use it."));
  }

  async function resourceLevels(resource) {
    const levels = await editLevels(`Permissions of ${resource.label}`,
      "What Clara may do here. “Same as the account” follows the account's default. A project or a conversation can still decide otherwise.",
      resource.levels, { inherit: "Same as the account", ceiling: ceilingOf(data, resource) });
    if (!levels) return;
    try { await api.patch(`/v1/integrations/resources/${resource.id}`, { ...who(user), levels }); toast("Saved."); load(); } catch (error) { fail(error); }
  }

  async function removeResource(resource) {
    if (!await confirmDialog("Remove", `Remove ${resource.label}? Clara can no longer reach it from the projects and conversations it was attached to. Nothing is deleted there.`, "Remove", true)) return;
    try { await api.delete(`/v1/integrations/resources/${resource.id}`, who(user)); toast("Removed."); load(); } catch (error) { fail(error); }
  }

  function addResource() {
    const choices = [
      ["github_repo", "branch", "GitHub repository", "github"],
      ["drive_folder", "cloud", "Google Drive folder", "gdrive"],
      ["drive_file", "file", "Google Drive file", "gdrive"],
      ["server_path", "server", "Folder on the server", "server"],
    ].filter(([, , , type]) => typeState(data, type).enabled && typeState(data, type).available);
    return openDialog((close) => {
      const body = h("div", { class: "stack" });
      const choose = () => fill(body, ...choices.map(([kind, glyph, label]) =>
        h("button", { class: "choice", onclick: () => pick(kind) }, icon(glyph, { size: 20 }), label)),
      h("p", { class: "muted small" }, "A folder on your own computer is added from the Clara desktop app (Integrations)."));
      const pick = (kind) => {
        const done = () => { close(true); load(); };
        const back = () => choose();
        if (kind === "github_repo") pickGitHub(body, done, back);
        else if (kind === "server_path") pickServer(body, done, back);
        else pickDrive(body, kind, done, back);
      };
      choose();
      return h("div", { class: "picker" }, h("h3", {}, "Add"), body, h("div", { class: "actions" }, h("button", { onclick: () => close(null) }, "Close")));
    });
  }

  const accountSelect = (kind) => {
    const found = data.accounts.filter((a) => a.kind === kind && a.status === "ok");
    return { found, select: h("select", { "aria-label": "Account" }, found.map((a) => h("option", { value: a.id }, a.label))) };
  };

  function pickGitHub(body, done, back) {
    const { found, select } = accountSelect("github");
    if (!found.length) return fill(body, h("p", {}, "Connect a GitHub account first."), h("button", { onclick: back }, "Back"));
    const search = h("input", { type: "search", placeholder: "Search your repositories", "aria-label": "Search your repositories" });
    const list = h("ul", { class: "list picker-list" });
    const typed = h("input", { placeholder: "owner/name or a github.com address", "aria-label": "Repository" });
    const branch = h("input", { placeholder: "Branch (optional)", "aria-label": "Branch" });
    let selected = "";
    const draw = (repos) => fill(list, ...(repos.length ? repos.map((repo) =>
      h("li", { class: repo.full_name === selected ? "chosen" : "" },
        h("button", { class: "ghost row-pick", onclick: () => { selected = repo.full_name; typed.value = repo.full_name; draw(repos); } },
          icon("branch", { size: 16 }), h("span", { class: "grow" }, h("strong", {}, repo.full_name), repo.description && h("span", { class: "muted small" }, ` ${repo.description}`)),
          h("span", { class: "badge" }, repo.private ? "Private" : "Public")))) : [h("li", { class: "empty-row" }, "No repository found.")]));
    let timer = 0;
    const find = async () => {
      try { draw((await api.get("/v1/integrations/browse/github", { ...who(user), account: select.value, q: search.value.trim() })).repos); } catch (error) { fail(error); }
    };
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(find, 250); });
    select.addEventListener("change", find);
    const add = h("button", { class: "primary", onclick: async () => {
      if (!typed.value.trim()) return toast("Choose a repository or type one.", true);
      add.disabled = true;
      try {
        await api.post("/v1/integrations/resources", { ...who(user), kind: "github_repo", account: Number(select.value), repo: typed.value.trim(), ref: branch.value.trim() });
        toast("Added."); done();
      } catch (error) { add.disabled = false; fail(error); }
    } }, "Add");
    fill(body, h("h4", {}, "GitHub repository"), found.length > 1 && select, search, list, h("div", { class: "row wrap" }, typed, branch),
      h("div", { class: "actions" }, h("button", { onclick: back }, "Back"), add));
    find();
  }

  function pickDrive(body, kind, done, back) {
    const { found, select } = accountSelect("gdrive");
    if (!found.length) return fill(body, h("p", {}, "Connect a Google Drive account first."), h("button", { onclick: back }, "Back"));
    const wantFolder = kind === "drive_folder";
    const search = h("input", { type: "search", placeholder: "Search by name", "aria-label": "Search by name" });
    const crumb = h("p", { class: "muted small" });
    const list = h("ul", { class: "list picker-list" });
    let here = { id: "root", name: "My Drive", parent: null };
    const use = async (item) => {
      try {
        await api.post("/v1/integrations/resources", { ...who(user), kind, account: Number(select.value), file_id: item.id });
        toast("Added."); done();
      } catch (error) { fail(error); }
    };
    async function show(folder) {
      let found2;
      try { found2 = await api.get("/v1/integrations/browse/drive", { ...who(user), account: select.value, folder, q: search.value.trim() }); } catch (error) { return fail(error); }
      if (found2.folder) here = { id: found2.folder.id, name: found2.folder.name, parent: found2.parent };
      fill(crumb, search.value.trim() ? "Search results" : `In ${here.name}`);
      clear(list);
      if (!search.value.trim() && here.parent) list.append(h("li", {}, h("button", { class: "ghost row-pick", onclick: () => show(here.parent) }, icon("back", { size: 16 }), "Up")));
      if (!search.value.trim() && wantFolder) list.append(h("li", {}, h("button", { class: "row-pick", onclick: () => use(here) }, icon("check", { size: 16 }), `Use “${here.name}”`)));
      for (const item of found2.items) {
        if (!wantFolder && item.folder) {
          list.append(h("li", {}, h("button", { class: "ghost row-pick", onclick: () => show(item.id) }, icon("folder", { size: 16 }), h("span", { class: "grow" }, item.name))));
        } else if (item.folder) {
          list.append(h("li", {}, h("button", { class: "ghost row-pick", onclick: () => { search.value = ""; show(item.id); } }, icon("folder", { size: 16 }), h("span", { class: "grow" }, item.name), icon("chevron", { size: 16 }))));
        } else if (!wantFolder) {
          list.append(h("li", {}, h("button", { class: "ghost row-pick", onclick: () => use(item) }, icon("file", { size: 16 }), h("span", { class: "grow" }, item.name), h("span", { class: "badge" }, "Use"))));
        }
      }
      if (!list.children.length) list.append(h("li", { class: "empty-row" }, "Nothing here."));
    }
    let timer = 0;
    search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => show(here.id), 300); });
    select.addEventListener("change", () => { here = { id: "root", name: "My Drive", parent: null }; show("root"); });
    fill(body, h("h4", {}, wantFolder ? "Google Drive folder" : "Google Drive file"), found.length > 1 && select, search, crumb, list,
      h("div", { class: "actions" }, h("button", { onclick: back }, "Back")));
    show("root");
  }

  function pickServer(body, done, back) {
    const crumb = h("p", { class: "muted small" });
    const list = h("ul", { class: "list picker-list" });
    async function show(path) {
      let found;
      try { found = await api.get("/v1/integrations/browse/server", { ...who(user), path }); } catch (error) { return fail(error); }
      fill(crumb, found.path || "Folders the administrator allows");
      clear(list);
      if (found.path) {
        list.append(h("li", {}, h("button", { class: "ghost row-pick", onclick: () => show(found.parent || "") }, icon("back", { size: 16 }), "Up")));
        list.append(h("li", {}, h("button", { class: "row-pick", onclick: async () => {
          try { await api.post("/v1/integrations/resources", { ...who(user), kind: "server_path", path: found.path }); toast("Added."); done(); } catch (error) { fail(error); }
        } }, icon("check", { size: 16 }), "Use this folder")));
      }
      for (const dir of found.dirs) {
        const full = found.roots ? dir : `${found.path}${found.path.includes("\\") ? "\\" : "/"}${dir}`;
        list.append(h("li", {}, h("button", { class: "ghost row-pick", onclick: () => show(full) }, icon("folder", { size: 16 }), h("span", { class: "grow" }, dir), icon("chevron", { size: 16 }))));
      }
      if (!found.dirs.length) list.append(h("li", { class: "empty-row" }, found.path ? "No sub-folder." : "The administrator has not allowed any folder yet."));
    }
    fill(body, h("h4", {}, "Folder on the server"), crumb, list, h("div", { class: "actions" }, h("button", { onclick: back }, "Back")));
    show("");
  }

  // ---- settings ----
  function drawSettings() {
    const current = data.settings.approval_notify_after;
    const select = h("select", { "aria-label": "Push a request that nobody answered after" }, NOTIFY_CHOICES.map(([value, text]) =>
      h("option", { value, selected: (current === null ? "" : String(current)) === value }, value === "" ? `${text} (${data.settings.default ? `${data.settings.default} seconds` : "never"})` : text)));
    select.addEventListener("change", async () => {
      try { await api.put("/v1/integrations/settings", { ...who(user), approval_notify_after: select.value === "" ? null : Number(select.value) }); toast("Saved."); } catch (error) { fail(error); }
    });
    fill(settingsPanel, 
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "When you do not answer"),
        h("p", { class: "muted small" }, `A request waits in its conversation. If you have not answered, it is sent to your other devices (the app, Discord, this site) so that you can answer there. It lapses after ${Math.round((data.settings.expire_after || 86400) / 3600)} hours, and Clara is told it was not done.`))),
      h("div", { class: "panel-body" }, h("label", { class: "field" }, "Send it to my other devices after", select)));
  }

  // ---- administrator ----
  async function loadAdmin() {
    let policy;
    let users = [];
    try {
      policy = await api.get("/v1/admin/integrations");
      users = (await api.get("/v1/admin/users")).users;
    } catch (error) { return fail(error); }
    drawAdmin(policy, users);
  }

  function drawAdmin(policy, users) {
    const save = async (patch) => {
      try { await api.put("/v1/admin/integrations", patch); toast("Saved."); await load(); } catch (error) { fail(error); }
    };
    const rows = policy.types.map((type) => {
      const on = h("input", { type: "checkbox", checked: policy.enabled[type.id], "aria-label": `${type.name} on`, onchange: () => save({ enabled: { [type.id]: on.checked } }) });
      const ceilings = LEVELS.map(([level]) => {
        const select = h("select", { "aria-label": `${type.name}: most Clara may be allowed to do (${SHORT[level]})`, onchange: () => {
          const next = { ...policy.ceiling[type.id] };
          if (select.value) next[level] = select.value; else delete next[level];
          save({ ceiling: { [type.id]: next } });
        } }, [["", "No limit"], ["ask", "Always ask"], ["deny", "Never"]].map(([value, text]) =>
          h("option", { value, selected: (policy.ceiling[type.id][level] || "") === value }, text)));
        return h("label", { class: "field small" }, SHORT[level], select);
      });
      return h("tr", {},
        h("td", {}, h("strong", {}, type.name), !type.available && h("div", { class: "muted small" }, type.id === "gdrive" ? "Needs a Google client in .env" : type.id === "computer" ? "Needs the desktop app" : "Not available")),
        h("td", { "data-label": "On" }, on),
        h("td", { "data-label": "Most she may be allowed" }, h("div", { class: "row wrap" }, ceilings)));
    });
    const roots = h("ul", { class: "list" }, policy.roots.map((root) => h("li", {},
      h("span", { class: "glyph" }, icon("server", { size: 18 })), h("div", { class: "text" }, root),
      h("div", { class: "actions" }, h("button", { class: "ghost icon-btn danger", title: "Remove", "aria-label": `Remove ${root}`, onclick: () => save({ roots: policy.roots.filter((r) => r !== root) }) }, icon("trash", { size: 18 }))))));
    const newRoot = h("input", { placeholder: "A folder on the server, e.g. /srv/clara-files", "aria-label": "A folder on the server" });
    const people = users.filter((u) => u.person);
    const peopleRows = people.map((u) => h("tr", {}, h("td", {}, h("strong", {}, u.name)),
      policy.types.map((type) => {
        const off = policy.disabled_users[type.id].includes(u.person.id);
        const box = h("input", { type: "checkbox", checked: !off, "aria-label": `${u.name}: ${type.name}`, onchange: () => {
          const set = new Set(policy.disabled_users[type.id]);
          if (box.checked) set.delete(u.person.id); else set.add(u.person.id);
          save({ disabled_users: { [type.id]: [...set] } });
        } });
        return h("td", { "data-label": type.name }, box);
      })));
    const log = h("div", {});
    fill(adminPanel, 
      h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Administration"),
        h("p", { class: "muted small" }, "Which integrations are on, how far people may open them up, the folders of the server they may pick from, and a log of what Clara did."))),
      h("table", { class: "grid cards" }, h("thead", {}, h("tr", {}, ["Integration", "On", "Limits"].map((t) => h("th", {}, t)))), h("tbody", {}, rows)),
      h("div", { class: "panel-body stack" }, h("h4", {}, "Folders of the server people may pick from"), roots,
        h("div", { class: "row wrap" }, newRoot, h("button", { onclick: () => newRoot.value.trim() && save({ roots: [...policy.roots, newRoot.value.trim()] }) }, "Allow this folder")),
        h("p", { class: "muted small" }, "Clara can reach nothing outside the folder a person picked inside one of these. Remove one and what was added from it stops working.")),
      people.length > 0 && h("div", { class: "panel-body stack" }, h("h4", {}, "Who may use what"),
        h("table", { class: "grid cards" }, h("thead", {}, h("tr", {}, ["Person", ...policy.types.map((t) => t.name)].map((t) => h("th", {}, t)))), h("tbody", {}, peopleRows))),
      h("div", { class: "panel-body stack" }, h("h4", {}, "Log"), log));
    showLog(log, null);
  }

  async function showLog(box, before) {
    let found;
    try { found = await api.get("/v1/admin/integrations/log", { limit: 50, before: before || undefined }); } catch (error) { return fail(error); }
    if (!before) clear(box);
    if (!found.entries.length && !before) return box.append(h("p", { class: "muted small" }, "Nothing yet."));
    const table = box.querySelector("table") || box.appendChild(h("table", { class: "grid cards" },
      h("thead", {}, h("tr", {}, ["When", "Who", "What", "Result"].map((t) => h("th", {}, t)))), h("tbody", {})));
    for (const entry of found.entries) {
      table.querySelector("tbody").append(h("tr", {},
        h("td", { "data-label": "When", title: entry.at }, ago(entry.at)),
        h("td", { "data-label": "Who" }, entry.person || "–"),
        h("td", { "data-label": "What" }, h("span", { class: `badge level-${entry.level}` }, levelWord(entry.level)), " ", entry.summary),
        h("td", { "data-label": "Result" }, h("span", { class: `badge ${["done", "approved"].includes(entry.outcome) ? "ok" : ["denied", "failed", "expired"].includes(entry.outcome) ? "off" : ""}` }, entry.outcome))));
    }
    box.querySelector(".more-log")?.remove();
    if (found.entries.length === 50) box.append(h("button", { class: "sm more-log", onclick: () => showLog(box, found.entries.at(-1).id) }, "Older"));
  }

  load();
  return {
    destroy() { for (const timer of timers) clearInterval(timer); timers.clear(); },
  };
}

// ---- the connections of a project or a conversation -------------------------------------------------------------

/** What is attached to a project (`{project}`) or a conversation (`{conversation}`): a panel with its own list and the
 *  button that attaches more. A conversation also shows what it gets from its project. Returns `{node, reload}`. */
export function connectionsPanel(user, target, { onChange } = {}) {
  const list = h("ul", { class: "list connections" });
  const attach = h("button", { class: "sm", onclick: attachDialog }, icon("plus", { size: 16 }), "Attach");
  const node = h("div", { class: "panel connections-panel" },
    h("div", { class: "panel-head" }, h("div", { class: "grow" }, h("h3", {}, "Connections"),
      h("p", { class: "muted small" }, target.project ? "What Clara can reach in every conversation of this project." : "What Clara can reach here, besides what the project gives her.")), attach),
    list);
  let found = { attachments: [], inherited: [] };
  let known = [];

  const query = () => ({ ...who(user), ...(target.project ? { project: target.project } : { conversation: target.conversation }) });

  async function reload() {
    try {
      found = await api.get("/v1/integrations/attachments", query());
      known = (await api.get("/v1/integrations", who(user))).types;
    } catch (error) { return fail(error); }
    draw();
    onChange?.(found.attachments.length + found.inherited.length);
  }

  function row(item, inherited) {
    const resource = item.resource;
    const [glyph, kind] = KINDS[resource.kind] || ["file", resource.kind];
    const ceiling = (known.find((t) => t.id === resource.type) || {}).ceiling || {};
    return h("li", {},
      h("span", { class: "glyph" }, icon(glyph, { size: 18 })),
      h("div", { class: "text" }, h("strong", {}, resource.label), inherited && h("span", { class: "badge" }, "From the project"),
        h("div", { class: "muted small" }, kind),
        h("div", {}, levelBadges(item.effective))),
      h("div", { class: "actions" },
        h("button", { class: "ghost icon-btn", title: "What Clara may do here", "aria-label": `Permissions of ${resource.label} here`, onclick: async () => {
          const levels = await editLevels(`${resource.label}: here`, target.project ? "For every conversation of this project." : "For this conversation only. “Same as the resource” follows its own permissions.",
            item.levels, { inherit: "Same as the resource", ceiling });
          if (!levels) return;
          try { await api.put("/v1/integrations/attachments", { ...who(user), resource: resource.id, ...target, levels }); reload(); } catch (error) { fail(error); }
        } }, icon("shield", { size: 18 })),
        !inherited && h("button", { class: "ghost icon-btn danger", title: "Detach", "aria-label": `Detach ${resource.label}`, onclick: async () => {
          try { await api.delete(`/v1/integrations/attachments/${item.attachment}`, who(user)); reload(); } catch (error) { fail(error); }
        } }, icon("close", { size: 18 }))));
  }

  function draw() {
    const items = [...found.inherited.map((i) => row(i, true)), ...found.attachments.map((i) => row(i, false))];
    fill(list, ...(items.length ? items : [h("li", { class: "empty-row" }, "Nothing connected here yet.")]));
  }

  async function attachDialog() {
    let all;
    try { all = await api.get("/v1/integrations", who(user)); } catch (error) { return fail(error); }
    const attached = new Set([...found.attachments, ...found.inherited].map((i) => i.resource.id));
    const free = all.resources.filter((r) => !attached.has(r.id));
    await openDialog((close) => h("div", { class: "picker" }, h("h3", {}, "Attach"),
      free.length ? h("ul", { class: "list picker-list" }, free.map((resource) => h("li", {},
        h("button", { class: "ghost row-pick", onclick: async () => {
          try { await api.put("/v1/integrations/attachments", { ...who(user), resource: resource.id, ...target }); close(true); toast("Attached."); } catch (error) { fail(error); }
        } }, icon((KINDS[resource.kind] || ["file"])[0], { size: 16 }), h("span", { class: "grow" }, resource.label), h("span", { class: "muted small" }, (KINDS[resource.kind] || [0, ""])[1])))))
        : h("p", { class: "muted" }, all.resources.length ? "Everything you added is attached already." : "You have not added anything yet.", " ",
          h("a", { href: "#/integrations", onclick: () => close(null) }, "Add a repository or a folder in Integrations")),
      h("div", { class: "actions" }, h("button", { onclick: () => close(null) }, "Close"))));
    reload();
  }

  reload();
  return { node, reload };
}

/** The connections of one conversation, in a dialog; `onChange(count)` after it closes. */
export async function openConnections(user, conversation, projectName) {
  const panel = connectionsPanel(user, { conversation });
  await openDialog((close) => h("div", { class: "connections-dialog" },
    projectName && h("p", { class: "muted small" }, `This conversation is in the project ${projectName}: what is connected to the project is available here too.`),
    panel.node, h("div", { class: "actions" }, h("button", { onclick: () => close(null) }, "Close"))));
}

/** How many things are attached to a conversation (or its project), for a badge; 0 when it cannot be read. */
export async function connectionCount(user, conversation) {
  try {
    const found = await api.get("/v1/integrations/attachments", { ...who(user), conversation });
    return found.attachments.length + found.inherited.length;
  } catch { return 0; }
}
