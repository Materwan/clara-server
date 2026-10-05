// The markdown files Clara writes: a card in the chat when she makes or changes one, and the Files page where they
// all are. They belong to you and are the same on every client; ask Clara to change one and she edits it in place.

import { api } from "./api.js";
import { mark } from "./icons.js";
import { renderMarkdown } from "./markdown.js";
import { sizeText } from "./projects.js";
import { ago, clear, confirmDialog, copy, dateTime, h, icon, openDialog, pageHead, toast } from "./ui.js";

const SURFACE = "web";
const ACTIONS = { created: "Created", updated: "Updated", replaced: "Rewritten" };

export const filesOf = (user) => ({ surface: SURFACE, user_id: user.name });

async function fetchFile(file, query) {
  return api.get(`/v1/markdown-files/${file.id}`, query);
}

/** Save a file to the computer: the text goes in a blob that the browser offers as a download. */
async function download(file, query) {
  try {
    const { name, content } = await fetchFile(file, query);
    const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
    const link = h("a", { href: url, download: name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch (error) {
    toast(error.detail || String(error), true);
  }
}

/** Show a file, read now (Clara may have changed it since the card was drawn): rendered, or as the text it is. */
export async function openFile(file, query) {
  let found;
  try {
    found = await fetchFile(file, query);
  } catch (error) {
    toast(error.status === 404 ? "This file does not exist any more." : error.detail || String(error), true);
    return;
  }
  await openDialog((close) => {
    const body = h("div", { class: "file-body" });
    const tabs = h("div", { class: "segmented", role: "tablist", "aria-label": "How to show the file" });
    const show = (mode) => {
      clear(tabs).append(...[["preview", "Preview"], ["source", "Markdown"]].map(([id, label]) =>
        h("button", { type: "button", role: "tab", "aria-selected": String(id === mode), onclick: () => show(id) }, label)));
      clear(body).append(mode === "preview"
        ? h("div", { class: "md file-preview" }, renderMarkdown(found.content))
        : h("pre", { class: "file-text" }, found.content));
    };
    show("preview");
    return h("div", { class: "file-view md-file" },
      h("h3", {}, found.name),
      h("p", { class: "muted small" }, `${sizeText(found.size)} · changed ${dateTime(found.updated_at)}`),
      tabs,
      body,
      h("div", { class: "actions" },
        h("button", { onclick: () => copy(found.content) }, icon("copy", { size: 18 }), "Copy"),
        h("button", { onclick: () => download(found, query) }, icon("download", { size: 18 }), "Download"),
        h("button", { class: "primary", onclick: () => close(true) }, "Close")));
  });
}

/** What the chat shows when Clara made or changed a file. */
export function fileCard(entry, query) {
  return h("div", { class: "file-card" },
    h("span", { class: "glyph" }, icon("file", { size: 20 })),
    h("div", { class: "info" },
      h("strong", {}, entry.name),
      h("span", { class: "muted small" }, `${ACTIONS[entry.action] || "Saved"} · ${sizeText(entry.size)}`)),
    h("div", { class: "actions" },
      h("button", { class: "sm", onclick: () => openFile(entry, query) }, "Open"),
      h("button", { class: "ghost icon-btn", title: "Download", "aria-label": `Download ${entry.name}`, onclick: () => download(entry, query) },
        icon("download", { size: 18 }))));
}

export function mountFiles(container, user) {
  const query = filesOf(user);
  const list = h("ul", { class: "list", "aria-label": "Your markdown files" });
  const count = h("span", { class: "count" });
  const root = h("section", { class: "page" },
    pageHead("Files", count),
    h("div", { class: "scroll" }, h("div", { class: "container" },
      h("p", { class: "intro" }, "The markdown files Clara writes for you. Ask her for a document, notes or a summary in a chat, then ask her to change it: she edits the same file."),
      h("div", { class: "panel" }, list))));
  container.append(root);

  let files = [];
  let loaded = false;

  function draw() {
    clear(list);
    count.textContent = loaded ? `${files.length} ${files.length === 1 ? "file" : "files"}` : "";
    if (!loaded) return;
    if (!files.length) {
      list.append(h("li", { class: "empty-row" }, h("div", { class: "empty-state" }, mark(36),
        h("strong", {}, "No file yet"),
        "Try: “Write me a one-page summary of this chat as a markdown file”.")));
      return;
    }
    for (const file of files) {
      list.append(h("li", {},
        h("span", { class: "glyph" }, icon("file", { size: 20 })),
        h("div", { class: "text" },
          h("button", { class: "linkish", onclick: () => openFile(file, query) }, file.name),
          h("div", { class: "muted small", title: dateTime(file.updated_at) }, `${sizeText(file.size)} · changed ${ago(file.updated_at)}`)),
        h("div", { class: "actions" },
          h("button", { class: "ghost icon-btn", title: "Open", "aria-label": `Open ${file.name}`, onclick: () => openFile(file, query) }, icon("search", { size: 18 })),
          h("button", { class: "ghost icon-btn", title: "Download", "aria-label": `Download ${file.name}`, onclick: () => download(file, query) }, icon("download", { size: 18 })),
          h("button", { class: "ghost icon-btn danger forget", title: "Delete", "aria-label": `Delete ${file.name}`, onclick: () => remove(file) }, icon("trash", { size: 18 })))));
    }
  }

  async function remove(file) {
    if (!await confirmDialog("Delete this file", `${file.name} will be deleted for good.`, "Delete", true)) return;
    try {
      await api.delete(`/v1/markdown-files/${file.id}`, query);
      files = files.filter((item) => item.id !== file.id);
      draw();
      toast("Deleted.");
    } catch (error) {
      toast(error.detail || String(error), true);
    }
  }

  (async () => {
    try {
      files = (await api.get("/v1/markdown-files", query)).files;
    } catch (error) {
      if (error.status !== 401) toast(error.detail || String(error), true);
      return;
    }
    loaded = true;
    draw();
  })();

  return { destroy() { root.remove(); } };
}
