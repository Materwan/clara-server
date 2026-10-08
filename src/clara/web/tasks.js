// Your to-do list: tasks with the reminders that are still to come, as a list and as a calendar. Clara keeps the
// same list on every device and in every chat ("add a task: …"); when a reminder is sent she looks at the task again
// and may move the next ones, so the numbers here change by themselves. A task can be divided into sub tasks (and
// those into sub tasks): each has its own description and reminders, none of them later than the deadline of the
// tasks it is part of; they are drawn under their task, and a task is done when all its sub tasks are.
// Tasks are dragged (by the mouse from anywhere on the row, by a finger from the grip): onto a task to become one of
// its sub tasks, on its upper or lower edge to be put before or after it, onto the bar at the bottom to be a main task.

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
  let drag = null; // a task being dragged: see "dragging" below
  let redraw = false; // the list changed while a task was dragged

  const count = h("span", { class: "count" });
  const content = h("div", { class: "tasks-content" });
  const filterBox = h("div", { class: "segmented", role: "tablist", "aria-label": "Which tasks" });
  const viewBox = h("div", { class: "segmented", role: "tablist", "aria-label": "How to show them" });
  const page = h("div", { class: "container" });
  const root = h("section", { class: "page" },
    pageHead("Tasks", count, h("button", { class: "primary", onclick: () => addTask() }, icon("plus", { size: 18 }), "New task")),
    h("div", { class: "scroll" }, page));
  container.append(root);

  const known = () => new Map(tasks.map((task) => [task.id, task]));
  const byOrder = (a, b) => a.position - b.position || a.id - b.id; // an order chosen by dragging, else the oldest first
  const kidsOf = (id) => tasks.filter((task) => task.parent_id === id).sort(byOrder);
  /** The tasks with that parent (null: the main tasks), in the order they are shown. */
  const siblingsOf = (id) => (id == null ? tasks.filter((task) => task.parent_id == null).sort((a, b) => a.position - b.position) : kidsOf(id));
  const below = (task) => kidsOf(task.id).flatMap((kid) => [kid, ...below(kid)]); // its sub tasks, theirs, and so on
  const earliest = (...moments) => moments.filter(Boolean).sort((a, b) => parseDate(a) - parseDate(b))[0] || null;

  /** What the list draws, in order, with how deep each is: the main tasks that match, each followed by its sub tasks. */
  function rows() {
    const byId = known();
    const isRoot = (task) => {
      const parent = byId.get(task.parent_id);
      if (filter === "done") return task.status === "done" && parent?.status !== "done"; // a done sub task of a task still to do
      if (filter === "open") return task.status === "open" && !parent;
      return !parent;
    };
    const out = [];
    const walk = (task, depth) => {
      out.push({ task, depth });
      for (const kid of kidsOf(task.id)) walk(kid, depth + 1);
    };
    for (const task of tasks.filter(isRoot).sort((a, b) => a.position - b.position)) walk(task, 0); // the sort keeps the server's order of those nobody placed
    return out;
  }

  // ---- drawing ---------------------------------------------------------------------------------------

  function tabs(box, items, current, set) {
    clear(box).append(...items.map(([id, label]) =>
      h("button", { type: "button", role: "tab", "aria-selected": String(id === current), onclick: () => set(id) }, label)));
  }

  function draw() {
    if (drag?.started) { redraw = true; return; } // the rows are in the user's hand: draw them when it is over
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
    list.addEventListener("pointerdown", pressed);
    const found = rows();
    if (!found.length) list.append(emptyState());
    for (const { task, depth } of found) list.append(row(task, depth));
    return h("div", { class: "panel" }, list);
  }

  function facts(task, depth = 0) {
    const found = [];
    const parent = task.parent_id && known().get(task.parent_id);
    if (parent && depth === 0) found.push(h("span", { class: "muted small", title: "The task it is part of" }, `Part of “${parent.title}”`));
    if (task.subtasks?.total) {
      found.push(h("span", { class: "muted small", title: "Sub tasks done" }, icon("tasks", { size: 13 }), ` ${task.subtasks.done}/${task.subtasks.total} sub tasks`));
    }
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

  function row(task, depth = 0) {
    const done = task.status === "done";
    return h("li", { class: "task" + (done ? " done" : "") + (depth ? " sub" : ""), style: `--depth: ${depth}`, "data-id": task.id },
      done ? h("span", { class: "grip-space" })
        : h("button", { class: "grip", type: "button", title: "Drag onto a task to make this a sub task of it, or between tasks to move it",
          "aria-label": `Drag ${task.title}`, tabindex: "-1" }, icon("grip", { size: 16 })),
      h("button", { class: "check", type: "button", role: "checkbox", "aria-checked": String(done),
        title: done ? "Reopen this task" : "Mark as done", "aria-label": `${done ? "Reopen" : "Mark as done"}: ${task.title}`,
        onclick: (event) => toggle(task, event.currentTarget) }, done && icon("check", { size: 15 })),
      h("div", { class: "text" },
        h("button", { class: "linkish title", onclick: () => show(task) }, task.title),
        h("div", { class: "meta" }, facts(task, depth))),
      h("div", { class: "actions" },
        !done && h("button", { class: "ghost icon-btn", title: "Add a sub task", "aria-label": `Add a sub task to ${task.title}`, onclick: () => addTask(task) }, icon("plus", { size: 18 })),
        h("button", { class: "ghost icon-btn", title: "Edit", "aria-label": `Edit ${task.title}`, onclick: () => editTask(task) }, icon("edit", { size: 18 })),
        h("button", { class: "ghost icon-btn danger forget", title: "Delete", "aria-label": `Delete ${task.title}`, onclick: () => remove(task) }, icon("trash", { size: 18 }))));
  }

  // ---- dragging ---------------------------------------------------------------------------------------

  const dropBar = h("div", { class: "drop-main", hidden: true }, "Drop here to make it a main task");
  root.append(dropBar);
  const DRAG_FROM = 5; // pixels the pointer goes before a press becomes a drag (below: a click)
  const EDGE = 0.28; // the part of a row, at its top and bottom, that means "before" and "after"

  /** Why `task` cannot be part of `parent` (null: a main task), or null when it can. */
  function refusal(task, parent) {
    if (parent == null) return null;
    if (parent.id === task.id || below(task).some((part) => part.id === parent.id)) return "A task cannot be part of itself.";
    if (parent.status !== "open") return `“${parent.title}” is done: reopen it first.`;
    const limit = earliest(parent.due_at, parent.due_limit);
    if (!limit) return null;
    const latest = parseDate(limit);
    for (const part of [task, ...below(task)]) {
      if (part.due_at && parseDate(part.due_at) > latest) return `“${part.title}” is due after ${dateTime(limit)}, the deadline of “${parent.title}”.`;
      if (part.status === "open" && part.reminders.some((at) => parseDate(at) > latest)) return `A reminder of “${part.title}” is after ${dateTime(limit)}, the deadline of “${parent.title}”.`;
    }
    return null;
  }

  /** Where the task would go if dropped at that point: `{ parent, before, label, bad }`, or null over nothing. */
  function aim(task, x, y) {
    const over = document.elementFromPoint(x, y);
    if (!over) return null;
    if (over.closest(".drop-main")) {
      return task.parent_id == null ? { bad: "It is a main task already.", label: "" } : { parent: null, before: null, label: "Make it a main task", bar: true };
    }
    const li = over.closest("li.task");
    const target = li && known().get(Number(li.dataset.id));
    if (!target) return null;
    const box = li.getBoundingClientRect();
    const spot = (y - box.top) / box.height;
    const zone = target.id === task.id ? "self" : spot < EDGE ? "before" : spot > 1 - EDGE ? "after" : "into";
    if (zone === "self") return { li, zone, bad: "" };
    if (below(task).some((part) => part.id === target.id)) return { li, zone, bad: "A task cannot be part of itself." };
    if (zone === "into") {
      const bad = refusal(task, target);
      return { li, zone, parent: target.id, before: null, label: `Sub task of “${target.title}”`, bad };
    }
    const parent = target.parent_id == null ? null : known().get(target.parent_id) || null;
    const others = siblingsOf(target.parent_id ?? null).filter((item) => item.id !== task.id);
    const first = zone === "after" ? kidsOf(target.id).find((kid) => kid.id !== task.id) : null;
    if (first) { // under a row that shows its sub tasks, "after" is where they start
      return { li, zone, parent: target.id, before: first.id, bad: refusal(task, target), label: `First sub task of “${target.title}”` };
    }
    const at = others.findIndex((item) => item.id === target.id);
    return { li, zone, parent: parent?.id ?? null, before: zone === "before" ? target.id : others[at + 1]?.id ?? null, bad: refusal(task, parent),
      label: `${zone === "before" ? "Before" : "After"} “${target.title}”${parent ? ` in “${parent.title}”` : ""}` };
  }

  function pressed(event) {
    if (view !== "list" || drag || (event.pointerType === "mouse" && event.button !== 0)) return;
    const li = event.target.closest("li.task");
    const task = li && known().get(Number(li.dataset.id));
    if (!task || task.status !== "open" || event.target.closest(".check, .actions")) return;
    if (event.pointerType !== "mouse" && !event.target.closest(".grip")) return; // a finger on the row scrolls the page
    drag = { task, li, id: event.pointerId, x: event.clientX, y: event.clientY, started: false, aim: null, ghost: null, hint: null, scroller: 0, speed: 0 };
    document.addEventListener("pointermove", moved);
    document.addEventListener("pointerup", released);
    document.addEventListener("pointercancel", cancelled);
    document.addEventListener("keydown", escaped);
  }

  function begin() {
    drag.started = true;
    getSelection()?.removeAllRanges();
    drag.hint = h("small", {});
    drag.ghost = h("div", { class: "drag-ghost", "aria-hidden": "true" }, h("span", {}, drag.task.title), drag.hint);
    document.body.append(drag.ghost);
    document.body.classList.add("dragging");
    drag.li.classList.add("dragging");
    dropBar.hidden = drag.task.parent_id == null;
  }

  /** The click that ends a drag is not a click on what is under the pointer. */
  function swallowClick() {
    const swallow = (event) => { event.stopPropagation(); event.preventDefault(); };
    document.addEventListener("click", swallow, true);
    setTimeout(() => document.removeEventListener("click", swallow, true), 0);
  }

  function moved(event) {
    if (!drag || event.pointerId !== drag.id) return;
    if (!drag.started) {
      if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < DRAG_FROM) return;
      begin();
    }
    event.preventDefault();
    drag.x = event.clientX;
    drag.y = event.clientY;
    follow();
  }

  /** Draw where the drag is: the ghost, what it would do, and scroll when it is near an edge of the page. */
  function follow() {
    const { ghost, task } = drag;
    ghost.style.left = `${Math.min(drag.x + 14, innerWidth - ghost.offsetWidth - 8)}px`;
    ghost.style.top = `${Math.min(drag.y + 14, innerHeight - ghost.offsetHeight - 8)}px`;
    const found = aim(task, drag.x, drag.y);
    for (const marked of root.querySelectorAll(".drop-into, .drop-before, .drop-after, .drop-bad")) marked.classList.remove("drop-into", "drop-before", "drop-after", "drop-bad");
    dropBar.classList.toggle("over", Boolean(found?.bar));
    drag.aim = found;
    ghost.classList.toggle("bad", Boolean(found?.bad));
    drag.hint.textContent = found?.bad || (found?.label ?? "");
    if (found?.li && found.zone !== "self") found.li.classList.add(found.bad ? "drop-bad" : `drop-${found.zone}`);
    const scroller = root.querySelector(".scroll");
    const box = scroller.getBoundingClientRect();
    const near = 60;
    drag.speed = drag.y < box.top + near ? -Math.ceil((box.top + near - drag.y) / 4) : drag.y > box.bottom - near ? Math.ceil((drag.y - (box.bottom - near)) / 4) : 0;
    if (drag.speed && !drag.scroller) {
      const step = () => {
        if (!drag?.started || !drag.speed) { if (drag) drag.scroller = 0; return; }
        scroller.scrollTop += drag.speed;
        follow();
        drag.scroller = requestAnimationFrame(step);
      };
      drag.scroller = requestAnimationFrame(step);
    }
  }

  function over() {
    const finished = drag;
    drag = null;
    document.removeEventListener("pointermove", moved);
    document.removeEventListener("pointerup", released);
    document.removeEventListener("pointercancel", cancelled);
    document.removeEventListener("keydown", escaped);
    if (finished?.started) {
      cancelAnimationFrame(finished.scroller);
      finished.ghost.remove();
      document.body.classList.remove("dragging");
      dropBar.hidden = true;
      dropBar.classList.remove("over");
      swallowClick();
    }
    return finished;
  }

  function cancelled() {
    const finished = over();
    if (finished?.started) { redraw = false; draw(); }
  }
  function escaped(event) { if (event.key === "Escape") cancelled(); }

  async function released(event) {
    if (!drag || event.pointerId !== drag.id) return;
    const { task, aim: found, started } = drag;
    over();
    if (!started) return;
    redraw = false;
    draw();
    if (!found) return; // dropped over nothing
    if (found.bad) toast(found.bad, true);
    else if (found.parent !== undefined) await moveTask(task, found.parent, found.before); // (undefined: on itself)
  }

  /** Put a task under `parent` (null: a main task) before the task `before` (null: last). */
  async function moveTask(task, parent, before) {
    const oldParent = known().get(task.parent_id);
    const order = siblingsOf(parent).filter((item) => item.id !== task.id).map((item) => item.id);
    const at = before == null ? order.length : order.indexOf(before);
    order.splice(at, 0, task.id);
    const same = (task.parent_id ?? null) === parent;
    if (same && siblingsOf(parent).map((item) => item.id).join() === order.join()) return; // dropped where it is
    // what is left of the task it was part of may be all done: the server finishes it
    const finished = oldParent && !same && oldParent.status === "open" && kidsOf(oldParent.id).length > 1
      && kidsOf(oldParent.id).filter((kid) => kid.id !== task.id).every((kid) => kid.status === "done");
    try {
      const moved = await api.patch(`/v1/tasks/${task.id}`, { ...query, parent_id: parent, before_id: before, timezone: browserZone() });
      await load(); // the order of the others changed too
      let text = same ? "Moved." : parent == null ? "Now a main task." : `Now a sub task of “${known().get(parent)?.title ?? "that task"}”.`;
      if (finished) text += ` “${oldParent.title}” is done now: all its sub tasks are.`;
      toast(text);
      return moved;
    } catch (error) {
      toast(error.detail || String(error), true);
      load();
    }
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
    const unfinished = reopening ? 0 : below(task).filter((kid) => kid.status === "open").length;
    if (unfinished && !await confirmDialog("Mark as done", `“${task.title}” has ${plural(unfinished, "sub task")} still to do: they will be marked as done too.`, "Mark as done")) return;
    if (button) button.disabled = true;
    if (reopening) toast("Reopening: Clara is choosing the next reminders…");
    try {
      replace(await api.patch(`/v1/tasks/${task.id}`, { ...query, status: reopening ? "open" : "done", timezone: browserZone() }));
      toast(reopening ? "Task reopened." : unfinished ? "Done, with its sub tasks. No more reminders for them." : "Done. No more reminders for this one.");
    } catch (error) {
      toast(error.detail || String(error), true);
      if (button) button.disabled = false;
    }
  }

  async function remove(task) {
    const parts = below(task);
    const also = parts.length ? `, its ${plural(parts.length, "sub task")}` : "";
    if (!await confirmDialog("Delete this task", `“${task.title}”${also} and the reminders will be deleted for good.`, "Delete", true)) return;
    try {
      await api.delete(`/v1/tasks/${task.id}`, query);
      tasks = tasks.filter((item) => item.id !== task.id && !parts.includes(item));
      draw();
      load(); // the task it was part of may be done now
      toast("Deleted.");
    } catch (error) {
      toast(error.detail || String(error), true);
    }
  }

  /** A new task, or a sub task of `parent`. */
  async function addTask(parent = null) {
    const created = await editor(null, parent);
    if (created) replace(created);
  }

  async function editTask(task) {
    const changed = await editor(task);
    if (changed) replace(changed);
  }

  /** The form to add or change a task (a sub task of `parent`, if given): resolves with the task as the server has it, or null. */
  function editor(task, parent = null) {
    // nothing of a sub task may be later than the deadline of the tasks it is part of
    const limit = task ? task.due_limit : earliest(parent?.due_at, parent?.due_limit);
    const limitInput = limit ? toInput(limit) : "";
    const partOf = task ? known().get(task.parent_id) : parent;
    return openDialog((close) => {
      const title = h("input", { type: "text", maxLength: 200, required: true, autofocus: true, placeholder: "What has to be done", value: task?.title || "" });
      const description = h("textarea", { rows: 3, maxLength: 2000, placeholder: "Details, if any", "aria-label": "Description" });
      description.value = task?.description || "";
      const initialDue = task?.due_at ? toInput(task.due_at) : "";
      const due = h("input", { type: "datetime-local", "aria-label": "Deadline", value: initialDue, max: limitInput || false });
      const initialReminders = (task?.reminders || []).map(toInput);
      const reminders = h("div", { class: "reminder-rows" });
      let changed = false; // the reminders were touched: only then are they sent
      const addRow = (value = "") => {
        const input = h("input", { type: "datetime-local", "aria-label": "Reminder", value, max: limitInput || false });
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
      const save = h("button", { class: "primary", type: "submit" }, task ? "Save" : parent ? "Add sub task" : "Add task");
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
              due: due.value || null, reminders: times, timezone: browserZone(), parent_id: parent?.id,
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
          save.textContent = task ? "Save" : parent ? "Add sub task" : "Add task";
        }
      } },
      h("h3", {}, task ? (task.parent_id ? "Edit sub task" : "Edit task") : parent ? "New sub task" : "New task"),
      partOf && h("p", { class: "muted small" }, `Part of “${partOf.title}”.${limit ? ` Its deadline and reminders cannot be after ${dateTime(limit)}.` : ""}`),
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
      const byId = known();
      const parent = byId.get(task.parent_id);
      if (parent) lines.unshift(["Part of", h("button", { class: "linkish", onclick: () => close(`open:${parent.id}`) }, parent.title)]);
      if (task.due_limit) lines.push(["Latest allowed", `${dateTime(task.due_limit)}, the deadline of the task it is part of`]);
      const kids = kidsOf(task.id);
      if (kids.length) {
        lines.push(["Sub tasks", h("ul", { class: "plain" }, kids.map((kid) => h("li", {},
          h("button", { class: "linkish", onclick: () => close(`open:${kid.id}`) }, `${kid.status === "done" ? "✓ " : ""}${kid.title}`))))]);
      }
      lines.push(["Added", dateTime(task.created_at)]);
      return h("div", { class: "task-view" },
        h("h3", {}, task.title),
        h("span", { class: "badge " + (task.status === "done" ? "ok" : "admin") }, task.status === "done" ? "Done" : "To do"),
        h("dl", {}, lines.flatMap(([name, value]) => [h("dt", {}, name), h("dd", {}, value)])),
        h("div", { class: "actions" },
          h("button", { class: "danger", onclick: () => close("delete") }, "Delete"),
          task.status === "open" && h("button", { onclick: () => close("sub") }, "Add a sub task"),
          h("button", { onclick: () => close("edit") }, "Edit"),
          h("button", { onclick: () => close("toggle") }, task.status === "done" ? "Reopen" : "Mark as done"),
          h("button", { class: "primary", onclick: () => close(null) }, "Close")));
    });
    if (action === "edit") await editTask(task);
    else if (action === "sub") await addTask(task);
    else if (typeof action === "string" && action.startsWith("open:")) {
      const next = known().get(Number(action.slice(5)));
      if (next) await show(next);
    }
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
    h("p", { class: "intro" }, "Your to-do list, the same on every device and in every chat with Clara. Each task has reminders: choose them, or leave it to Clara, who also moves the next ones each time one is sent. A task can be divided into sub tasks, each with its own reminders and none of them later than the task's deadline. You can just ask her too: “add a task: send the invoice by Friday”."),
    h("div", { class: "toolbar" }, filterBox, viewBox),
    content);
  draw();
  load();
  const timer = setInterval(load, REFRESH);
  return { destroy() { clearInterval(timer); root.remove(); } };
}
