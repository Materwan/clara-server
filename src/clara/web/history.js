// The conversations in the rail. The chat page keeps its own list (it knows which conversation is open); on the other
// pages you work in (Projects, Tasks, Files) `mountHistory` shows the same list, so that it never disappears: a click
// goes to the chat on that conversation. Both draw it with `renderGroups`.

import { api, conversationPath } from "./api.js";
import { splitMessage } from "./documents.js";
import { icon } from "./icons.js";
import { chooseProject, listProjects } from "./projects.js";
import { clear, confirmDialog, h, parseDate, popupMenu, promptDialog, toast, toggleRail } from "./ui.js";

const ORDER = ["Pinned", "Today", "Yesterday", "Previous 7 days", "Older"];

export function groupOf(info) {
  if (info.pinned) return "Pinned";
  const date = parseDate(info.updated_at);
  if (!date) return "Older";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.floor((today - new Date(date.getFullYear(), date.getMonth(), date.getDate())) / 86400000);
  return days <= 0 ? "Today" : days === 1 ? "Yesterday" : days < 7 ? "Previous 7 days" : "Older";
}

export const labelOf = (info) => info.title || (info.preview ? splitMessage(info.preview).text || info.preview : "") || "New chat";

// Which groups the person folded, kept in this browser (a group is "d:<date group>" or "p:<project id>").
const FOLDED = "clara.rail.folded";
const folded = (() => {
  try { return new Set(JSON.parse(localStorage.getItem(FOLDED) || "[]")); } catch { return new Set(); }
})();
const keepFolded = () => {
  try { localStorage.setItem(FOLDED, JSON.stringify([...folded])); } catch { /* private window: it is forgotten */ }
};

/** A group that folds: its title (a button; for a project, the name stays a link to it) and its conversations. A search shows everything. */
function group(key, label, items, row, query, link = null) {
  const open = Boolean(query) || !folded.has(key);
  const body = h("div", { class: "group-items", hidden: !open }, items.map(row));
  const toggle = h("button", { type: "button", class: "ghost group-toggle", "aria-expanded": String(open), title: open ? "Fold" : "Unfold",
    onclick: () => {
      const show = body.hidden;
      body.hidden = !show;
      toggle.setAttribute("aria-expanded", String(show));
      toggle.title = show ? "Fold" : "Unfold";
      if (show) folded.delete(key); else folded.add(key);
      keepFolded();
    } }, icon("chevron", { size: 14, className: "caret" }), !link && h("span", { class: "name" }, label));
  return h("section", { class: "group" },
    h("div", { class: "group-title" + (link ? " project-title" : "") }, toggle, link, h("span", { class: "count" }, items.length)), body);
}

/**
 * Draw `list` in `box`: what is pinned first, then each project in a group of its own, named after it, newest first,
 * then the conversations that belong to no project by date. Every group folds. `row(info, inProject)` makes the line
 * of one conversation.
 */
export function renderGroups(box, list, projects, row, query = "") {
  clear(box);
  if (!list.length) {
    box.append(h("p", { class: "empty-note" }, query ? "No conversation matches." : "Your conversations will appear here."));
    return;
  }
  const groups = new Map(ORDER.map((name) => [name, []]));
  const byProject = new Map();
  for (const info of list) {
    if (info.project && !info.pinned) {
      if (!byProject.has(info.project)) byProject.set(info.project, []);
      byProject.get(info.project).push(info);
    } else groups.get(groupOf(info)).push(info);
  }
  const dates = ([name, items]) => items.length && box.append(group(`d:${name}`, name, items, (info) => row(info, false), query));
  const [pinned, ...others] = groups;
  dates(pinned);
  const time = (info) => parseDate(info.updated_at)?.getTime() || 0;
  const newest = (items) => Math.max(...items.map(time));
  for (const [id, items] of [...byProject].sort((a, b) => newest(b[1]) - newest(a[1]))) {
    items.sort((a, b) => time(b) - time(a));
    const link = h("a", { href: `#/projects/${id}`, title: "Open the project" }, icon("folder", { size: 14 }), h("span", {}, projects.get(id) || "Project"));
    box.append(group(`p:${id}`, projects.get(id) || "Project", items, (info) => row(info, true), query, link));
  }
  others.forEach(dates);
}

/** The list for a page that is not the chat. Returns `{destroy}`. */
export function mountHistory(slot, user) {
  const who = { surface: "web", user_id: user.name };
  const state = { list: [], query: "", projects: new Map() };
  const search = h("input", { type: "search", placeholder: "Search conversations", "aria-label": "Search conversations" });
  const box = h("div", { class: "convos", role: "list", "aria-label": "Conversations" });
  const parts = [h("div", { class: "convo-search" }, icon("search", { size: 16 }), search), box];
  slot.append(...parts);
  let timer = null;
  let gone = false;

  const safely = async (action) => {
    try { await action(); } catch (error) { if (error.status !== 401) toast(error.detail || String(error), true); }
  };

  async function load() {
    try {
      const [found, projects] = await Promise.all([
        api.get("/v1/conversations", { ...who, q: state.query }),
        listProjects(user).catch(() => []),
      ]);
      if (gone) return;
      state.list = found.conversations;
      state.projects = new Map(projects.map((item) => [item.id, item.name]));
    } catch (error) {
      if (error.status !== 401 && !gone) toast(error.detail || String(error), true);
      return;
    }
    draw();
  }

  const open = (id) => { toggleRail(false); location.hash = `#/chat/open/${encodeURIComponent(id)}`; };

  function actionsFor(info) {
    return [
      { label: "Rename", icon: "edit", run: async () => {
        const value = await promptDialog("Rename conversation", "Title", info.title || "", "Rename", { hint: "Leave it empty to let Clara choose a title." });
        if (value === null) return;
        await safely(async () => {
          await api.patch(conversationPath(info.id), { ...who, title: value.trim() });
          if (!value.trim()) await api.post(conversationPath(info.id) + "/title", who).catch(() => {});
          await load();
        });
      } },
      { label: info.pinned ? "Unpin" : "Pin to the top", icon: "pin", run: () => safely(async () => {
        await api.patch(conversationPath(info.id), { ...who, pinned: !info.pinned });
        await load();
      }) },
      { label: info.project ? "Move to another project" : "Move to a project", icon: "folder", run: async () => {
        const target = await chooseProject(user, info.project);
        if (target === null || target === (info.project || 0)) return;
        await safely(async () => {
          await api.patch(conversationPath(info.id), { ...who, project: target || null });
          await load();
        });
      } },
      "-",
      { label: "Delete", icon: "trash", danger: true, run: async () => {
        if (!await confirmDialog("Delete conversation", `Delete “${info.title || "this conversation"}”? What Clara knows about you is kept.`, "Delete", true)) return;
        await safely(async () => { await api.delete(conversationPath(info.id), who); await load(); });
      } },
    ];
  }

  function row(info, inProject) {
    const label = labelOf(info);
    const more = h("button", { class: "ghost icon-btn more", "aria-label": `Actions for ${label}`, title: "More",
      onclick: (event) => { event.stopPropagation(); popupMenu(more, actionsFor(info)); } }, icon("more", { size: 18 }));
    return h("div", {
      class: "convo", role: "listitem", tabindex: 0, onclick: () => open(info.id),
      onkeydown: (event) => { if (event.key === "Enter" && event.target === event.currentTarget) open(info.id); },
    }, info.pinned && h("span", { class: "pin", title: "Pinned" }, icon("pin", { size: 15 })), h("span", { class: "title", title: label }, label),
    info.project && !inProject && h("span", { class: "convo-project", title: `In the project ${state.projects.get(info.project) || ""}` }, icon("folder", { size: 14 })),
    more);
  }

  const draw = () => renderGroups(box, state.list, state.projects, row, state.query);

  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.query = search.value.trim(); load(); }, 250);
  });
  load();
  return { destroy() { gone = true; clearTimeout(timer); for (const node of parts) node.remove(); } };
}
