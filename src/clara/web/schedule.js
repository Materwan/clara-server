// Scheduled tasks: a prompt, with files and integrations, that Clara runs by herself at the time you choose, once or as a
// routine (every day, on some days of the week, every month). Every run goes on in the same conversation, which is in
// your history; when it is over Clara sends you a private message on Discord with a summary of what she did.

import { api } from "./api.js";
import { DocumentError, MAX_TOTAL_CHARS, forModel, readDocument } from "./documents.js";
import { mark } from "./icons.js";
import { listProjects } from "./projects.js";
import { ago, clear, confirmDialog, dateTime, h, icon, openDialog, pageHead, parseDate, toast } from "./ui.js";

const who = (user) => ({ surface: "web", user_id: user.name });
const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
const REFRESH = 10000; // a run ends while the page is open: look again from time to time
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]; // 0 is Monday, as on the server
const REPEATS = [["", "Once"], ["daily", "Every day"], ["weekly", "On some days of the week"], ["monthly", "Every month"]];
const pad = (n) => String(n).padStart(2, "0");
const weekdayOf = (date) => (date.getDay() + 6) % 7;
const STATUS = { ok: ["ok", "Finished"], failed: ["off", "Failed"], missed: ["", "Skipped"] };

/** A moment as the value of a `datetime-local` input (the browser's clock). */
function toInput(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** "Every Mon, Wed at 09:00". */
function when(schedule) {
  const start = parseDate(schedule.start_at);
  const time = start.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (!schedule.repeat) return `Once, ${dateTime(schedule.start_at)}`;
  if (schedule.repeat === "daily") return `Every day at ${time}`;
  if (schedule.repeat === "weekly") return `Every ${schedule.days.map((day) => WEEKDAYS[day]).join(", ")} at ${time}`;
  return `Every month on day ${start.getDate()} at ${time}`;
}

export function mountSchedule(container, user) {
  const query = who(user);
  const discord = user.accounts?.some((account) => account.startsWith("discord:"));
  let schedules = [];
  let loaded = false;
  let max = 50;

  const count = h("span", { class: "count" });
  const content = h("div", { class: "schedule-content" });
  const page = h("div", { class: "container" });
  const root = h("section", { class: "page" },
    pageHead("Schedule", count, h("button", { class: "primary", onclick: () => edit(null) }, icon("plus", { size: 18 }), "New schedule")),
    h("div", { class: "scroll" }, page));
  container.append(root);

  // ---- drawing ---------------------------------------------------------------------------------------

  function draw() {
    count.textContent = loaded ? `${schedules.length} scheduled` : "";
    clear(content);
    if (!loaded) return;
    const list = h("ul", { class: "list schedules", "aria-label": "Your scheduled tasks" });
    if (!schedules.length) {
      list.append(h("li", { class: "empty-row" }, h("div", { class: "empty-state" }, mark(36), h("strong", {}, "Nothing is scheduled"),
        "Plan a prompt for Clara: a report every morning, a check every Friday, a reminder to do something for you.")));
    }
    for (const schedule of schedules) list.append(row(schedule));
    content.append(h("div", { class: "panel" }, list));
  }

  function state(schedule) {
    if (schedule.running) return h("span", { class: "badge admin" }, "Running…");
    if (!schedule.enabled) return h("span", { class: "badge" }, "Paused");
    if (!schedule.next_at) return h("span", { class: "badge ok" }, schedule.last_at ? "Completed" : "Nothing to come");
    return h("span", { class: "muted small next", title: dateTime(schedule.next_at) }, icon("clock", { size: 13 }), ` Next run ${dateTime(schedule.next_at)}`);
  }

  function row(schedule) {
    const [tone, label] = STATUS[schedule.last_status] || ["", ""];
    return h("li", { class: "sched" },
      h("div", { class: "text" },
        h("button", { class: "linkish title", onclick: () => edit(schedule) }, schedule.name),
        h("div", { class: "meta" },
          h("span", { class: "muted small" }, icon("calendar", { size: 13 }), ` ${when(schedule)}`),
          state(schedule),
          schedule.documents.length > 0 && h("span", { class: "muted small", title: schedule.documents.map((doc) => doc.name).join(", ") }, icon("file", { size: 13 }), ` ${schedule.documents.length} file${schedule.documents.length === 1 ? "" : "s"}`),
          schedule.resources.length > 0 && h("span", { class: "muted small" }, icon("plug", { size: 13 }), ` ${schedule.resources.length} integration${schedule.resources.length === 1 ? "" : "s"}`)),
        schedule.last_at && h("p", { class: "last" },
          h("span", { class: `badge ${tone}` }, label), ` ${ago(schedule.last_at)}: ${schedule.last_summary}`)),
      h("div", { class: "actions" },
        schedule.runs > 0 && h("button", { class: "ghost icon-btn", title: "Open its conversation", "aria-label": `Open the conversation of ${schedule.name}`, onclick: () => { location.hash = `#/chat/open/${encodeURIComponent(schedule.conversation)}`; } }, icon("chat", { size: 18 })),
        h("button", { class: "ghost icon-btn", title: "Run it now", "aria-label": `Run ${schedule.name} now`, disabled: schedule.running, onclick: () => runNow(schedule) }, icon("bolt", { size: 18 })),
        h("button", { class: "ghost icon-btn", title: schedule.enabled ? "Pause" : "Resume", "aria-label": `${schedule.enabled ? "Pause" : "Resume"} ${schedule.name}`, onclick: () => toggle(schedule) }, icon(schedule.enabled ? "pause" : "play", { size: 18 })),
        h("button", { class: "ghost icon-btn", title: "Edit", "aria-label": `Edit ${schedule.name}`, onclick: () => edit(schedule) }, icon("edit", { size: 18 })),
        h("button", { class: "ghost icon-btn danger forget", title: "Delete", "aria-label": `Delete ${schedule.name}`, onclick: () => remove(schedule) }, icon("trash", { size: 18 }))));
  }

  // ---- changing them ----------------------------------------------------------------------------------

  const attempt = async (action) => {
    try { await action(); } catch (error) { toast(error.detail || String(error), true); }
    await load();
  };

  const runNow = (schedule) => attempt(async () => {
    await api.post(`/v1/schedules/${schedule.id}/run`, query);
    toast(discord ? "Started. Clara will send you a message on Discord when she is done." : "Started: see its conversation when it is done.");
  });

  const toggle = (schedule) => attempt(() => api.patch(`/v1/schedules/${schedule.id}`, { ...query, enabled: !schedule.enabled }));

  async function remove(schedule) {
    if (!await confirmDialog("Delete this scheduled task", `“${schedule.name}” will not run again. Its conversation stays in your history.`, "Delete", true)) return;
    await attempt(() => api.delete(`/v1/schedules/${schedule.id}`, query));
  }

  /** The form to plan a task, or change `schedule`. */
  async function edit(schedule) {
    let resources = [];
    let projects = [];
    try {
      [resources, projects] = await Promise.all([
        api.get("/v1/integrations", query).then((found) => found.resources),
        listProjects(user).catch(() => []),
      ]);
    } catch (error) {
      if (error.status !== 404) return toast(error.detail || String(error), true);
    }
    const saved = await openDialog((close) => {
      const name = h("input", { type: "text", maxLength: 100, required: true, autofocus: true, placeholder: "What this is, e.g. Morning report", value: schedule?.name || "" });
      const prompt = h("textarea", { rows: 5, maxLength: 20000, placeholder: "What Clara has to do when the time comes", "aria-label": "Prompt" });
      prompt.value = schedule?.prompt || "";
      // files: those the task has (no text here, the server keeps it) and those added now
      const docs = (schedule?.documents || []).map((doc) => ({ name: doc.name, kind: doc.kind, chars: doc.chars }));
      const chips = h("div", { class: "chips" });
      const drawChips = () => clear(chips).append(...docs.map((doc) => h("span", { class: "chip" }, icon("file", { size: 15 }), h("span", { class: "name" }, doc.name),
        h("button", { type: "button", "aria-label": `Remove ${doc.name}`, onclick: () => { docs.splice(docs.indexOf(doc), 1); drawChips(); } }, icon("close", { size: 14 })))));
      const picker = h("input", { type: "file", multiple: true, hidden: true, onchange: async () => {
        for (const file of picker.files) {
          if (docs.some((doc) => doc.name === file.name)) { toast(`${file.name} is already attached.`); continue; }
          try {
            const doc = await readDocument(file);
            if (docs.reduce((sum, item) => sum + (item.text ? forModel(item).length : item.chars), 0) + forModel(doc).length > MAX_TOTAL_CHARS) throw new DocumentError("Too much text in all: remove a file first.");
            docs.push(doc);
          } catch (error) { toast(error.message, true); }
        }
        picker.value = "";
        drawChips();
      } });
      drawChips();
      const chosen = new Set(schedule?.resources || []);
      const connections = resources.length
        ? h("div", { class: "pick-list" }, resources.map((resource) => h("label", { class: "pick" },
          h("input", { type: "checkbox", checked: chosen.has(resource.id), onchange: (event) => { if (event.target.checked) chosen.add(resource.id); else chosen.delete(resource.id); } }),
          icon(resource.type === "github" ? "branch" : resource.type === "gdrive" ? "cloud" : "folder", { size: 16 }), h("span", {}, resource.label))))
        : h("p", { class: "muted small" }, "Nothing is connected yet: add GitHub, Google Drive or a folder on the ", h("a", { href: "#/integrations", onclick: () => close(null) }, "Integrations"), " page.");
      const project = h("select", { "aria-label": "Project" }, h("option", { value: "" }, "No project"),
        projects.map((item) => h("option", { value: item.id, selected: item.id === schedule?.project }, item.name)));

      const first = schedule ? parseDate(schedule.start_at) : (() => { const d = new Date(Date.now() + 3600000); d.setMinutes(0, 0, 0); return d; })();
      const at = h("input", { type: "datetime-local", required: true, value: toInput(first), "aria-label": "First run" });
      const repeat = h("select", { "aria-label": "How often" }, REPEATS.map(([value, label]) => h("option", { value, selected: value === (schedule?.repeat || "") }, label)));
      const days = new Set(schedule?.days || []);
      const weekdays = h("div", { class: "weekdays", role: "group", "aria-label": "Days of the week" }, WEEKDAYS.map((label, day) => h("label", { class: "pick" },
        h("input", { type: "checkbox", checked: days.has(day), onchange: (event) => { if (event.target.checked) days.add(day); else days.delete(day); } }), label)));
      const whenLabel = h("span", {}, "");
      const sync = () => {
        weekdays.hidden = repeat.value !== "weekly";
        whenLabel.textContent = repeat.value ? "Starting" : "When";
        if (repeat.value === "weekly" && !days.size && at.value) {
          days.add(weekdayOf(new Date(at.value)));
          weekdays.querySelectorAll("input")[weekdayOf(new Date(at.value))].checked = true;
        }
      };
      repeat.addEventListener("change", sync);
      sync();
      const initial = { at: at.value, repeat: repeat.value, days: [...days].sort().join() };

      const error = h("p", { class: "notice warn small", hidden: true, role: "alert" });
      const save = h("button", { class: "primary", type: "submit" }, schedule ? "Save" : "Schedule it");
      return h("form", { class: "task-form sched-form", onsubmit: async (event) => {
        event.preventDefault();
        error.hidden = true;
        save.disabled = true;
        const body = {
          ...query, name: name.value.trim(), prompt: prompt.value,
          documents: docs.map(({ name: docName, kind, text }) => ({ name: docName, kind, text })),
          project: project.value ? Number(project.value) : null, resources: [...chosen],
        };
        const timing = { at: at.value, timezone: browserZone(), repeat: repeat.value, days: repeat.value === "weekly" ? [...days].sort() : [] };
        const unchanged = schedule && initial.at === at.value && initial.repeat === repeat.value && initial.days === timing.days.join();
        try {
          close(schedule ? await api.patch(`/v1/schedules/${schedule.id}`, unchanged ? body : { ...body, ...timing })
            : await api.post("/v1/schedules", { ...body, ...timing }));
        } catch (failure) {
          error.hidden = false;
          error.textContent = failure.detail || String(failure);
          save.disabled = false;
        }
      } },
      h("h3", {}, schedule ? "Edit scheduled task" : "New scheduled task"),
      h("div", { class: "stack" },
        h("label", { class: "field" }, "Name", name),
        h("label", { class: "field" }, "Prompt", prompt),
        h("div", { class: "field" }, "Files", chips,
          h("button", { type: "button", class: "sm add-reminder", onclick: () => picker.click() }, icon("clip", { size: 16 }), "Attach files (PDF, Word, code, text)"), picker),
        h("div", { class: "field" }, "What Clara can reach", connections),
        h("label", { class: "field" }, "Project (its files and instructions)", project),
        h("div", { class: "field" }, whenLabel, at),
        h("label", { class: "field" }, "How often", repeat),
        weekdays,
        !discord && h("p", { class: "notice small" }, "Your Discord account is not linked, so Clara cannot message you when she is done: the result is in the task's conversation. Link it in your account settings."),
        h("p", { class: "muted small" }, "Clara works alone: she asks you nothing, and an action that needs your permission is asked in the usual way while she carries on."),
        error),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: () => close(null) }, "Cancel"), save));
    });
    if (saved) {
      toast(schedule ? "Saved." : "Scheduled.");
      await load();
    }
  }

  // ---- loading ----------------------------------------------------------------------------------------

  async function load() {
    try {
      const found = await api.get("/v1/schedules", query);
      schedules = found.schedules;
      max = found.max;
    } catch (error) {
      if (error.status !== 404 && error.status !== 401) toast(error.detail || String(error), true);
      schedules = [];
    }
    loaded = true;
    draw();
  }

  page.append(
    h("p", { class: "intro" }, `A prompt that Clara runs by herself at the time you choose, once or as a routine, with the files and the integrations you give her (at most ${max}). Every run goes on in the same conversation, which you find in your history. When it is over, Clara sends you a message on Discord with a short summary of what she did.`),
    content);
  draw();
  load();
  const timer = setInterval(load, REFRESH);
  return { destroy() { clearInterval(timer); root.remove(); } };
}
