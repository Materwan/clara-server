// The chat page: conversations in the navigation rail, the conversation, documents, the context meter.

import { ApiError, api, conversationPath, streamChat } from "./api.js";
import { DocumentError, MAX_TOTAL_CHARS, compose, readDocument, splitMessage, totalChars } from "./documents.js";
import { icon, mark, ring } from "./icons.js";
import { renderMarkdown } from "./markdown.js";
import { clear, confirmDialog, h, pageHead, parseDate, popupMenu, promptDialog, randomId, toast, toggleRail } from "./ui.js";

const SURFACE = "web";
const INSTRUCTIONS =
  "You are talking through Clara's web site, in a chat window. Markdown is displayed, but keep answers " +
  "short and conversational. The user can attach files (PDF, code, Markdown, text): their content comes in the " +
  'message, each inside <document name="..." type="..."> tags. Refer to them by name.';

function greeting() {
  const hour = new Date().getHours();
  return hour < 5 ? "Hello" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

export function mountChat(container, user, { slot, fresh = false } = {}) {
  const who = { surface: SURFACE, user_id: user.name };
  const lastKey = `clara.last.${user.name}`;
  const displayName = user.person?.name || user.name;
  const state = {
    list: [], query: "", current: null, messages: [], summary: "", earlier: false,
    busy: false, abort: null, docs: [], context: null, live: null, stick: true,
  };

  // ---- the conversations, in the rail ----------------------------------------------------------------
  const search = h("input", { type: "search", placeholder: "Search conversations", "aria-label": "Search conversations" });
  const listBox = h("div", { class: "convos", role: "list", "aria-label": "Conversations" });
  const railPart = [h("div", { class: "convo-search" }, icon("search", { size: 16 }), search), listBox];
  slot?.append(...railPart);

  // ---- the page ---------------------------------------------------------------------------------------
  const title = h("h1", { class: "title grow" }, "New chat");
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
  const composer = h("div", { class: "composer" },
    h("div", { class: "composer-inner" }, chips, input, h("div", { class: "composer-bar" }, attach, picker, docInfo, sendButton)),
    h("p", { class: "composer-hint" }, "Enter to send, Shift + Enter for a new line. Drop files here to attach them."));
  const root = h("section", { class: "page chat" },
    pageHead(title, context, compactButton, moreButton, newButton), messagesBox, composer);
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
    const order = ["Pinned", "Today", "Yesterday", "Previous 7 days", "Older"];
    const groups = new Map(order.map((name) => [name, []]));
    for (const info of state.list) groups.get(groupOf(info)).push(info);
    for (const [name, items] of groups) {
      if (!items.length) continue;
      listBox.append(h("div", { class: "group-title" }, name));
      for (const info of items) listBox.append(convoRow(info));
    }
  }

  function actionsFor(info) {
    return [
      { label: "Rename", icon: "edit", run: () => rename(info) },
      { label: info.pinned ? "Unpin" : "Pin to the top", icon: "pin", run: () => pin(info) },
      "-",
      { label: "Delete", icon: "trash", danger: true, run: () => remove(info) },
    ];
  }

  function convoRow(info) {
    const label = labelOf(info);
    const more = h("button", { class: "ghost icon-btn more", "aria-label": `Actions for ${label}`, title: "More",
      onclick: (event) => { event.stopPropagation(); popupMenu(more, actionsFor(info)); } }, icon("more", { size: 18 }));
    const active = info.id === state.current;
    return h("div", {
      class: "convo" + (active ? " active" : ""), role: "listitem", tabindex: 0, "aria-current": active ? "true" : null,
      onclick: () => { open(info.id); toggleRail(false); },
      onkeydown: (event) => { if (event.key === "Enter" && event.target === event.currentTarget) { open(info.id); toggleRail(false); } },
    }, info.pinned && h("span", { class: "pin", title: "Pinned" }, icon("pin", { size: 15 })), h("span", { class: "title", title: label }, label), more);
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
  }

  function newChat() {
    if (state.busy) return toast("Wait for Clara to finish, or stop the answer first.");
    state.current = `${SURFACE}:${user.name}:${randomId()}`;
    state.messages = [];
    state.summary = "";
    state.earlier = false;
    state.context = null;
    state.live = null;
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
      state.messages = body.messages.map((m) => ({ role: m.role, content: m.content }));
      state.summary = body.summary || "";
      state.earlier = Boolean(body.earlier);
    } catch (error) {
      if (error.status === 404 || error.status === 403) {
        state.messages = []; state.summary = ""; state.earlier = false;
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

  function assistantNode(message) {
    const body = h("div", { class: "body" },
      message.content ? renderMarkdown(message.content) : null,
      message.failed && h("div", { class: "failed-note", role: "alert" }, icon("bolt", { size: 17 }), h("span", {}, message.failed)));
    return h("div", { class: "msg assistant" + (message.failed ? " failed" : "") }, h("div", { class: "who" }, mark(26)), body);
  }

  function messageNode(message) {
    if (message.role === "user") {
      const { text, names } = splitMessage(message.content);
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
      messagesInner.append(h("div", { class: "welcome" },
        h("div", { class: "halo" }, mark(52)),
        h("h1", {}, `${greeting()}, ${displayName.charAt(0).toUpperCase()}${displayName.slice(1)}`),
        h("p", {}, "What would you like to talk about?")));
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

  async function send() {
    if (state.busy) { state.abort?.abort(); return; }
    const text = input.value;
    if (!text.trim() && !state.docs.length) return;
    const message = compose(text, state.docs);
    const conversation = state.current;
    input.value = "";
    autosize();
    state.docs = [];
    renderChips();
    state.messages.push({ role: "user", content: message });
    const reply = { role: "assistant", content: "" };
    state.messages.push(reply);
    state.live = { reply, node: null };
    renderMessages();
    const node = messagesInner.lastElementChild;
    node.classList.add("live");
    const body = node.querySelector(".body");
    body.append(h("span", { class: "typing-dots", "aria-label": "Clara is writing" }, h("i", {}), h("i", {}), h("i", {})));
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
      };
      for await (const event of streamChat(request, state.abort.signal)) {
        if (event.type === "token") {
          reply.content += event.text;
          if (!frame) frame = requestAnimationFrame(paint);
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
    if (finished && !reply.failed) titleIfNeeded(conversation);
  }

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

  // ---- start -------------------------------------------------------------------------------------------
  (async () => {
    await loadList();
    if (fresh) return newChat();
    let last = null;
    try { last = localStorage.getItem(lastKey); } catch { /* private mode */ }
    const start = state.list.find((info) => info.id === last) || state.list[0];
    if (start) await open(start.id);
    else newChat();
  })();

  return {
    newChat,
    destroy() { state.abort?.abort(); clearTimeout(searchTimer); root.remove(); for (const node of railPart) node.remove(); },
  };
}
