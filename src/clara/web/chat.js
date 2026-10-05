// The chat page: conversations in the navigation rail, the conversation, documents, the context meter. The
// conversations of projects are not in the rail (they are on their project's page) unless a search finds them.

import { ApiError, api, conversationPath, streamChat } from "./api.js";
import { DocumentError, MAX_TOTAL_CHARS, compose, readDocument, splitMessage, totalChars } from "./documents.js";
import { icon, mark, ring } from "./icons.js";
import { renderMarkdown } from "./markdown.js";
import { fileCard } from "./files.js";
import { chooseModel, costText, loadModels, modelSelect } from "./models.js";
import { displayAnswers, qcmNode } from "./qcm.js";
import { chooseProject, listProjects } from "./projects.js";
import { clear, confirmDialog, h, pageHead, parseDate, popupMenu, promptDialog, randomId, toast, toggleRail } from "./ui.js";

const SURFACE = "web";
const INSTRUCTIONS =
  "You are talking through Clara's web site, in a chat window. Markdown is displayed, but keep answers " +
  "short and conversational. Write mathematical formulas in LaTeX: $...$ inline and $$...$$ on their own lines " +
  "(they are typeset). The user can attach files (PDF, code, Markdown, text): their content comes in the " +
  'message, each inside <document name="..." type="..."> tags. Refer to them by name. To quiz the user or to ' +
  "collect several answers at once, call the qcm tool: the page shows it as a form (radio buttons, check boxes " +
  "or a text box) and their answers come back in their next message.";

// What Clara used to answer, written in the margin of her reply: [icon, what she did, the argument that says what about].
// Only the turns made in this tab have them: the server does not keep tool calls with the messages.
const TOOL_NOTES = {
  remember: ["memory", "Remembered", "fact"],
  forget: ["memory", "Forgot a fact"],
  recall_facts: ["memory", "Looked in memory for", "query"],
  about_person: ["users", "Read what she knows about", "name"],
  web_search: ["globe", "Searched the web for", "query"],
  web_fetch: ["globe", "Read a page", "url"],
  remind: ["bell", "Set a reminder", "text"],
  list_reminders: ["bell", "Checked your reminders"],
  cancel_reminder: ["bell", "Cancelled a reminder"],
  notify: ["bell", "Notified you", "text"],
  add_task: ["tasks", "Added a task", "title"],
  list_tasks: ["tasks", "Read your tasks"],
  update_task: ["tasks", "Updated a task", "title"],
  delete_task: ["tasks", "Deleted a task"],
  create_markdown_file: ["file", "Wrote", "name"],
  edit_markdown_file: ["file", "Edited", "name"],
  append_markdown_file: ["file", "Added to", "name"],
  read_markdown_file: ["file", "Read", "name"],
  list_markdown_files: ["file", "Listed your files"],
  list_project_files: ["folder", "Listed the project's files"],
  read_project_file: ["folder", "Read", "path"],
  search_project: ["folder", "Searched the project for", "query"],
};
const SILENT_TOOLS = new Set(["qcm", "adjust_relation"]); // the form is its own card; the relationship is not shown here

/** The margin note for a `tool` event of the stream, or null for a tool that leaves none. */
function toolNote(event) {
  const name = String(event.name || "");
  if (!name || SILENT_TOOLS.has(name)) return null;
  const [glyph, label, key] = TOOL_NOTES[name] || ["bolt", name.replaceAll("_", " ")];
  let args = event.arguments;
  if (typeof args === "string") {
    try { args = JSON.parse(args); } catch { args = {}; }
  }
  const raw = key && args && typeof args[key] === "string" ? args[key].trim() : "";
  return { glyph, label, detail: raw.length > 80 ? raw.slice(0, 79) + "…" : raw };
}

function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? "Hello" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

export function mountChat(container, user, { slot, fresh = false, project = null, open: openId = null } = {}) {
  const who = { surface: SURFACE, user_id: user.name };
  const lastKey = `clara.last.${user.name}`;
  const displayName = user.person?.name || user.name;
  const state = {
    list: [], query: "", current: null, messages: [], summary: "", earlier: false,
    busy: false, abort: null, docs: [], context: null, live: null, stick: true,
    stamp: null, // when the conversation shown was last written in, as we read it: another device may have gone on
    project: null, projects: new Map(), // the project of the conversation shown; every project's name, by id
  };

  // ---- the conversations, in the rail ----------------------------------------------------------------
  const search = h("input", { type: "search", placeholder: "Search conversations", "aria-label": "Search conversations" });
  const listBox = h("div", { class: "convos", role: "list", "aria-label": "Conversations" });
  const railPart = [h("div", { class: "convo-search" }, icon("search", { size: 16 }), search), listBox];
  slot?.append(...railPart);

  // ---- the page ---------------------------------------------------------------------------------------
  const title = h("h1", { class: "title grow" }, "New chat");
  const projectChip = h("a", { class: "project-chip", hidden: true, title: "Open the project" }, icon("folder", { size: 15 }), h("span", {}));
  const meter = ring(18);
  const percentText = h("span", {}, "0%");
  const context = h("div", { class: "context", role: "img", "aria-label": "Context used", hidden: true },
    meter, percentText, h("span", { class: "label" }, "of context"));
  const compactButton = h("button", { class: "ghost sm hide-sm", onclick: compact, title: "Replace the older messages with a summary, to make room", hidden: true },
    icon("compress", { size: 16 }), "Summarise");
  const moreButton = h("button", { class: "ghost icon-btn", "aria-label": "Conversation actions", title: "Conversation actions", hidden: true,
    onclick: (event) => { event.stopPropagation(); conversationMenu(moreButton); } }, icon("more"));
  const newButton = h("button", { class: "ghost icon-btn only-narrow", "aria-label": "New chat", title: "New chat", onclick: () => newChat() }, icon("edit"));

  const messagesInner = h("div", { class: "messages-inner" });
  const messagesBox = h("div", { class: "messages", role: "log", "aria-live": "polite", "aria-label": "Conversation" }, messagesInner);
  const input = h("textarea", { rows: 1, placeholder: `Message Clara`, "aria-label": "Message", enterkeyhint: "send" });
  const chips = h("div", { class: "chips" });
  const picker = h("input", { type: "file", multiple: true, hidden: true, onchange: () => { addFiles([...picker.files]); picker.value = ""; } });
  const attach = h("button", { class: "ghost icon-btn", title: "Attach documents (PDF, code, text)", "aria-label": "Attach documents", onclick: () => picker.click() }, icon("clip"));
  const sendButton = h("button", { class: "send", "aria-label": "Send", title: "Send", onclick: send, disabled: true }, icon("send", { size: 19 }));
  const docInfo = h("span", { class: "grow docinfo" });
  const modelBox = h("span", { class: "model-box", hidden: true }); // the model picker, when an administrator offers a choice
  const composer = h("div", { class: "composer" }, h("div", { class: "composer-frame" },
    h("div", { class: "composer-inner" }, chips, input, h("div", { class: "composer-bar" }, attach, picker, docInfo, modelBox, sendButton)),
    h("p", { class: "composer-hint" }, "Enter to send, Shift + Enter for a new line. Drop files here to attach them.")));
  const root = h("section", { class: "page chat" },
    pageHead(h("div", { class: "grow chat-title" }, title, projectChip), context, compactButton, moreButton, newButton), messagesBox, composer);
  container.append(root);

  // ---- the list of conversations ----------------------------------------------------------------------
  let searchTimer = null;
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.query = search.value.trim(); loadList(); }, 250);
  });

  async function loadList() {
    try {
      state.list = (await api.get("/v1/conversations", { ...who, q: state.query })).conversations;
    } catch (error) {
      if (error.status !== 401) toast(error.detail || String(error), true);
      return;
    }
    renderList();
    renderHead();
  }

  async function loadProjects() {
    try {
      state.projects = new Map((await listProjects(user)).map((item) => [item.id, item.name]));
    } catch { /* the chips just show no name */ }
  }

  function groupOf(info) {
    if (info.pinned) return "Pinned";
    const date = parseDate(info.updated_at);
    if (!date) return "Older";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.floor((today - new Date(date.getFullYear(), date.getMonth(), date.getDate())) / 86400000);
    return days <= 0 ? "Today" : days === 1 ? "Yesterday" : days < 7 ? "Previous 7 days" : "Older";
  }

  const labelOf = (info) => info.title || (info.preview ? splitMessage(info.preview).text || info.preview : "") || "New chat";

  function renderList() {
    clear(listBox);
    if (!state.list.length) {
      listBox.append(h("p", { class: "empty-note" }, state.query ? "No conversation matches." : "Your conversations will appear here."));
      return;
    }
    // the date groups hold what is pinned and what belongs to no project; each project then has a group of its own
    const order = ["Pinned", "Today", "Yesterday", "Previous 7 days", "Older"];
    const groups = new Map(order.map((name) => [name, []]));
    const byProject = new Map();
    for (const info of state.list) {
      if (info.project && !info.pinned) {
        if (!byProject.has(info.project)) byProject.set(info.project, []);
        byProject.get(info.project).push(info);
      } else groups.get(groupOf(info)).push(info);
    }
    for (const [name, items] of groups) {
      if (!items.length) continue;
      listBox.append(h("div", { class: "group-title" }, name));
      for (const info of items) listBox.append(convoRow(info));
    }
    const newest = (items) => Math.max(...items.map((info) => parseDate(info.updated_at)?.getTime() || 0));
    for (const [id, items] of [...byProject].sort((a, b) => newest(b[1]) - newest(a[1]))) {
      items.sort((a, b) => (parseDate(b.updated_at)?.getTime() || 0) - (parseDate(a.updated_at)?.getTime() || 0));
      listBox.append(h("a", { class: "group-title project-title", href: `#/projects/${id}`, title: "Open the project" },
        icon("folder", { size: 14 }), h("span", {}, state.projects.get(id) || "Project")));
      for (const info of items) listBox.append(convoRow(info, true));
    }
  }

  function actionsFor(info) {
    return [
      { label: "Rename", icon: "edit", run: () => rename(info) },
      { label: info.pinned ? "Unpin" : "Pin to the top", icon: "pin", run: () => pin(info) },
      { label: info.project ? "Move to another project" : "Move to a project", icon: "folder", run: () => move(info) },
      "-",
      { label: "Delete", icon: "trash", danger: true, run: () => remove(info) },
    ];
  }

  function convoRow(info, inProject = false) {
    const label = labelOf(info);
    const more = h("button", { class: "ghost icon-btn more", "aria-label": `Actions for ${label}`, title: "More",
      onclick: (event) => { event.stopPropagation(); popupMenu(more, actionsFor(info)); } }, icon("more", { size: 18 }));
    const active = info.id === state.current;
    return h("div", {
      class: "convo" + (active ? " active" : ""), role: "listitem", tabindex: 0, "aria-current": active ? "true" : null,
      onclick: () => { open(info.id); toggleRail(false); },
      onkeydown: (event) => { if (event.key === "Enter" && event.target === event.currentTarget) { open(info.id); toggleRail(false); } },
    }, info.pinned && h("span", { class: "pin", title: "Pinned" }, icon("pin", { size: 15 })), h("span", { class: "title", title: label }, label),
    info.project && !inProject && h("span", { class: "convo-project", title: `In the project ${state.projects.get(info.project) || ""}` }, icon("folder", { size: 14 })),
    more);
  }

  function conversationMenu(anchor) {
    const info = state.list.find((item) => item.id === state.current);
    if (!info) return;
    const items = actionsFor(info);
    if (!compactButton.hidden) items.splice(2, 0, { label: "Summarise older messages", icon: "compress", run: compact });
    popupMenu(anchor, items);
  }

  async function rename(info) {
    const value = await promptDialog("Rename conversation", "Title", info.title || "", "Rename", { hint: "Leave it empty to let Clara choose a title." });
    if (value === null) return;
    await safely(async () => {
      await api.patch(conversationPath(info.id), { ...who, title: value.trim() });
      if (!value.trim()) await api.post(conversationPath(info.id) + "/title", who).catch(() => {});
      await loadList();
    });
  }

  async function move(info) {
    const target = await chooseProject(user, info.project);
    if (target === null || target === (info.project || 0)) return;
    await safely(async () => {
      await api.patch(conversationPath(info.id), { ...who, project: target || null });
      await loadProjects();
      if (info.id === state.current) state.project = target || null;
      await loadList();
      toast(target ? `Moved to ${state.projects.get(target) || "the project"}.` : "Moved out of its project.");
    });
  }

  async function pin(info) {
    await safely(async () => { await api.patch(conversationPath(info.id), { ...who, pinned: !info.pinned }); await loadList(); });
  }

  async function remove(info) {
    const label = info.title || "this conversation";
    if (!await confirmDialog("Delete conversation", `Delete “${label}”? What Clara knows about you is kept.`, "Delete", true)) return;
    await safely(async () => {
      await api.delete(conversationPath(info.id), who);
      if (info.id === state.current) newChat();
      await loadList();
    });
  }

  async function safely(action) {
    try {
      await action();
    } catch (error) {
      if (error.status !== 401) toast(error.detail || String(error), true);
    }
  }

  // ---- the conversation ------------------------------------------------------------------------------
  function renderHead() {
    const info = state.list.find((item) => item.id === state.current);
    title.textContent = info ? labelOf(info) : "New chat";
    moreButton.hidden = !info;
    if (info) state.project = info.project || null;
    projectChip.hidden = !state.project;
    if (state.project) {
      projectChip.href = `#/projects/${state.project}`;
      projectChip.lastChild.textContent = state.projects.get(state.project) || "Project";
    }
  }

  function newChat(inProject = null) {
    if (state.busy) return toast("Wait for Clara to finish, or stop the answer first.");
    state.project = inProject;
    state.current = `${SURFACE}:${user.name}:${randomId()}`;
    state.messages = [];
    state.summary = "";
    state.earlier = false;
    state.context = null;
    state.live = null;
    state.stamp = null;
    remember();
    renderAll();
    input.focus();
  }

  function remember() {
    try { localStorage.setItem(lastKey, state.current); } catch { /* private mode */ }
  }

  async function open(id) {
    if (state.busy && id !== state.current) return toast("Wait for Clara to finish, or stop the answer first.");
    state.current = id;
    state.live = null;
    remember();
    try {
      const body = await api.get(conversationPath(id) + "/messages", who);
      state.messages = body.messages.map((m) => ({ role: m.role, content: m.content, qcm: m.qcm }));
      state.summary = body.summary || "";
      state.earlier = Boolean(body.earlier);
      state.project = body.project || null;
      state.stamp = body.updated_at || null;
    } catch (error) {
      if (error.status === 404 || error.status === 403) {
        state.messages = []; state.summary = ""; state.earlier = false; state.project = null; state.stamp = null;
      } else {
        return toast(error.detail || String(error), true);
      }
    }
    state.context = null;
    renderAll();
    refreshContext();
  }

  async function refreshContext() {
    const id = state.current;
    try {
      const info = await api.get(conversationPath(id));
      if (id === state.current) { state.context = info; renderMeter(); }
    } catch { /* a conversation that does not exist yet has no context */ }
  }

  function renderMeter() {
    const info = state.context;
    const percent = info ? Math.min(100, info.percent || 0) : 0;
    meter.set(percent);
    percentText.textContent = `${percent.toFixed(0)}%`;
    context.hidden = !info || !state.messages.length;
    context.classList.toggle("hot", percent >= 80);
    const text = info ? `Context ${percent.toFixed(0)}% full: ${(info.tokens || 0).toLocaleString()} of ${(info.window || 0).toLocaleString()} tokens` : "Context";
    context.title = text;
    context.setAttribute("aria-label", text);
    compactButton.hidden = !info || !(info.messages > 2);
  }

  function renderAll() {
    renderList();
    renderHead();
    renderMessages();
    renderMeter();
  }

  /** A QCM answered here is sent as the next message, unless Clara is still writing. */
  function submitAnswers(text) {
    if (state.busy) { toast("Wait for Clara to finish, then send your answers."); return false; }
    send(text);
    return true;
  }

  const cardsOf = (message) => [
    ...(message.qcm || []).map((form) => qcmNode(form, { submit: submitAnswers })),
    ...(message.files || []).map((file) => fileCard(file, who)),
  ];

  const noteNode = (note) => h("div", { class: "note" }, icon(note.glyph, { size: 14 }),
    h("span", {}, h("b", {}, note.label), note.detail ? ` ${note.detail}` : ""));

  function assistantNode(message) {
    const body = h("div", { class: "body" },
      h("div", { class: "text" }, message.content ? renderMarkdown(message.content) : null),
      h("div", { class: "cards" }, cardsOf(message)),
      message.failed && h("div", { class: "failed-note", role: "alert" }, icon("bolt", { size: 17 }), h("span", {}, message.failed)));
    body.append(h("div", { class: "notes", "aria-label": "What Clara used" }, (message.notes || []).map(noteNode)));
    return h("div", { class: "msg assistant" + (message.failed ? " failed" : "") }, h("div", { class: "who" }, mark(30)), body);
  }

  function messageNode(message) {
    if (message.role === "user") {
      const { text, names } = splitMessage(displayAnswers(message.content));
      return h("div", { class: "msg user" }, h("div", { class: "body" },
        text && h("div", { class: "bubble" }, text),
        names.length > 0 && h("div", { class: "chips" }, names.map((name) => h("span", { class: "chip" }, icon("file", { size: 15 }), h("span", { class: "name" }, name))))));
    }
    return assistantNode(message);
  }

  function renderMessages() {
    clear(messagesInner);
    if (state.summary) messagesInner.append(h("div", { class: "summary" }, h("strong", {}, "Earlier in this conversation: "), state.summary));
    else if (state.earlier) messagesInner.append(h("div", { class: "summary" }, "Older messages are not shown."));
    if (!state.messages.length && !state.live) {
      const projectName = state.project && (state.projects.get(state.project) || "this project");
      messagesInner.append(h("div", { class: "welcome" },
        h("div", { class: "halo" }, mark(52)),
        h("h1", {}, `${greeting()}, ${displayName.charAt(0).toUpperCase()}${displayName.slice(1)}`),
        h("p", {}, projectName ? `A new chat in ${projectName}: Clara can use its files and instructions.` : "What would you like to talk about?")));
    }
    for (const message of state.messages) messagesInner.append(messageNode(message));
    scrollDown(true);
  }

  function scrollDown(force) {
    if (force || state.stick) messagesBox.scrollTop = messagesBox.scrollHeight;
  }
  messagesBox.addEventListener("scroll", () => {
    state.stick = messagesBox.scrollHeight - messagesBox.scrollTop - messagesBox.clientHeight < 80;
  });

  // ---- sending ------------------------------------------------------------------------------------------
  function canSend() {
    return state.busy || Boolean(input.value.trim()) || state.docs.length > 0;
  }

  function setBusy(busy) {
    state.busy = busy;
    clear(sendButton).append(icon(busy ? "stop" : "send", { size: busy ? 18 : 19 }));
    sendButton.classList.toggle("stop", busy);
    sendButton.setAttribute("aria-label", busy ? "Stop the answer" : "Send");
    sendButton.title = busy ? "Stop the answer" : "Send";
    sendButton.disabled = !canSend();
    input.disabled = false;
  }

  /** The caret goes at the very end of the text being written, not on a line of its own. */
  function placeCaret(node) {
    let at = node;
    while (at.lastChild && at.lastChild.nodeType === Node.ELEMENT_NODE && !["PRE", "TABLE", "DIV", "HR", "BR"].includes(at.lastChild.tagName)) at = at.lastChild;
    at.classList.add("caret");
  }

  /** Send what is in the box (with its documents), or, given a text, that text alone (the answers of a QCM). */
  async function send(answers) {
    if (state.busy) { state.abort?.abort(); return; }
    const direct = typeof answers === "string";
    const text = direct ? answers : input.value;
    if (!direct && !text.trim() && !state.docs.length) return;
    const message = direct ? text : compose(text, state.docs);
    const conversation = state.current;
    if (!direct) {
      input.value = "";
      autosize();
      state.docs = [];
      renderChips();
    }
    state.messages.push({ role: "user", content: message });
    const reply = { role: "assistant", content: "" };
    state.messages.push(reply);
    state.live = { reply, node: null };
    renderMessages();
    const node = messagesInner.lastElementChild;
    node.classList.add("live");
    const body = node.querySelector(".text");
    const cards = node.querySelector(".cards");
    const notes = node.querySelector(".notes");
    body.append(h("span", { class: "waiting", role: "img", "aria-label": "Clara is writing" }));
    state.stick = true;
    scrollDown(true);
    state.abort = new AbortController();
    setBusy(true);
    let frame = 0;
    const paint = () => {
      frame = 0;
      const md = renderMarkdown(reply.content);
      placeCaret(md);
      clear(body).append(md);
      scrollDown();
    };
    let finished = false;
    try {
      const request = {
        ...who, user_name: displayName, message, conversation, instructions: INSTRUCTIONS,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        project: state.project || undefined, // a new conversation goes in it; one that exists stays where it is
      };
      for await (const event of streamChat(request, state.abort.signal)) {
        if (event.type === "token") {
          reply.content += event.text;
          if (!frame) frame = requestAnimationFrame(paint);
        } else if (event.type === "qcm") {
          const form = { ...event.form, answers: null };
          (reply.qcm ||= []).push(form);
          cards.append(qcmNode(form, { submit: submitAnswers }));
          if (!reply.content) clear(body); // nothing written before the form: no dots above it
          scrollDown();
        } else if (event.type === "markdown_file") {
          const file = { ...event.file, action: event.action };
          (reply.files ||= []).push(file);
          cards.append(fileCard(file, who));
          if (!reply.content) clear(body); // nothing written before the file: no dots above it
          scrollDown();
        } else if (event.type === "tool") {
          const note = toolNote(event);
          if (note) {
            (reply.notes ||= []).push(note);
            notes.append(noteNode(note));
          }
        } else if (event.type === "compacted") toast("Older messages were summarised to make room.");
        else if (event.type === "warning") toast(event.message);
        else if (event.type === "error") { reply.failed = event.message; finished = true; }
        else if (event.type === "done") {
          if (event.reply) reply.content = event.reply;
          state.context = { ...state.context, ...event.context, messages: (state.context?.messages || 0) + 2 };
          finished = true;
        }
      }
    } catch (error) {
      if (error.name === "AbortError") reply.failed = "Stopped. What was written so far is not kept by Clara.";
      else if (error instanceof ApiError) reply.failed = error.status === 401 ? "" : error.detail;
      else reply.failed = String(error);
    }
    if (!finished && !reply.failed) reply.failed = "The answer was cut off.";
    cancelAnimationFrame(frame);
    state.live = null;
    state.abort = null;
    setBusy(false);
    if (conversation === state.current) {
      renderMessages();
      renderMeter();
    }
    if (matchMedia("(hover: hover)").matches) input.focus(); // a phone would pop its keyboard back up
    await loadList();
    if (conversation === state.current) state.stamp = state.list.find((item) => item.id === conversation)?.updated_at ?? state.stamp;
    if (finished && !reply.failed) titleIfNeeded(conversation);
  }

  // The web site and the app share their conversations: while this page is in front, read the list again now and
  // then, and the conversation shown if it went on elsewhere (never while an answer is being written here).
  const REFRESH = 30000;
  async function refresh() {
    if (document.hidden || state.busy) return;
    const id = state.current;
    await loadList();
    const info = state.list.find((item) => item.id === id);
    if (!info || state.busy || id !== state.current || !state.stamp || info.updated_at === state.stamp) return;
    await open(id);
  }
  const refreshTimer = setInterval(refresh, REFRESH);
  document.addEventListener("visibilitychange", refresh);
  addEventListener("focus", refresh);

  async function titleIfNeeded(id) {
    const info = state.list.find((item) => item.id === id);
    if (!info || info.title) return;
    try {
      await api.post(conversationPath(id) + "/title", who);
      await loadList();
    } catch { /* the preview stands in for a title */ }
  }

  async function compact() {
    if (state.busy) return;
    compactButton.disabled = true;
    try {
      const result = await api.post(conversationPath(state.current) + "/compact", {});
      toast(`Summarised: the context went from ${result.before_percent}% to ${result.after_percent}%.`);
      await open(state.current);
    } catch (error) {
      toast(error.detail || String(error), true);
    } finally {
      compactButton.disabled = false;
    }
  }

  // ---- the input box and documents ---------------------------------------------------------------------
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
    sendButton.disabled = !canSend();
  }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (event) => {
    // on a touch screen Enter makes a new line; the send button sends
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && matchMedia("(hover: hover)").matches) { event.preventDefault(); send(); }
  });
  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (files.length) { event.preventDefault(); addFiles(files); }
  });
  for (const type of ["dragenter", "dragover"]) root.addEventListener(type, (event) => { event.preventDefault(); composer.classList.add("drop"); });
  for (const type of ["dragleave", "drop"]) root.addEventListener(type, (event) => { if (type === "drop" || !root.contains(event.relatedTarget)) composer.classList.remove("drop"); });
  root.addEventListener("drop", (event) => { event.preventDefault(); addFiles([...event.dataTransfer.files]); });

  async function addFiles(files) {
    for (const file of files) {
      if (state.docs.some((doc) => doc.name === file.name)) { toast(`${file.name} is already attached.`); continue; }
      try {
        const doc = await readDocument(file);
        if (totalChars([...state.docs, doc]) > MAX_TOTAL_CHARS) {
          toast(`${file.name} does not fit: a message holds about ${MAX_TOTAL_CHARS.toLocaleString()} characters of documents.`, true);
          continue;
        }
        state.docs.push(doc);
        renderChips();
      } catch (error) {
        toast(error instanceof DocumentError ? error.message : String(error), true);
      }
    }
  }

  function renderChips() {
    clear(chips);
    for (const doc of state.docs) {
      chips.append(h("span", { class: "chip", title: doc.note }, icon("file", { size: 15 }), h("span", { class: "name" }, doc.name),
        h("button", { "aria-label": `Remove ${doc.name}`, title: "Remove", onclick: () => { state.docs = state.docs.filter((d) => d !== doc); renderChips(); } }, icon("close", { size: 14 }))));
    }
    docInfo.textContent = state.docs.length ? `${totalChars(state.docs).toLocaleString()} of ${MAX_TOTAL_CHARS.toLocaleString()} characters` : "";
    sendButton.disabled = !canSend();
  }

  // ---- the model ---------------------------------------------------------------------------------------
  async function loadModelPicker() {
    let info;
    try { info = await loadModels(who); } catch { return; } // no picker: Clara answers with the server's model
    clear(modelBox);
    modelBox.hidden = !info.models.length;
    if (!info.models.length) return;
    const select = modelSelect(info, info.choices[SURFACE] ?? null, async (ref) => {
      select.disabled = true;
      try {
        const done = await chooseModel(who, SURFACE, ref);
        toast(`Clara now answers with ${done.current.name} (${costText(done.current.weight)}).`);
      } catch (error) { toast(error.detail || String(error), true); }
      loadModelPicker();
    }, "Model for this site");
    modelBox.append(select);
  }

  // ---- start -------------------------------------------------------------------------------------------
  loadModelPicker();
  (async () => {
    await Promise.all([loadList(), loadProjects()]);
    renderList(); // the project groups are named by the projects, which may have come after the list
    if (fresh) return newChat(project);
    if (openId) return open(openId);
    let last = null;
    try { last = localStorage.getItem(lastKey); } catch { /* private mode */ }
    const start = state.list.find((info) => info.id === last) || state.list[0];
    if (start) await open(start.id);
    else newChat();
  })();

  return {
    newChat: () => newChat(),
    destroy() {
      state.abort?.abort(); clearTimeout(searchTimer); clearInterval(refreshTimer);
      document.removeEventListener("visibilitychange", refresh); removeEventListener("focus", refresh);
      root.remove(); for (const node of railPart) node.remove();
    },
  };
}
