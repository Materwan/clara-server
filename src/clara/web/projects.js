// Projects: files, folders and GitHub repositories that Clara uses in every conversation of the project, with
// instructions of their own. The list of projects, and one project: its conversations, instructions and files.

import { api } from "./api.js";
import { splitMessage } from "./documents.js";
import { mark } from "./icons.js";
import { ago, clear, confirmDialog, copy, h, icon, openDialog, pageHead, parseDate, popupMenu, toast } from "./ui.js";

const SURFACE = "web";
const BATCH_BYTES = 6_000_000; // bytes of files per request (base64 makes them a third bigger)
const BATCH_FILES = 200;
const MAX_FILE_BYTES = 30_000_000;
const FILES_SHOWN = 300;

// What a folder holds that is not worth sending (the server leaves it out too: see ingest.py)
const IGNORED_DIRS = new Set([
  ".git", ".hg", ".svn", "node_modules", "bower_components", "__pycache__", ".venv", "venv", ".tox", ".nox",
  ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".gradle", ".idea", ".next", ".nuxt", ".svelte-kit",
  ".parcel-cache", ".turbo", "dist", "build", "target", "out", "coverage", "htmlcov", ".terraform", ".dart_tool",
  "Pods", "DerivedData", ".eggs",
]);
const IGNORED_FILES = new Set([
  "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "composer.lock", "Gemfile.lock",
  "uv.lock", "Pipfile.lock", "go.sum", ".DS_Store", "Thumbs.db", "desktop.ini",
]);
const IGNORED_SUFFIXES = [".min.js", ".min.css", ".map", ".pyc", ".pyo", ".lock"];
const BINARY = new Set((
  "png jpg jpeg gif bmp ico webp tif tiff psd heic avif mp3 wav ogg flac m4a aac mp4 mkv avi mov webm wmv exe dll so " +
  "dylib bin o a lib obj class jar war apk msi dmg iso woff woff2 ttf otf eot 7z rar gz tgz bz2 xz tar zst sqlite " +
  "sqlite3 db pkl pickle npy npz pt pth onnx h5 parquet doc xls xlsx ppt pptx odt ods odp key pages numbers"
).split(" "));

const who = (user) => ({ surface: SURFACE, user_id: user.name });

export function sizeText(characters) {
  if (characters < 1000) return `${characters} chars`;
  if (characters < 1_000_000) return `${Math.round(characters / 1000)} k chars`;
  return `${(characters / 1_000_000).toFixed(1)} M chars`;
}

const plural = (count, word) => `${count.toLocaleString()} ${word}${count === 1 ? "" : "s"}`;

/** Why a file of a folder is not sent, or "". */
function leftOut(path) {
  const parts = path.split("/");
  const folder = parts.slice(0, -1).find((part) => IGNORED_DIRS.has(part) || part.endsWith(".egg-info"));
  if (folder) return `in ${folder}/`;
  const name = parts.at(-1);
  if (IGNORED_FILES.has(name) || IGNORED_SUFFIXES.some((suffix) => name.toLowerCase().endsWith(suffix))) return "generated file";
  const dot = name.lastIndexOf(".");
  if (dot > 0 && BINARY.has(name.slice(dot + 1).toLowerCase())) return "not a text file";
  return "";
}

export async function listProjects(user) {
  return (await api.get("/v1/projects", who(user))).projects;
}

/** Ask which project a conversation goes in: its id, 0 for none, or null (cancelled). */
export async function chooseProject(user, current) {
  let projects;
  try {
    projects = await listProjects(user);
  } catch (error) {
    toast(error.detail || String(error), true);
    return null;
  }
  if (!projects.length) {
    toast("You have no project yet: make one on the Projects page.");
    return null;
  }
  return openDialog((close) => {
    const select = h("select", { autofocus: true },
      h("option", { value: "0", selected: !current }, "No project"),
      projects.map((project) => h("option", { value: String(project.id), selected: project.id === current }, project.name)));
    return h("form", { onsubmit: (event) => { event.preventDefault(); close(Number(select.value)); } },
      h("h3", {}, "Move to a project"),
      h("p", {}, "From the next message on, Clara uses the files and instructions of the project you choose."),
      h("div", { class: "stack" }, h("label", { class: "field" }, "Project", select)),
      h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { class: "primary", type: "submit" }, "Move")));
  });
}

function projectForm(title, okLabel, project = {}) {
  return openDialog((close) => {
    const name = h("input", { type: "text", value: project.name || "", maxLength: 100, required: true, autofocus: true });
    const description = h("textarea", { rows: 2, maxLength: 2000, placeholder: "What is it about? (shown here, and to Clara)" });
    description.value = project.description || "";
    const instructions = h("textarea", { rows: 6, maxLength: 20000, placeholder: "How Clara should work in this project: tone, language, conventions…" });
    instructions.value = project.instructions || "";
    return h("form", { class: "project-form", onsubmit: (event) => {
      event.preventDefault();
      if (!name.value.trim()) return name.focus();
      close({ name: name.value.trim(), description: description.value.trim(), instructions: instructions.value.trim() });
    } },
    h("h3", {}, title),
    h("div", { class: "stack" },
      h("label", { class: "field" }, "Name", name),
      h("label", { class: "field" }, "Description", description),
      h("label", { class: "field" }, "Instructions", instructions)),
    h("div", { class: "actions" },
      h("button", { type: "button", onclick: () => close(null) }, "Cancel"),
      h("button", { class: "primary", type: "submit" }, okLabel)));
  });
}

async function createProject(user) {
  const values = await projectForm("New project", "Create");
  if (!values) return;
  try {
    const project = await api.post("/v1/projects", { ...who(user), user_name: user.person?.name || user.name, ...values });
    location.hash = `#/projects/${project.id}`;
  } catch (error) {
    toast(error.detail || String(error), true);
  }
}

export function mountProjects(container, user, sub) {
  const id = Number(sub);
  return Number.isInteger(id) && id > 0 ? mountProject(container, user, id) : mountList(container, user);
}

// ---- the list ------------------------------------------------------------------------------------------------

function mountList(container, user) {
  const grid = h("div", { class: "project-grid" });
  const root = h("section", { class: "page" },
    pageHead("Projects", h("button", { class: "primary sm", onclick: () => createProject(user) }, icon("plus", { size: 16 }), "New project")),
    h("div", { class: "scroll" }, h("div", { class: "container wide" },
      h("p", { class: "intro" }, "A project keeps files, folders and GitHub repositories with instructions of its own. Every conversation of the project can use them; they are the same in the desktop app."),
      grid)));
  container.append(root);

  (async () => {
    let projects;
    try {
      projects = await listProjects(user);
    } catch (error) {
      if (error.status !== 401) toast(error.detail || String(error), true);
      return;
    }
    clear(grid);
    if (!projects.length) {
      grid.append(h("div", { class: "panel empty-project" }, h("div", { class: "empty-state" }, mark(40),
        h("strong", {}, "No project yet"),
        h("p", {}, "Make one for a code base, a course, a book you are writing… then add its files."),
        h("button", { class: "primary", onclick: () => createProject(user) }, icon("plus", { size: 18 }), "New project"))));
      return;
    }
    for (const project of projects) {
      grid.append(h("a", { class: "project-card", href: `#/projects/${project.id}` },
        h("div", { class: "project-card-head" }, h("span", { class: "glyph" }, icon("folder", { size: 20 })), h("strong", {}, project.name)),
        project.description ? h("p", { class: "description" }, project.description) : h("p", { class: "description muted" }, "No description"),
        h("div", { class: "meta" },
          h("span", {}, plural(project.files, "file")),
          h("span", {}, plural(project.conversations, "chat")),
          h("span", {}, `updated ${ago(project.updated_at)}`))));
    }
  })();

  return { destroy() { root.remove(); } };
}

// ---- one project ---------------------------------------------------------------------------------------------

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error || new Error(`${file.name} could not be read`));
    reader.readAsDataURL(file);
  });
}

/** The files of what was dropped, folders included: `[{file, path, inFolder}]`. */
async function droppedFiles(items) {
  const found = [];
  const walk = async (entry, prefix) => {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      found.push({ file, path: prefix + entry.name, inFolder: Boolean(prefix) });
    } else if (entry.isDirectory) {
      if (IGNORED_DIRS.has(entry.name)) return;
      const reader = entry.createReader();
      for (;;) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
      }
    }
  };
  const entries = [...items].map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  for (const entry of entries) await walk(entry, "");
  return found;
}

function mountProject(container, user, id) {
  let project = null;
  let busy = false;
  const title = h("h1", { class: "title grow" }, "Project");
  const chatButton = h("button", { class: "primary sm", onclick: () => { location.hash = `#/chat/new/${id}`; } }, icon("plus", { size: 16 }), h("span", { class: "hide-sm" }, "New chat"));
  const moreButton = h("button", { class: "ghost icon-btn", "aria-label": "Project actions", title: "Project actions",
    onclick: (event) => { event.stopPropagation(); popupMenu(moreButton, [
      { label: "Edit", icon: "edit", run: edit },
      "-",
      { label: "Delete project", icon: "trash", danger: true, run: remove },
    ]); } }, icon("more"));
  const back = h("a", { class: "ghost icon-btn back", href: "#/projects", title: "All projects", "aria-label": "All projects" }, icon("back"));

  const description = h("p", { class: "intro" });
  const convos = h("ul", { class: "list", "aria-label": "Conversations of the project" });
  const instructions = h("div", { class: "panel-body instructions" });
  const filesSummary = h("p", { class: "small files-summary" });
  const contextNote = h("p", { class: "muted small" });
  const progress = h("p", { class: "notice small", hidden: true, role: "status" });
  const sources = h("ul", { class: "list sources" });
  const filter = h("input", { type: "search", placeholder: "Filter files", "aria-label": "Filter files", hidden: true });
  const fileList = h("ul", { class: "list files", "aria-label": "Files" });

  const pickFiles = h("input", { type: "file", multiple: true, hidden: true,
    onchange: () => { upload([...pickFiles.files].map((file) => ({ file, path: file.name, inFolder: false }))); pickFiles.value = ""; } });
  const pickFolder = h("input", { type: "file", multiple: true, hidden: true, webkitdirectory: true,
    onchange: () => { upload([...pickFolder.files].map((file) => ({ file, path: file.webkitRelativePath || file.name, inFolder: true }))); pickFolder.value = ""; } });
  const actionButtons = [
    h("button", { class: "sm", onclick: () => pickFiles.click(), title: "Text, code, Markdown, PDF, Word, .zip" }, icon("upload", { size: 16 }), "Files"),
    h("button", { class: "sm", onclick: () => pickFolder.click(), title: "A folder and its text files" }, icon("folder", { size: 16 }), "Folder"),
    h("button", { class: "sm", onclick: addRepository, title: "Download a GitHub repository" }, icon("branch", { size: 16 }), "GitHub"),
  ];

  const page = h("div", { class: "project-layout" },
    h("div", { class: "project-main" },
      description,
      h("div", { class: "panel" },
        h("div", { class: "panel-head" }, h("h3", { class: "grow" }, "Conversations"),
          h("button", { class: "sm", onclick: () => { location.hash = `#/chat/new/${id}`; } }, icon("plus", { size: 16 }), "New chat")),
        convos)),
    h("div", { class: "project-side" },
      h("div", { class: "panel" },
        h("div", { class: "panel-head" }, h("h3", { class: "grow" }, "Instructions"),
          h("button", { class: "ghost icon-btn", title: "Edit", "aria-label": "Edit the instructions", onclick: edit }, icon("edit", { size: 18 }))),
        instructions),
      h("div", { class: "panel drop-zone" },
        h("div", { class: "panel-head" }, h("h3", { class: "grow" }, "Files")),
        h("div", { class: "panel-body stack" },
          h("div", {}, filesSummary, contextNote),
          h("div", { class: "row wrap" }, actionButtons, pickFiles, pickFolder),
          progress,
          h("p", { class: "muted small drop-hint" }, "Or drop files and folders here.")),
        sources,
        h("div", { class: "files-filter" }, filter),
        fileList)));

  const root = h("section", { class: "page project-page" },
    pageHead(h("div", { class: "row grow head-title" }, back, title), chatButton, moreButton),
    h("div", { class: "scroll" }, h("div", { class: "container wide" }, page)));
  container.append(root);

  // ---- drawing ----------------------------------------------------------------------------------------
  function draw() {
    if (!project) return;
    title.textContent = project.name;
    document.title = `${project.name} – Clara`;
    description.textContent = project.description;
    description.hidden = !project.description;
    clear(instructions).append(project.instructions
      ? h("p", { class: "pre" }, project.instructions)
      : h("p", { class: "muted small" }, "None yet. Tell Clara how to work in this project: the language to answer in, conventions, what to focus on…"));
    filesSummary.textContent = project.files
      ? `${plural(project.files, "file")}, ${sizeText(project.size)} (at most ${plural(project.limits.files, "file")} and ${sizeText(project.limits.size)})`
      : "No file yet.";
    const context = project.context;
    contextNote.textContent = !project.files ? "Add text, code, PDF or Word files, a folder, a .zip or a GitHub repository."
      : context.inline ? `Clara reads all of them with every message: they take ${context.percent}% of the model's context.`
        : `Too big to be read whole by the model in use (${context.percent}% of its context): Clara searches and reads them when she needs to.`;
    drawSources();
    drawFiles();
  }

  function drawSources() {
    clear(sources);
    for (const source of project.sources) {
      const synced = source.synced_at ? `synced ${ago(source.synced_at)}` : "never synced";
      const sub = source.problem ? h("span", { class: "error" }, source.problem)
        : `${plural(source.files, "file")} in ${source.folder}/, ${synced}` + (source.commit ? `, ${source.commit.slice(0, 7)}` : "") +
          (source.skipped ? `, ${source.skipped} left out` : "");
      sources.append(h("li", {},
        h("span", { class: "glyph" }, icon("branch", { size: 18 })),
        h("div", { class: "text" }, h("strong", {}, source.repo + (source.ref ? ` @ ${source.ref}` : "")), h("div", { class: "muted small" }, sub)),
        h("div", { class: "actions" },
          h("button", { class: "ghost icon-btn", title: "Download it again", "aria-label": `Sync ${source.repo}`, onclick: () => sync(source) }, icon("refresh", { size: 18 })),
          h("button", { class: "ghost icon-btn danger", title: "Remove it and its files", "aria-label": `Remove ${source.repo}`, onclick: () => removeSource(source) }, icon("trash", { size: 18 })))));
    }
  }

  function drawFiles() {
    clear(fileList);
    const files = project.file_list;
    filter.hidden = files.length <= 8;
    const needle = filter.value.trim().toLowerCase();
    const shown = needle ? files.filter((file) => file.path.toLowerCase().includes(needle)) : files;
    for (const file of shown.slice(0, FILES_SHOWN)) {
      fileList.append(h("li", {},
        h("button", { class: "ghost file-name", title: "Show its text", onclick: () => view(file) }, icon("file", { size: 16 }), h("span", {}, file.path)),
        h("span", { class: "count" }, sizeText(file.size)),
        h("button", { class: "ghost icon-btn danger forget", title: "Remove", "aria-label": `Remove ${file.path}`, onclick: () => removeFile(file.path) }, icon("close", { size: 16 }))));
    }
    if (shown.length > FILES_SHOWN) fileList.append(h("li", { class: "empty-row" }, `${shown.length - FILES_SHOWN} more: filter to find them.`));
    else if (needle && !shown.length) fileList.append(h("li", { class: "empty-row" }, "No file matches."));
  }

  async function load() {
    try {
      project = await api.get(`/v1/projects/${id}`, who(user));
    } catch (error) {
      if (error.status === 404) {
        toast("This project does not exist (any more).", true);
        location.hash = "#/projects";
      } else if (error.status !== 401) toast(error.detail || String(error), true);
      return;
    }
    draw();
    loadConversations();
  }

  async function loadConversations() {
    let found;
    try {
      found = (await api.get("/v1/conversations", { ...who(user), project: id })).conversations;
    } catch { return; }
    clear(convos);
    if (!found.length) {
      convos.append(h("li", { class: "empty-row" }, "No conversation yet: start one with New chat."));
      return;
    }
    for (const info of found) {
      const label = info.title || splitMessage(info.preview || "").text || "New chat";
      const date = parseDate(info.updated_at);
      convos.append(h("li", { class: "convo-row", tabindex: 0, role: "link",
        onclick: () => openConversation(info.id),
        onkeydown: (event) => { if (event.key === "Enter") openConversation(info.id); } },
      h("span", { class: "glyph" }, icon("chat", { size: 18 })),
      h("span", { class: "text" }, label),
      h("span", { class: "count", title: date ? date.toLocaleString() : "" }, ago(info.updated_at))));
    }
  }

  const openConversation = (conversation) => { location.hash = `#/chat/open/${encodeURIComponent(conversation)}`; };

  // ---- actions ----------------------------------------------------------------------------------------
  function setBusy(text) {
    busy = Boolean(text);
    progress.hidden = !text;
    progress.textContent = text || "";
    for (const button of actionButtons) button.disabled = busy;
  }

  function skippedDialog(skipped) {
    return openDialog((close) => h("div", { class: "skipped-view" },
      h("h3", {}, `${plural(skipped.length, "file")} left out`),
      h("ul", { class: "skipped" }, skipped.slice(0, 300).map((item) => h("li", {}, h("code", {}, item.path), " ", h("span", { class: "muted" }, item.reason)))),
      skipped.length > 300 && h("p", { class: "muted small" }, `And ${skipped.length - 300} more.`),
      h("div", { class: "actions" }, h("button", { class: "primary", onclick: () => close(true) }, "OK"))));
  }

  function report(added, skipped, total = skipped.length) {
    const parts = [`${plural(added, "file")} added`];
    if (total) parts.push(`${total.toLocaleString()} left out`);
    toast(parts.join(", ") + ".", !added && total > 0);
    if (skipped.some((item) => !["generated file", "not a text file"].includes(item.reason) && !item.reason.startsWith("in "))) skippedDialog(skipped);
  }

  async function upload(entries) {
    if (busy || !entries.length) return;
    const skipped = [];
    const kept = [];
    for (const entry of entries) {
      const reason = entry.inFolder ? leftOut(entry.path) : "";
      if (reason) skipped.push({ path: entry.path, reason });
      else if (entry.file.size > MAX_FILE_BYTES) skipped.push({ path: entry.path, reason: `too big (${Math.round(entry.file.size / 1e6)} MB)` });
      else kept.push(entry);
    }
    let added = 0;
    let total = skipped.length;
    let sent = 0;
    try {
      while (sent < kept.length) {
        const batch = [];
        let bytes = 0;
        while (sent < kept.length && batch.length < BATCH_FILES && (!batch.length || bytes + kept[sent].file.size <= BATCH_BYTES)) {
          bytes += kept[sent].file.size;
          batch.push(kept[sent++]);
        }
        setBusy(`Sending ${sent.toLocaleString()} of ${plural(kept.length, "file")}…`);
        const files = await Promise.all(batch.map(async (entry) => ({ path: entry.path, data: await readAsBase64(entry.file) })));
        const result = await api.post(`/v1/projects/${id}/files`, { ...who(user), files });
        added += result.added.length + result.replaced.length;
        skipped.push(...result.skipped);
        total += result.skipped_count;
        project = result.project;
        draw();
      }
    } catch (error) {
      toast(error.detail || String(error), true);
    } finally {
      setBusy("");
    }
    report(added, skipped, total);
  }

  async function addRepository() {
    if (busy) return;
    const values = await openDialog((close) => {
      const repo = h("input", { type: "text", placeholder: "owner/name or https://github.com/owner/name", required: true, autofocus: true });
      const ref = h("input", { type: "text", placeholder: "default branch" });
      return h("form", { onsubmit: (event) => { event.preventDefault(); if (repo.value.trim()) close({ repo: repo.value.trim(), ref: ref.value.trim() }); } },
        h("h3", {}, "Add a GitHub repository"),
        h("p", {}, "Clara's server downloads its text files (not its history). Sync it to get its latest version."),
        h("div", { class: "stack" },
          h("label", { class: "field" }, "Repository", repo),
          h("label", { class: "field" }, h("span", {}, "Branch, tag or commit ", h("span", { class: "hint" }, "(optional)")), ref),
          h("p", { class: "muted small" }, "Public repositories work as they are; private ones need a GITHUB_TOKEN on the server.")),
        h("div", { class: "actions" },
          h("button", { type: "button", onclick: () => close(null) }, "Cancel"),
          h("button", { class: "primary", type: "submit" }, "Add")));
    });
    if (!values) return;
    setBusy(`Downloading ${values.repo}…`);
    try {
      const result = await api.post(`/v1/projects/${id}/github`, { ...who(user), ...values });
      project = result.project;
      draw();
      report(result.added.length, result.skipped, result.skipped_count);
    } catch (error) {
      toast(error.detail || String(error), true);
    } finally {
      setBusy("");
    }
  }

  async function sync(source) {
    if (busy) return;
    setBusy(`Downloading ${source.repo} again…`);
    try {
      const result = await api.post(`/v1/projects/${id}/sources/${source.id}/sync`, who(user));
      project = result.project;
      draw();
      toast(`${source.repo} is up to date: ${plural(result.added.length, "file")}.`);
    } catch (error) {
      toast(error.detail || String(error), true);
      await load();
    } finally {
      setBusy("");
    }
  }

  async function removeSource(source) {
    if (!await confirmDialog("Remove the repository", `Remove ${source.repo} and its ${plural(source.files, "file")} from the project?`, "Remove", true)) return;
    try {
      project = (await api.delete(`/v1/projects/${id}/sources/${source.id}`, who(user))).project;
      draw();
    } catch (error) { toast(error.detail || String(error), true); }
  }

  async function removeFile(path) {
    try {
      project = (await api.delete(`/v1/projects/${id}/files`, { ...who(user), path })).project;
      draw();
    } catch (error) { toast(error.detail || String(error), true); }
  }

  async function view(file) {
    let body;
    try {
      body = await api.get(`/v1/projects/${id}/file`, { ...who(user), path: file.path });
    } catch (error) { return toast(error.detail || String(error), true); }
    openDialog((close) => h("div", { class: "file-view" },
      h("h3", {}, body.path),
      h("p", { class: "muted small" }, `${body.kind || "text"}, ${sizeText(body.size)}`),
      h("pre", { class: "file-text" }, body.content),
      h("div", { class: "actions" },
        h("button", { onclick: () => copy(body.content) }, icon("copy", { size: 18 }), "Copy"),
        h("button", { class: "primary", onclick: () => close(true) }, "Close"))));
  }

  async function edit() {
    if (!project) return;
    const values = await projectForm("Edit the project", "Save", project);
    if (!values) return;
    try {
      project = await api.patch(`/v1/projects/${id}`, { ...who(user), ...values });
      draw();
    } catch (error) { toast(error.detail || String(error), true); }
  }

  async function remove() {
    if (!project) return;
    const chats = project.conversations ? ` Its ${plural(project.conversations, "conversation")} stay, in your list of chats.` : "";
    if (!await confirmDialog("Delete the project", `Delete “${project.name}” and its files?${chats}`, "Delete", true)) return;
    try {
      await api.delete(`/v1/projects/${id}`, who(user));
      toast("Project deleted.");
      location.hash = "#/projects";
    } catch (error) { toast(error.detail || String(error), true); }
  }

  filter.addEventListener("input", drawFiles);
  const zone = root.querySelector(".drop-zone");
  for (const type of ["dragenter", "dragover"]) root.addEventListener(type, (event) => { event.preventDefault(); zone.classList.add("drop"); });
  for (const type of ["dragleave", "drop"]) root.addEventListener(type, (event) => { if (type === "drop" || !root.contains(event.relatedTarget)) zone.classList.remove("drop"); });
  root.addEventListener("drop", async (event) => {
    event.preventDefault();
    const items = event.dataTransfer.items;
    const entries = items && items.length && items[0].webkitGetAsEntry ? await droppedFiles(items)
      : [...event.dataTransfer.files].map((file) => ({ file, path: file.name, inFolder: false }));
    upload(entries);
  });

  load();
  return { destroy() { root.remove(); } };
}
