// Small helpers to build the page: elements, dialogs, menus, toasts, dates. No inline styles or HTML strings:
// everything is made with the DOM, so text from the server can never become markup.

import { icon } from "./icons.js";

export { icon };

export function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "for") node.htmlFor = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in node && key !== "list") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  add(node, children);
  return node;
}

export function add(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function toast(message, bad = false) {
  const box = document.getElementById("toasts");
  const node = h("div", { class: "toast" + (bad ? " bad" : ""), role: "status" }, message);
  box.append(node);
  setTimeout(() => node.remove(), bad ? 6000 : 3200);
}

const dialog = () => document.getElementById("dialog");

/** Show a dialog and resolve with what `build(close)` passes to `close(value)`; Escape resolves with null. */
export function openDialog(build) {
  return new Promise((resolve) => {
    const box = dialog();
    clear(box);
    const close = (value) => {
      box.close();
      resolve(value ?? null);
    };
    box.addEventListener("cancel", () => resolve(null), { once: true });
    box.append(build(close));
    box.showModal();
    const first = box.querySelector("[autofocus], input, textarea, button.primary");
    if (first) first.focus();
  });
}

export function confirmDialog(title, text, okLabel = "OK", danger = false) {
  return openDialog((close) =>
    h("div", {}, h("h3", {}, title), h("p", {}, text),
      h("div", { class: "actions" },
        h("button", { onclick: () => close(false) }, "Cancel"),
        h("button", { class: danger ? "danger solid" : "primary", onclick: () => close(true) }, okLabel))),
  ).then((value) => value === true);
}

export function promptDialog(title, label, value = "", okLabel = "Save", { type = "text", hint = "" } = {}) {
  return openDialog((close) => {
    const input = h("input", { type, value, autocomplete: type === "password" ? "new-password" : "off", autofocus: true });
    const form = h("form", { onsubmit: (event) => { event.preventDefault(); close(input.value); } },
      h("h3", {}, title),
      h("div", { class: "stack" }, h("label", { class: "field" }, label, input), hint && h("p", { class: "muted small" }, hint)),
      h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => close(null) }, "Cancel"),
        h("button", { class: "primary", type: "submit" }, okLabel)));
    return form;
  });
}

/** A password handed out once: shown big, with a copy button. */
export function secretDialog(title, intro, secret) {
  return openDialog((close) =>
    h("div", {}, h("h3", {}, title), h("p", {}, intro), h("div", { class: "secret-box" }, secret),
      h("p", { class: "muted small" }, "It is shown only now: copy it before closing."),
      h("div", { class: "actions" },
        h("button", { onclick: () => copy(secret) }, icon("copy", { size: 18 }), "Copy"),
        h("button", { class: "primary", onclick: () => close(true) }, "Done"))));
}

export async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied");
  } catch {
    toast("Could not copy: select the text and copy it by hand.", true);
  }
}

let openPopup = null;

/** A small menu next to `anchor`; `items` are `{label, icon, run, danger}`, "-" for a separator, or null for nothing. */
export function popupMenu(anchor, items) {
  closePopup();
  const rect = anchor.getBoundingClientRect();
  const menu = h("div", { class: "popup", role: "menu" },
    items.filter(Boolean).map((item) => item === "-" ? h("hr", {}) :
      h("button", { role: "menuitem", class: item.danger ? "danger" : "", onclick: () => { closePopup(); item.run(); } },
        item.icon && icon(item.icon, { size: 18 }), item.label)));
  document.body.append(menu);
  const width = menu.offsetWidth;
  const left = rect.right - width > 8 ? rect.right - width : rect.left; // aligned on the button's right edge when it fits
  menu.style.left = Math.max(8, Math.min(left, window.innerWidth - width - 8)) + "px";
  const below = rect.bottom + 4;
  menu.style.top = (below + menu.offsetHeight > window.innerHeight ? Math.max(8, rect.top - menu.offsetHeight - 4) : below) + "px";
  openPopup = menu;
  menu.querySelector("button")?.focus({ preventScroll: true });
  menu.addEventListener("keydown", (event) => {
    const buttons = [...menu.querySelectorAll("button")];
    const at = buttons.indexOf(document.activeElement);
    if (event.key === "Escape") { closePopup(); anchor.focus(); }
    else if (event.key === "ArrowDown") { event.preventDefault(); buttons[(at + 1) % buttons.length].focus(); }
    else if (event.key === "ArrowUp") { event.preventDefault(); buttons[(at - 1 + buttons.length) % buttons.length].focus(); }
  });
  setTimeout(() => document.addEventListener("click", closePopup, { once: true }), 0);
}

export function closePopup() {
  if (openPopup) openPopup.remove();
  openPopup = null;
}

// ---- the page frame ----------------------------------------------------------------------------------------

/** Open or close the navigation rail (it slides in over the page on small screens). */
export function toggleRail(open) {
  document.body.classList.toggle("rail-open", open);
}

/** A page's header: on a phone it starts with the button that opens the navigation. */
export function pageHead(title, ...actions) {
  return h("header", { class: "page-head" },
    h("button", { class: "ghost icon-btn open-rail", "aria-label": "Open navigation", onclick: () => toggleRail(true) }, icon("menu")),
    typeof title === "string" ? h("h1", { class: "title grow" }, title) : title,
    ...actions);
}

/** The round badge with someone's first letter. */
export function avatar(name, extra = "") {
  return h("span", { class: ("avatar " + extra).trim(), "aria-hidden": "true" }, (name || "?").trim().charAt(0) || "?");
}

// ---- the theme switch --------------------------------------------------------------------------------------

const THEMES = [["auto", "Auto", "auto"], ["light", "Light", "sun"], ["dark", "Dark", "moon"]];

/** Auto / Light / Dark, remembered in this browser (theme.js); null when theme.js did not load. */
export function themeSwitch() {
  const theme = window.claraTheme;
  if (!theme) return null; // the system's colours apply
  const box = h("div", { class: "theme-switch", role: "group", "aria-label": "Colour theme" });
  const draw = () => clear(box).append(...THEMES.map(([id, label, glyph]) =>
    h("button", { type: "button", "aria-pressed": String(theme.get() === id), title: id === "auto" ? "Follow the system" : `${label} theme`,
      onclick: () => { theme.set(id); for (const other of document.querySelectorAll(".theme-switch")) other.redraw?.(); } },
    icon(glyph, { size: 16 }), label)));
  box.redraw = draw;
  draw();
  return box;
}

// ---- dates -----------------------------------------------------------------------------------------------

export function parseDate(text) {
  if (!text) return null;
  const value = /[zZ]|[+-]\d\d:?\d\d$/.test(text) ? text : text + "Z";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function dateTime(text) {
  const date = parseDate(text);
  return date ? date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";
}

export function ago(text) {
  const date = parseDate(text);
  if (!date) return "never";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  const units = [["minute", 60], ["hour", 3600], ["day", 86400], ["month", 2592000], ["year", 31536000]];
  let best = ["minute", 60];
  for (const unit of units) if (seconds >= unit[1]) best = unit;
  const count = Math.floor(seconds / best[1]);
  return `${count} ${best[0]}${count === 1 ? "" : "s"} ago`;
}

export function duration(seconds) {
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  if (days) return `${days}d ${hours % 24}h`;
  return hours ? `${hours}h ${String(minutes % 60).padStart(2, "0")}m` : `${minutes}m`;
}

// ---- usage limits -------------------------------------------------------------------------------------------

export const tokenCount = (tokens) => Number(tokens).toLocaleString();

/** What a person typed as a daily limit (`500000`, `500k`, `2m`, `off`) as a whole number of credits (0: no limit), or null. */
export function parseTokens(text) {
  const word = text.trim().toLowerCase().replace(/[_,\s]/g, "");
  if (["off", "none", "no", "unlimited", "0"].includes(word)) return 0;
  const found = /^(\d+(?:\.\d+)?)([km]?)$/.exec(word);
  if (!found) return null;
  const exact = parseFloat(found[1]) * (found[2] === "m" ? 1e6 : found[2] === "k" ? 1e3 : 1);
  const tokens = Math.round(exact);
  return Math.abs(exact - tokens) < 1e-6 && tokens >= 1 && tokens <= 1e13 ? tokens : null; // a whole number of credits
}

/** Credits used today against the limit (`usage` as the server describes it): a bar, or just the count when there is none. */
export function usageBar(usage) {
  if (!usage.limit) return h("span", { class: "usage" }, h("span", {}, tokenCount(usage.used)), h("span", { class: "muted small" }, " no limit"));
  const share = Math.min(1, usage.used / usage.limit);
  const bar = h("span", { class: "usage-bar", "aria-hidden": "true" }, h("span", { class: "fill" }));
  bar.firstChild.style.width = `${Math.round(share * 100)}%`;
  return h("span", { class: "usage" + (usage.used >= usage.limit ? " over" : share >= 0.8 ? " near" : ""), title: `${Math.round(share * 100)}% of today's credits` },
    h("span", {}, `${tokenCount(usage.used)} / ${tokenCount(usage.limit)}`), bar);
}

export function randomId() {
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
