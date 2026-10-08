// What a reply of Clara is made of, in the order she made it: pieces of text, runs of tool calls (consecutive calls
// are one line, which opens on one line per call) and QCM forms. The files she wrote come after everything else.
// A reply is `{ parts: [{kind: "text" | "run" | "qcm", ...}], files: [], content, live }`; `replyBody` draws it.

import { fileCard } from "./files.js";
import { icon } from "./icons.js";
import { renderMarkdown } from "./markdown.js";
import { qcmNode } from "./qcm.js";
import { clear, h, toast } from "./ui.js";

// What Clara did, in words: [icon, what she did, the argument that says what about].
const TOOL_NOTES = {
  remember: ["memory", "Remembered", "fact"],
  forget: ["memory", "Forgot a fact"],
  recall_facts: ["memory", "Looked in memory for", "query"],
  about_person: ["users", "Looked up", "name"],
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
  resources: ["plug", "Looked at what is connected"],
  res_list: ["plug", "Listed", "path"],
  res_read: ["plug", "Read", "path"],
  res_search: ["plug", "Searched for", "query"],
  res_write: ["plug", "Wrote", "path"],
  res_delete: ["plug", "Asked to delete", "path"],
  res_move: ["plug", "Asked to move", "path"],
  github_branch: ["branch", "Worked on branches", "name"],
  github_pr: ["branch", "Worked on a pull request", "title"],
  github_issue: ["branch", "Worked on an issue", "title"],
};
const SILENT_TOOLS = new Set(["qcm", "adjust_relation"]); // the form is its own card; the relationship is not shown here
const FILE_TOOLS = { create_markdown_file: "created", edit_markdown_file: "updated", append_markdown_file: "updated" };

/** The line for a call (a `tool` / `tool_start` event, or a call of the history), or null for a tool that leaves none. */
export function toolNote(event) {
  const name = String(event.name || "");
  if (!name || SILENT_TOOLS.has(name)) return null;
  const [glyph, label, key] = TOOL_NOTES[name] || ["bolt", name.replaceAll("_", " ")];
  let args = event.arguments;
  if (typeof args === "string") {
    try { args = JSON.parse(args); } catch { args = {}; }
  }
  if (!args || typeof args !== "object" || Array.isArray(args)) args = {};
  const raw = key && typeof args[key] === "string" ? args[key].trim() : "";
  // `args` and `result` are what a click on the call shows: what the tool was given, and what it gave back
  // (undefined while it runs, or when the conversation was stored before results were kept)
  return {
    name, glyph, label, detail: raw.length > 80 ? raw.slice(0, 79) + "…" : raw,
    args, result: typeof event.result === "string" ? event.result : undefined, truncated: event.truncated === true,
  };
}

// ---- the reply as data ------------------------------------------------------------------------------------
export const newReply = () => ({ role: "assistant", content: "", parts: [], files: [] });
const last = (reply) => reply.parts[reply.parts.length - 1];

export function addText(reply, text) {
  let part = last(reply);
  if (part?.kind !== "text") reply.parts.push(part = { kind: "text", text: "" });
  part.text += text;
  reply.content += text;
}

/** A call of a tool joins the run just before it (calls with no text between them are one run). */
export function addCall(reply, note, state) {
  let part = last(reply);
  if (part?.kind !== "run") reply.parts.push(part = { kind: "run", calls: [], open: false });
  part.calls.push({ ...note, state });
}

/** A call ended: it is the first one still running with that name (or a new one, for a server that did not announce
 * it). Several calls of a round may run at once, and end after something else was shown (a QCM): every run is looked in. */
export function endCall(reply, note) {
  const call = reply.parts
    .filter((part) => part.kind === "run")
    .flatMap((part) => part.calls)
    .find((c) => c.state === "running" && c.name === note.name);
  if (call) Object.assign(call, note, { state: "done" });
  else addCall(reply, note, "done");
}

export const addForm = (reply, form) => reply.parts.push({ kind: "qcm", form });

export function addFile(reply, file) {
  const known = reply.files.find((item) => item.id === file.id);
  if (known) Object.assign(known, file, { action: known.action === "created" ? "created" : file.action });
  else reply.files.push(file);
}

/** The names of the files the history says Clara wrote or changed. */
export const fileNamesIn = (rows) => rows.flatMap((row) =>
  (row.calls || []).filter((call) => call.name in FILE_TOOLS && typeof call.arguments?.name === "string").map((call) => call.arguments.name));

/** The messages of a conversation as the server gives them (with `calls`): the answers between two questions are one
 * reply. `files` maps a file's name to its description, for the cards of the files she wrote. */
export function messagesFrom(rows, files = new Map()) {
  const messages = [];
  for (const row of rows) {
    if (row.role === "user") {
      messages.push({ role: "user", content: row.content });
      continue;
    }
    let reply = messages[messages.length - 1];
    if (reply?.role !== "assistant") messages.push(reply = newReply());
    if (row.content) {
      if (last(reply)?.kind === "text") addText(reply, "\n\n");
      addText(reply, row.content);
    }
    for (const call of row.calls || []) {
      const note = toolNote(call);
      if (note) addCall(reply, note, "done");
      const file = call.name in FILE_TOOLS && files.get(call.arguments?.name);
      if (file) addFile(reply, { ...file, action: call.arguments.overwrite === true ? "replaced" : FILE_TOOLS[call.name] });
    }
    for (const form of row.qcm || []) addForm(reply, form);
  }
  return messages.filter((m) => m.role === "user" || m.parts.length || m.files.length); // an answer of silent tools only: nothing
}

// ---- drawing it -------------------------------------------------------------------------------------------
const spinner = () => h("span", { class: "spin", "aria-hidden": "true" });

/** What a value of an argument looks like: text as it is, anything else as JSON. */
const shown = (value) => (typeof value === "string" ? value : JSON.stringify(value, null, 2));

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied.");
  } catch {
    toast("Could not copy.", true);
  }
}

/** What a call was given and gave back: the tool, each argument, the result. */
function callDetail(call) {
  const entries = Object.entries(call.args || {});
  const result = call.result === undefined
    ? h("p", { class: "call-none" }, call.state === "running" ? "Running…" : "Not kept for this call.")
    : h("pre", { class: "call-result" }, call.result || "(nothing)");
  return h("div", { class: "call-detail" },
    h("div", { class: "call-field" }, h("span", { class: "call-key" }, "Tool"), h("code", {}, call.name)),
    entries.length
      ? entries.map(([key, value]) => h("div", { class: "call-field" },
        h("span", { class: "call-key" }, key), h("pre", { class: "call-value" }, shown(value))))
      : h("div", { class: "call-field" }, h("span", { class: "call-key" }, "Arguments"), h("span", { class: "call-none" }, "none")),
    h("div", { class: "call-field" },
      h("span", { class: "call-key" }, "Result",
        call.result ? h("button", { type: "button", class: "ghost icon-btn call-copy", title: "Copy the result", "aria-label": "Copy the result", onclick: () => copy(call.result) }, icon("copy", { size: 14 })) : null),
      result,
      call.truncated && h("p", { class: "call-none" }, "Cut short: only the start of the result is shown.")));
}

/** One call: a line that opens on what the tool was given and what it gave back. */
function callNode(call, working, toggle) {
  const open = call.open === true;
  return h("div", { class: "call" + (open ? " open" : "") },
    h("button", { type: "button", class: "note call-toggle", "aria-expanded": String(open), title: open ? "Hide the details" : "Show the details", onclick: toggle },
      icon(call.glyph, { size: 14 }),
      h("span", {}, h("b", {}, call.label), call.detail ? ` ${call.detail}` : ""), working && spinner(), icon("chevron", { size: 12, className: "call-caret" })),
    open && callDetail(call));
}

/** "Searched the web ×2, read a page · 3 actions" */
function summary(calls) {
  const counts = new Map();
  for (const call of calls) {
    const kind = call.label.replace(/ (for|about|to)$/, "");
    counts.set(kind, (counts.get(kind) || 0) + 1);
  }
  const kinds = [...counts].map(([kind, n], index) => {
    const text = index ? kind.charAt(0).toLowerCase() + kind.slice(1) : kind;
    return n > 1 ? `${text} ×${n}` : text;
  });
  return `${kinds.length > 3 ? [...kinds.slice(0, 3), "…"].join(", ") : kinds.join(", ")} · ${calls.length} actions`;
}

/** The caret goes at the very end of the text being written, not on a line of its own. */
function placeCaret(node) {
  let at = node;
  while (at.lastChild && at.lastChild.nodeType === Node.ELEMENT_NODE && !["PRE", "TABLE", "DIV", "HR", "BR"].includes(at.lastChild.tagName)) at = at.lastChild;
  at.classList.add("caret");
}

/** One piece of the reply drawn: `{node, paint}`; `paint()` brings it up to date and costs nothing when nothing changed. */
function partView(reply, part, submit) {
  let drawn = null;
  const once = (signature, draw) => () => { if (signature() !== drawn) { drawn = signature(); draw(); } };
  if (part.kind === "qcm") return { node: h("div", { class: "part-card" }, qcmNode(part.form, { submit })), paint() {} };
  if (part.kind === "text") {
    const node = h("div", { class: "text" });
    const caret = () => Boolean(reply.live) && part === last(reply);
    let cost = 0; // milliseconds the last drawing took
    let drawnAt = 0;
    let later = 0; // a drawing planned for when the wait is over
    const draw = once(() => `${part.text.length}|${caret()}`, () => {
      const started = performance.now();
      const md = renderMarkdown(part.text);
      if (caret()) placeCaret(md);
      clear(node).append(md);
      drawnAt = performance.now();
      cost = drawnAt - started;
    });
    // The whole text is drawn again as it streams: a long answer waits 4× what drawing it took before the next
    // drawing, so that it never takes more than a fifth of the time (a short one, a fraction of a frame, never waits)
    const paint = () => {
      const wait = reply.live ? 4 * cost - (performance.now() - drawnAt) : 0;
      if (wait > 0) {
        if (!later) later = setTimeout(() => {
          later = 0;
          paint();
          node.dispatchEvent(new Event("repainted", { bubbles: true })); // drawn outside a frame: the page may scroll
        }, wait);
        return;
      }
      clearTimeout(later);
      later = 0;
      draw();
    };
    return { node, paint };
  }
  // a run of calls: alone it is a line of its own, with several it is one line that opens on one line per call
  const summaryText = h("span", { class: "run-summary" });
  const list = h("div", { class: "run-list" });
  const lone = h("div", { class: "run-lone" });
  const head = h("button", { type: "button", class: "run-head", onclick: () => { part.open = !part.open; view.paint(); } },
    icon("chevron", { size: 14 }), summaryText, spinner());
  const node = h("div", { class: "run" }, head, list, lone);
  const view = {
    node,
    paint: once(
      // working: the model is still on it (a call runs, or it reads what the calls gave back)
      () => JSON.stringify([part.calls.map((c) => [c.state, c.label, c.detail, c.open === true, c.result?.length, c.truncated]), part.open, working()]),
      () => {
        const many = part.calls.length > 1;
        node.classList.toggle("many", many);
        node.classList.toggle("open", many && part.open);
        node.classList.toggle("working", working());
        head.hidden = !many;
        head.setAttribute("aria-expanded", String(part.open));
        list.hidden = !(many && part.open);
        lone.hidden = many;
        summaryText.textContent = summary(part.calls);
        clear(list);
        clear(lone);
        const flip = (call) => () => { call.open = !call.open; view.paint(); };
        if (many && part.open) list.append(...part.calls.map((call) => callNode(call, call.state === "running" && Boolean(reply.live), flip(call))));
        if (!many) lone.append(callNode(part.calls[0], working(), flip(part.calls[0])));
      },
    ),
  };
  function working() {
    return Boolean(reply.live) && (part === last(reply) || part.calls.some((c) => c.state === "running"));
  }
  return view;
}

const count = new Intl.NumberFormat();

/** "1,204 tokens in · 312 out · 41.8 tok/s · 7.5 s" — what the answer used; null when the server said nothing. */
export function statsLine(stats) {
  if (!stats) return null;
  const { prompt_tokens: inTokens = 0, completion_tokens: outTokens = 0, generating = 0, total = 0 } = stats;
  const pieces = [`${count.format(inTokens)} in`, `${count.format(outTokens)} out`];
  if (outTokens && generating > 0) pieces.push(`${(outTokens / generating).toFixed(1)} tok/s`);
  if (total > 0) pieces.push(total < 10 ? `${total.toFixed(1)} s` : `${Math.round(total)} s`);
  return pieces.join(" · ");
}

/** The body of a reply: its parts in order, then the files. `sync()` draws what changed in the reply since the last time. */
export function replyBody(reply, who, submit) {
  const parts = h("div", { class: "parts" });
  const files = h("div", { class: "cards" });
  const waiting = h("span", { class: "waiting", role: "img", "aria-label": "Clara is writing" });
  const stats = h("div", { class: "reply-stats", title: "Tokens in and out, generation speed, total time" });
  const node = h("div", { class: "body" }, parts, files, stats);
  const views = new Map();
  let filesDrawn = "";
  return {
    node,
    sync() {
      if (!reply.parts.length && reply.live) { if (!waiting.isConnected) parts.append(waiting); } else waiting.remove();
      for (const part of reply.parts) {
        let view = views.get(part);
        if (!view) {
          views.set(part, view = partView(reply, part, submit));
          parts.append(view.node);
        }
      }
      for (const view of views.values()) view.paint();
      const line = reply.live ? null : statsLine(reply.stats);
      stats.hidden = !line;
      stats.textContent = line || "";
      const shown = JSON.stringify(reply.files);
      if (shown !== filesDrawn) {
        filesDrawn = shown;
        clear(files).append(...reply.files.map((file) => fileCard(file, who)));
      }
    },
  };
}
