// Your to-do list: tasks with the reminders that are still to come, as a list and as a calendar. Clara keeps the
// same list on every device and in every chat ("add a task: …"); when a reminder is sent she looks at the task again
// and may move the next ones, so the numbers here change by themselves.

import { api } from "./api.js";
import { mark } from "./icons.js";
import { clear, confirmDialog, dateTime, h, icon, openDialog, pageHead, parseDate, toast } from "./ui.js";

const who = (user) => ({ surface: "web", user_id: user.name });
const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
const REFRESH = 30000; // reminders are sent while the page is open: look again from time to time

const FILTERS = [["open", "To do"], ["done", "Done"], ["all", "All"]];
const VIEWS = [["list", "List"], ["calendar", "Calendar"]];
const pad = (n) => String(n).padStart(2, "0");

/** A moment as the value of a `datetime-local` input (the browser's clock). */
function toInput(text) {
  const date = parseDate(text);
  return date ? `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}` : "";
}

const dayKey = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
const clock = (date) => date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

const relativeFormat = new Intl.RelativeTimeFormat([], { numeric: "auto" });

/** "in 3 hours", "yesterday". */
function relative(text) {
  const date = parseDate(text);
  if (!date) return "";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const size = Math.abs(seconds);
  const [unit, step] = size < 3600 ? ["minute", 60] : size < 86400 ? ["hour", 3600] : size < 2592000 ? ["day", 86400] : ["month", 2592000];
  return relativeFormat.format(Math.round(seconds / step), unit);
}

const isOverdue = (task) => task.status === "open" && task.due_at && parseDate(task.due_at) < new Date();

export function mountTasks(container, user) {
  const query = who(user);
  let tasks = [];
  let limit = 10; // reminders the server sends for one task at most
  let loaded = false;
  let filter = "open";
  let view = "list";
  let month = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
  let selected = dayKey(new Date());

  const count = h("span", { class: "count" });
  const content = h("div", { class: "tasks-content" });
  const filterBox = h("div", { class: "segmented", role: "tablist", "aria-label": "Which tasks" });
  const viewBox = h("div", { class: "segmented", role: "tablist", "aria-label": "How to show them" });
  const page = h("div", { class: "container" });
  const root = h("section", { class: "page" },
    pageHead("Tasks", count, h("button", { class: "primary", onclick: () => addTask() }, icon("plus", { size: 18 }), "New task")),
    h("div", { class: "scroll" }, page));
  container.append(root);

  const shown = () => tasks.filter((task) => filter === "all" || task.status === filter);

  // ---- drawing ---------------------------------------------------------------------------------------

  function tabs(box, items, current, set) {
    clear(box).append(...items.map(([id, label]) =>
      h("button", { type: "button", role: "tab", "aria-selected": String(id === current), onclick: () => set(id) }, label)));
  }

  function draw() {
    const open = tasks.filter((task) => task.status === "open").length;
    count.textContent = loaded ? plural(open, "open task") : "";
    tabs(filterBox, FILTERS, filter, (id) => { filter = id; draw(); });
    tabs(viewBox, VIEWS, view, (id) => { view = id; draw(); });
    page.classList.toggle("wide", view === "calendar");
    clear(content);
    if (!loaded) return;
    content.append(view === "list" ? listView() : calendarView());
  }

  function emptyState() {
    const text = filter === "done" ? ["Nothing is done yet", "Finished tasks stay here."]
      : ["No task yet", "Add one above, or tell Clara in a chat: “add a task: send the invoice by Friday”."];
    return h("li", { class: "empty-row" }, h("div", { class: "empty-state" }, mark(36), h("strong", {}, text[0]), text[1]));
  }

  function listView() {
    const list = h("ul", { class: "list tasks", "aria-label": "Your tasks" });
    const found = shown();
    if (!found.length) list.append(emptyState());
    for (const task of found) list.append(row(task));
    return h("div", { class: "panel" }, list);
  }

  function facts(task) {
    const found = [];
    if (task.due_at) {
      found.push(h("span", { class: "badge" + (isOverdue(task) ? " off" : task.status === "done" ? "" : " admin"), title: dateTime(task.due_at) },
        icon("calendar", { size: 13 }), `${isOverdue(task) ? "Overdue, was due" : "Due"} ${dateTime(task.due_at)}`));
    }
    found.push(h("span", { class: "muted small", title: `At most ${limit} reminders are sent for one task` },
      `${plural(task.reminders_sent, "reminder")} sent`));
    if (task.status === "open") {
      found.push(task.next_reminder
        ? h("span", { class: "muted small next", title: dateTime(task.next_reminder) }, icon("bell", { size: 13 }), `Next reminder ${relative(task.next_reminder)}`)
        : h("span", { class: "muted small next" }, icon("bell", { size: 13 }), "No reminder to come"));
    }
    return found;
  }

  function row(task) {
    const done = task.status === "done";
    return h("li", { class: "task" + (done ? " done" : "") },
      h("button", { class: "check", type: "button", role: "checkbox", "aria-checked": String(done),
        title: done ? "Reopen this task" : "Mark as done", "aria-label": `${done ? "Reopen" : "Mark as done"}: ${task.title}`,
        onclick: (event) => toggle(task, event.currentTarget) }, done && icon("check", { size: 15 })),
      h("div", { class: "text" },
        h("button", { class: "linkish title", onclick: () => show(task) }, task.title),
        h("div", { class: "meta" }, facts(task))),
      h("div", { class: "actions" },
        h("button", { class: "ghost icon-btn", title: "Edit", "aria-label": `Edit ${task.title}`, onclick: () => editTask(task) }, icon("edit", { size: 18 })),
        h("button", { class: "ghost icon-btn danger forget", title: "Delete", "aria-label": `Delete ${task.title}`, onclick: () => remove(task) }, icon("trash", { size: 18 }))));
  }

  // ---- the calendar -----------------------------------------------------------------------------------

  /** What happens on each day: the deadlines and the reminders still to come, by day. */
  function eventsByDay() {
    const days = new Map();
    const put = (date, event) => {
      if (!date) return;
      const key = dayKey(date);
      if (!days.has(key)) days.set(key, []);
      days.get(key).push({ ...event, date });
    };
    for (const task of tasks) {
      if (filter !== "all" && task.status !== filter) continue;
      put(parseDate(task.due_at), { kind: "due", task });
      if (task.status === "open") for (const at of task.reminders) put(parseDate(at), { kind: "reminder", task });
    }
    for (const list of days.values()) list.sort((a, b) => a.date - b.date);
    return days;
  }

  function calendarView() {
    const days = eventsByDay();
    const today = dayKey(new Date());
    const first = new Date(month);
    const start = new Date(first);
    start.setDate(1 - ((first.getDay() + 6) % 7)); // weeks start on Monday
    const grid = h("div", { class: "cal-grid", role: "grid", "aria-label": month.toLocaleDateString([], { month: "long", year: "numeric" }) },
      ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((name) => h("div", { class: "cal-dow", role: "columnheader" }, name)));
    for (let i = 0; i < 42; i++) {
      const date = new Date(start);
      date.setDate(start.getDate() + i);
      const key = dayKey(date);
      const events = days.get(key) || [];
      const cell = h("div", { class: "cal-cell" + (date.getMonth() !== month.getMonth() ? " other" : "") + (key === today ? " today" : "") + (key === selected ? " selected" : ""),
        role: "gridcell", tabindex: "0", "aria-label": `${date.toLocaleDateString([], { dateStyle: "full" })}${events.length ? `, ${plural(events.length, "event")}` : ""}`,
        onclick: () => { selected = key; draw(); },
        onkeydown: (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selected = key; draw(); } } },
      h("span", { class: "cal-num" }, date.getDate()),
      h("div", { class: "cal-events" },
        events.slice(0, 2).map((item) => h("span", { class: `cal-chip ${item.kind}${item.task.status === "done" ? " done" : ""}`, title: eventTitle(item) }, h("span", { class: "chip-text" }, `${clock(item.date)} ${item.task.title}`))),
        events.length > 2 && h("span", { class: "cal-more" }, `+${events.length - 2}`)));
      grid.append(cell);
    }
    const heading = month.toLocaleDateString([], { month: "long", year: "numeric" });
    const bar = h("div", { class: "cal-bar" },
      h("button", { class: "ghost icon-btn", "aria-label": "Previous month", title: "Previous month", onclick: () => { month = new Date(month.getFullYear(), month.getMonth() - 1, 1); draw(); } },
        icon("chevron", { size: 18, className: "flip" })),
      h("h3", { class: "cal-title" }, heading),
      h("button", { class: "ghost icon-btn", "aria-label": "Next month", title: "Next month", onclick: () => { month = new Date(month.getFullYear(), month.getMonth() + 1, 1); draw(); } },
        icon("chevron", { size: 18 })),
      h("button", { class: "sm", onclick: () => { const now = new Date(); month = new Date(now.getFullYear(), now.getMonth(), 1); selected = dayKey(now); draw(); } }, "Today"));
    return h("div", {}, h("div", { class: "panel cal" }, bar, grid), agenda(days));
  }

  const eventTitle = (item) => `${item.kind === "due" ? "Due" : "Reminder"}: ${item.task.title} (${dateTime(item.date.toISOString())})`;

  function agenda(days) {
    const events = days.get(selected) || [];
    const date = new Date(`${selected}T12:00`);
    const list = h("ul", { class: "list agenda", "aria-label": "That day" });
    if (!events.length) list.append(h("li", { class: "empty-row" }, "Nothing planned that day."));
    for (const item of events) {
      list.append(h("li", {},
        h("span", { class: `glyph ${item.kind}` }, icon(item.kind === "due" ? "calendar" : "bell", { size: 16 })),
        h("div", { class: "text" },
          h("button", { class: "linkish title", onclick: () => show(item.task) }, item.task.title),
          h("div", { class: "muted small" }, `${item.kind === "due" ? "Due" : "Reminder"} at ${clock(item.date)}${item.task.status === "done" ? " · done" : ""}`))));
    }
    return h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", {}, date.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" })), h("span", { class: "count" }, plural(events.length, "event"))),
      list);
  }

  // ---- changing tasks ---------------------------------------------------------------------------------

  /** Show a task as the server just answered it, then read the list again: the server decides the order. */
  const replace = (task) => {
    tasks = tasks.some((item) => item.id === task.id) ? tasks.map((item) => (item.id === task.id ? task : item)) : [...tasks, task];
    draw();
    load();
  };

  async function toggle(task, button) {
    const reopening = task.status === "done";
    if (button) button.disabled = true;
    if (reopening) toast("Reopening: Clara is choosing the next reminders…");
    try {
      replace(await api.patch(`/v1/tasks/${task.id}`, { ...query, status: reopening ? "open" : "done", timezone: browserZone() }));
      toast(reopening ? "Task reopened." : "Done. No more reminders for this one.");
    } catch (error) {
      toast(error.detail || String(error), true);
      if (button) button.disabled = false;
    }
  }

  async function remove(task) {
    if (!await confirmDialog("Delete this task", `“${task.title}” and its reminders will be deleted for good.`, "Delete", true)) return;
    try {
      await api.delete(`/v1/tasks/${task.id}`, query);
      tasks = tasks.filter((item) => item.id !== task.id);
      draw();
      toast("Deleted.");
    } catch (error) {
      toast(error.detail || String(error), true);
    }
  }

  async function addTask() {
    const created = await editor(null);
    if (created) replace(created);
  }

  async function editTask(task) {
    const changed = await editor(task);
    if (changed) replace(changed);
  }

  /** The form to add or change a task: resolves with the task as the server has it, or null. */
  function editor(task) {
    return openDialog((close) => {
      const title = h("input", { type: "text", maxLength: 200, required: true, autofocus: true, placeholder: "What has to be done", value: task?.title || "" });
      const description = h("textarea", { rows: 3, maxLength: 2000, placeholder: "Details, if any", "aria-label": "Description" });
      description.value = task?.description || "";
      const initialDue = task?.due_at ? toInput(task.due_at) : "";
      const due = h("input", { type: "datetime-local", "aria-label": "Deadline", value: initialDue });
      const initialReminders = (task?.reminders || []).map(toInput);
      const reminders = h("div", { class: "reminder-rows" });
      let changed = false; // the reminders were touched: only then are they sent
      const addRow = (value = "") => {
        const input = h("input", { type: "datetime-local", "aria-label": "Reminder", value });
        input.addEventListener("input", () => { changed = true; });
        const line = h("div", { class: "row" }, input,
          h("button", { type: "button", class: "ghost icon-btn", title: "Remove this reminder", "aria-label": "Remove this reminder",
            onclick: () => { changed = true; line.remove(); hint(); } }, icon("close", { size: 16 })));
        reminders.append(line);
        hint();
      };
      const note = h("p", { class: "muted small" });
      const hint = () => {
        const none = !reminders.children.length;
        note.textContent = !none ? "" : task
          ? (task.status === "done" ? "A task that is done is not reminded." : "No reminder: Clara will not remind you of this task.")
          : "No reminder given: Clara chooses when to remind you, and moves the next ones each time one is sent.";
      };
      for (const value of initialReminders) addRow(value);
      hint();
      const error = h("p", { class: "notice warn small", hidden: true, role: "alert" });
      const save = h("button", { class: "primary", type: "submit" }, task ? "Save" : "Add task");
      const form = h("form", { class: "task-form", onsubmit: async (event) => {
        event.preventDefault();
        error.hidden = true;
        save.disabled = true;
        const times = [...reminders.querySelectorAll("input")].map((input) => input.value).filter(Boolean);
        try {
          let saved;
          if (!task) {
            save.textContent = times.length ? "Adding…" : "Clara is choosing the reminders…";
            saved = await api.post("/v1/tasks", {
              ...query, user_name: user.person?.name || user.name, title: title.value.trim(), description: description.value,
              due: due.value || null, reminders: times, timezone: browserZone(),
            });
          } else {
            const body = { ...query, title: title.value.trim(), description: description.value, timezone: browserZone() };
            if (due.value !== initialDue) body.due = due.value || null;
            if (changed && task.status === "open") body.reminders = times;
            saved = await api.patch(`/v1/tasks/${task.id}`, body);
          }
          close(saved);
        } catch (failure) {
          error.hidden = false;
          error.textContent = failure.detail || String(failure);
          save.disabled = false;
          save.textContent = task ? "Save" : "Add task";
        }
      } },
      h("h3", {}, task ? "Edit task" : "New task"),
      h("div", { class: "stack" },
        h("label", { class: "field" }, "Title", title),
        h("label", { class: "field" }, "Description", description),
        h("div", { class: "field" }, h("label", { for: "task-due" }, "Deadline (optional)"),
          h("div", { class: "row" }, Object.assign(due, { id: "task-due" }),
            h("button", { type: "button", class: "sm", onclick: () => { due.value = ""; } }, "Clear"))),
        h("div", { class: "field" }, "Reminders", reminders, note,
          (!task || task.status === "open") && h("button", { type: "button", class: "sm add-reminder", onclick: () => { changed = true; addRow(); } }, icon("plus", { size: 16 }), "Add a reminder")),
        error),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: () => close(null) }, "Cancel"), save));
      return form;
    });
  }

  /** One task in full; what to do next (edit, close it, delete it) comes back from the dialog. */
  async function show(task) {
    const action = await openDialog((close) => {
      const lines = [
        ["Description", task.description ? h("p", { class: "task-description" }, task.description) : h("span", { class: "muted" }, "none")],
        ["Deadline", task.due_at ? `${dateTime(task.due_at)}${isOverdue(task) ? " (overdue)" : ""}` : "none"],
        ["Reminders sent", `${task.reminders_sent} (at most ${limit} are sent for a task)`],
      ];
      if (task.status === "open") {
        lines.push(["Reminders to come", task.reminders.length
          ? h("ul", { class: "plain" }, task.reminders.map((at) => h("li", {}, `${dateTime(at)} · ${relative(at)}`)))
          : h("span", { class: "muted" }, "none: Clara will not remind you of this task unless you add one")]);
      } else if (task.done_at) lines.push(["Done", dateTime(task.done_at)]);
      if (task.targets.length) lines.push(["Shown on", task.targets.join(", ")]);
      lines.push(["Added", dateTime(task.created_at)]);
      return h("div", { class: "task-view" },
        h("h3", {}, task.title),
        h("span", { class: "badge " + (task.status === "done" ? "ok" : "admin") }, task.status === "done" ? "Done" : "To do"),
        h("dl", {}, lines.flatMap(([name, value]) => [h("dt", {}, name), h("dd", {}, value)])),
        h("div", { class: "actions" },
          h("button", { class: "danger", onclick: () => close("delete") }, "Delete"),
          h("button", { onclick: () => close("edit") }, "Edit"),
          h("button", { onclick: () => close("toggle") }, task.status === "done" ? "Reopen" : "Mark as done"),
          h("button", { class: "primary", onclick: () => close(null) }, "Close")));
    });
    if (action === "edit") await editTask(task);
    else if (action === "toggle") await toggle(task);
    else if (action === "delete") await remove(task);
  }

  // ---- loading ----------------------------------------------------------------------------------------

  async function load() {
    try {
      const found = await api.get("/v1/tasks", { ...query, status: "all" });
      tasks = found.tasks;
      limit = found.max_reminders;
    } catch (error) {
      if (error.status !== 404 && error.status !== 401) toast(error.detail || String(error), true);
      tasks = [];
    }
    loaded = true;
    draw();
  }

  page.append(
    h("p", { class: "intro" }, "Your to-do list, the same on every device and in every chat with Clara. Each task has reminders: choose them, or leave it to Clara, who also moves the next ones each time one is sent. You can just ask her too: “add a task: send the invoice by Friday”."),
    h("div", { class: "toolbar" }, filterBox, viewBox),
    content);
  draw();
  load();
  const timer = setInterval(load, REFRESH);
  return { destroy() { clearInterval(timer); root.remove(); } };
}
