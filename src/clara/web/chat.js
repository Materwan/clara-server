// The chat page: conversations in the navigation rail, the conversation, documents, the context meter. The
// conversations of projects are grouped under their project's name in the rail.

import { ApiError, api, conversationPath, streamChat } from "./api.js";
import { approvalCard, pendingApprovals } from "./approvals.js";
import { DocumentError, MAX_TOTAL_CHARS, compose, readDocument, splitMessage, totalChars } from "./documents.js";
import { icon, mark, ring } from "./icons.js";
import { labelOf, renderGroups } from "./history.js";
import { connectionCount, openConnections } from "./integrations.js";
import { chooseModel, costText, loadModels, modelSelect } from "./models.js";
import { displayAnswers } from "./qcm.js";
import { addCall, addFile, addForm, addText, endCall, fileNamesIn, messagesFrom, newReply, replyBody, toolNote } from "./reply.js";
import { chooseProject, listProjects } from "./projects.js";
import { clear, confirmDialog, h, pageHead, popupMenu, promptDialog, randomId, toast, toggleRail } from "./ui.js";

const SURFACE = "web";
const INSTRUCTIONS =
  "You are talking through Clara's web site, in a chat window. Markdown is displayed, but keep answers " +
  "short and conversational. Write mathematical formulas in LaTeX: $...$ inline and $$...$$ on their own lines " +
  "(they are typeset). The user can attach files (PDF, code, Markdown, text): their content comes in the " +
  'message, each inside <document name="..." type="..."> tags. Refer to them by name. To quiz the user or to ' +
  "collect several answers at once, call the qcm tool: the page shows it as a form (radio buttons, check boxes " +
  "or a text box) and their answers come back in their next message.";

function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? "Hello" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

// What was typed and not sent, per conversation (a conversation not started yet has its own id too): the text is kept in
// this browser, so that it survives a change of page and a reload; the documents are too big to keep, they last until
// the page is reloaded.
const DRAFTS = "clara.drafts.";
const DRAFT_DAYS = 30;
const MAX_DRAFTS = 30;
const keptDocs = new Map(); // "<user>|<conversation>" -> the documents of the draft

export function mountChat(container, user, { slot, fresh = false, project = null, open: openId = null } = {}) {
  const who = { surface: SURFACE, user_id: user.name };
  const lastKey = `clara.last.${user.name}`;
  const displayName = user.person?.name || user.name;
  const state = {
    list: [], query: "", current: null, messages: [], summary: "", earlier: false,
    busy: false, abort: null, docs: [], context: null, live: null, stick: true,
    stamp: null, // when the conversation shown was last written in, as we read it: another device may have gone on
    project: null, projects: new Map(), pinned: null, // the project of the conversation shown; every project's name, by id; the ids of those pinned
    approvals: [], // the requests for permission of the conversation shown that wait for an answer
    connections: 0, // how many resources are attached to it (or its project)
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
  const connectCount = h("span", { class: "count", hidden: true });
  const connectButton = h("button", { class: "ghost sm chat-connect", title: "What Clara can reach in this conversation: GitHub, Google Drive, folders",
    onclick: () => connections() }, icon("plug", { size: 16 }), h("span", { class: "hide-sm" }, "Connections"), connectCount);

  const messagesInner = h("div", { class: "messages-inner" });
  const approvalsBox = h("div", { class: "messages-inner approvals-stack", "aria-label": "Waiting for your permission" });
  const messagesBox = h("div", { class: "messages", role: "log", "aria-live": "polite", "aria-label": "Conversation" }, messagesInner, approvalsBox);
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
    pageHead(h("div", { class: "grow chat-title" }, title, projectChip), context, connectButton, compactButton, moreButton, newButton), messagesBox, composer);
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
      const found = await listProjects(user);
      state.projects = new Map(found.map((item) => [item.id, item.name]));
      state.pinned = new Set(found.filter((item) => item.pinned).map((item) => item.id));
    } catch { /* the chips just show no name */ }
  }

  const renderList = () => renderGroups(listBox, state.list, state.projects, convoRow, state.query, state.pinned);

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

  // ---- connections and requests for permission --------------------------------------------------------
  async function connections() {
    if (!state.current) return;
    await openConnections(user, state.current, state.project ? state.projects.get(state.project) : "");
    countConnections();
  }

  async function countConnections() {
    const id = state.current;
    const count = await connectionCount(user, id);
    if (id !== state.current) return;
    state.connections = count;
    connectCount.hidden = !count;
    connectCount.textContent = String(count);
  }

  function drawApprovals() {
    clear(approvalsBox);
    for (const item of state.approvals) {
      approvalsBox.append(approvalCard(item, user, { onDecided: () => { dropApproval(item.id); watchFollowUp(); } }));
    }
    scrollDown();
  }

  function dropApproval(id) {
    state.approvals = state.approvals.filter((a) => a.id !== id);
    drawApprovals();
  }

  /** What waits for an answer in the conversation shown (the stream tells of the new ones; this finds the others). */
  async function loadApprovals() {
    const id = state.current;
    if (!id || document.hidden) return;
    let found;
    try { found = await pendingApprovals(user, id); } catch { return; }
    if (id !== state.current) return;
    const key = (list) => list.map((a) => a.id).join(",");
    if (key(found) === key(state.approvals)) return;
    state.approvals = found;
    drawApprovals();
  }

  // Once a request was answered, Clara goes on by herself (a follow-up turn on the server): read the conversation again
  // until it moves, for a minute at most.
  let followUp = null;
  function watchFollowUp() {
    clearInterval(followUp);
    const until = Date.now() + 60000;
    followUp = setInterval(async () => {
      if (Date.now() > until) return clearInterval(followUp);
      const before = state.stamp;
      await refresh();
      if (state.stamp !== before) clearInterval(followUp);
    }, 3000);
  }
  const approvalTimer = setInterval(loadApprovals, 8000);

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

  // ---- what was typed and not sent ---------------------------------------------------------------------
  const draftsKey = DRAFTS + user.name;
  const docsKey = (id) => `${user.name}|${id}`;
  function readDrafts() {
    try { return JSON.parse(localStorage.getItem(draftsKey) || "{}"); } catch { return {}; }
  }
  const hasDraft = (id) => Boolean(readDrafts()[id]) || keptDocs.has(docsKey(id));

  /** Keep the box (and its documents) as the draft of the conversation shown; an empty box leaves no draft. */
  function saveDraft() {
    if (!state.current) return;
    const drafts = readDrafts();
    if (input.value.trim()) drafts[state.current] = { text: input.value, project: state.project, at: Date.now() };
    else delete drafts[state.current];
    const recent = Object.entries(drafts).filter(([, draft]) => Date.now() - draft.at < DRAFT_DAYS * 86400000)
      .sort((a, b) => b[1].at - a[1].at).slice(0, MAX_DRAFTS);
    try { localStorage.setItem(draftsKey, JSON.stringify(Object.fromEntries(recent))); } catch { /* private mode: not kept */ }
    if (state.docs.length) keptDocs.set(docsKey(state.current), state.docs); else keptDocs.delete(docsKey(state.current));
  }

  /** Put the draft of the conversation shown in the box (the box is emptied if there is none). */
  function restoreDraft() {
    input.value = readDrafts()[state.current]?.text || "";
    state.docs = keptDocs.get(docsKey(state.current)) || [];
    autosize();
    renderChips();
  }

  let draftTimer = null;

  /** A conversation to show: its draft replaces the one of the conversation left. */
  function switchTo(id) {
    if (id === state.current) return;
    saveDraft();
    state.current = id;
    restoreDraft();
  }

  /** A new conversation (in a project, maybe); `again` is one not started yet that has a draft: it is shown instead. */
  function newChat(inProject = null, again = null) {
    if (state.busy) return toast("Wait for Clara to finish, or stop the answer first.");
    if (!again && state.current && !state.messages.length && !state.list.some((info) => info.id === state.current) && state.project === inProject) {
      return input.focus(); // this one has not been started: it is the new chat
    }
    switchTo(again || `${SURFACE}:${user.name}:${randomId()}`); // saves the draft of the one left, with its project
    state.project = inProject;
    state.messages = [];
    state.summary = "";
    state.earlier = false;
    state.context = null;
    state.live = null;
    state.stamp = null;
    state.approvals = [];
    remember();
    renderAll();
    countConnections();
    input.focus();
  }

  function remember() {
    try { localStorage.setItem(lastKey, state.current); } catch { /* private mode */ }
  }

  async function open(id) {
    if (state.busy && id !== state.current) return toast("Wait for Clara to finish, or stop the answer first.");
    switchTo(id);
    state.live = null;
    remember();
    try {
      const body = await api.get(conversationPath(id) + "/messages", { ...who, calls: true });
      state.messages = messagesFrom(body.messages, await filesNamed(fileNamesIn(body.messages)));
      state.summary = body.summary || "";
      state.earlier = Boolean(body.earlier);
      state.project = body.project || null;
      state.stamp = body.updated_at || null;
    } catch (error) {
      if (error.status === 404 || error.status === 403) {
        state.messages = []; state.summary = ""; state.earlier = false; state.stamp = null;
        state.project = readDrafts()[id]?.project || null; // a conversation not started yet remembers its project
      } else {
        return toast(error.detail || String(error), true);
      }
    }
    state.context = null;
    state.approvals = [];
    renderAll();
    refreshContext();
    countConnections();
    loadApprovals();
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
    drawApprovals();
  }

  /** A QCM answered here is sent as the next message, unless Clara is still writing. */
  function submitAnswers(text) {
    if (state.busy) { toast("Wait for Clara to finish, then send your answers."); return false; }
    send(text);
    return true;
  }

  const views = new WeakMap(); // the reply -> what draws it, for the one being written

  function assistantNode(message) {
    const view = replyBody(message, who, submitAnswers);
    views.set(message, view);
    view.sync();
    if (message.failed) view.node.append(h("div", { class: "failed-note", role: "alert" }, icon("bolt", { size: 17 }), h("span", {}, message.failed)));
    return h("div", { class: "msg assistant" + (message.failed ? " failed" : "") }, h("div", { class: "who" }, mark(30)), view.node);
  }

  /** The files of Clara that a conversation's history names, by name (those deleted since have no card). */
  async function filesNamed(names) {
    if (!names.length) return new Map();
    try {
      return new Map((await api.get("/v1/markdown-files", who)).files.map((file) => [file.name, file]));
    } catch {
      return new Map();
    }
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
      saveDraft(); // nothing is left to keep
    }
    state.messages.push({ role: "user", content: message });
    const reply = newReply();
    reply.live = true; // what draws it shows the dots, the caret and the spinners
    state.messages.push(reply);
    state.live = { reply, node: null };
    renderMessages();
    messagesInner.lastElementChild.classList.add("live");
    state.stick = true;
    scrollDown(true);
    state.abort = new AbortController();
    setBusy(true);
    let frame = 0;
    const paint = () => {
      frame = 0;
      views.get(reply)?.sync();
      scrollDown();
    };
    const draw = () => { if (!frame) frame = requestAnimationFrame(paint); };
    let finished = false;
    try {
      const request = {
        ...who, user_name: displayName, message, conversation, instructions: INSTRUCTIONS,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        project: state.project || undefined, // a new conversation goes in it; one that exists stays where it is
      };
      for await (const event of streamChat(request, state.abort.signal)) {
        if (event.type === "token") {
          addText(reply, event.text);
          draw();
        } else if (event.type === "qcm") {
          addForm(reply, { ...event.form, answers: null });
          draw();
        } else if (event.type === "markdown_file") {
          addFile(reply, { ...event.file, action: event.action });
          draw();
        } else if (event.type === "tool_start" || event.type === "tool") {
          const note = toolNote(event);
          if (note) {
            if (event.type === "tool_start") addCall(reply, note, "running");
            else endCall(reply, note);
            draw();
          }
        } else if (event.type === "approval") {
          if (!state.approvals.some((a) => a.id === event.approval.id)) state.approvals.push(event.approval);
          if (conversation === state.current) drawApprovals();
        } else if (event.type === "compacted") toast("Older messages were summarised to make room.");
        else if (event.type === "warning" || event.type === "retrying") toast(event.message);
        else if (event.type === "error") { reply.failed = event.message; finished = true; }
        else if (event.type === "done") {
          if (event.reply && !reply.content) addText(reply, event.reply);
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
    reply.live = false;
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
  input.addEventListener("input", () => { clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 300); });
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
    let last = null;
    try { last = localStorage.getItem(lastKey); } catch { /* private mode */ }
    // the last conversation shown, if it was not started yet and something was typed in it
    const unsent = last && !state.list.some((info) => info.id === last) && hasDraft(last) ? last : null;
    if (fresh) return newChat(project, unsent && (readDrafts()[unsent]?.project || null) === project ? unsent : null);
    if (openId) return open(openId);
    const start = state.list.find((info) => info.id === last)?.id || unsent || state.list[0]?.id;
    if (start) await open(start);
    else newChat();
  })();

  return {
    newChat: () => newChat(),
    destroy() {
      clearTimeout(draftTimer); saveDraft();
      state.abort?.abort(); clearTimeout(searchTimer); clearInterval(refreshTimer); clearInterval(approvalTimer); clearInterval(followUp);
      document.removeEventListener("visibilitychange", refresh); removeEventListener("focus", refresh);
      root.remove(); for (const node of railPart) node.remove();
    },
  };
}
