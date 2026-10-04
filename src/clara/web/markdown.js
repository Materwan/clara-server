// A small Markdown renderer that builds DOM nodes (never HTML strings), so nothing in an answer can become
// markup or script. Covers what a chat needs: headings, paragraphs, lists, quotes, code, tables, rules, links,
// bold, italic, strikethrough and inline code. Links open only http(s) and mailto addresses. Formulas ($x^2$,
// \(x^2\), $$...$$, \[...\]) are typeset by KaTeX, loaded the first time one shows up (see math below).

import { copy, h, icon } from "./ui.js";

const SAFE_LINK = /^(https?:\/\/|mailto:)/i;

export function renderMarkdown(source) {
  const root = h("div", { class: "md" });
  blocks(source.replace(/\r\n?/g, "\n").split("\n"), root);
  return root;
}

function blocks(lines, parent) {
  let i = 0;
  const paragraph = [];
  const flush = () => {
    if (paragraph.length) {
      const p = h("p", {});
      paragraph.forEach((line, index) => {
        if (index) p.append(document.createElement("br"));
        inline(line.trim(), p);
      });
      parent.append(p);
      paragraph.length = 0;
    }
  };
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if ((m = /^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)[^\n]*$/.exec(line))) {
      flush();
      const fence = m[2];
      const body = [];
      i++;
      while (i < lines.length && !new RegExp(`^\\s*${fence[0]}{${fence.length},}\\s*$`).test(lines[i])) body.push(lines[i++]);
      i++; // the closing fence (a missing one ends at the end: an answer still being written)
      parent.append(codeBlock(body.join("\n"), m[3]));
    } else if (!line.trim()) {
      flush();
      i++;
    } else if ((m = mathBlock(lines, i))) {
      flush();
      parent.append(math(m.tex, true));
      i = m.end;
    } else if ((m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) {
      flush();
      const level = Math.min(m[1].length + 1, 4); // an answer's # is a section, not the page's title
      const heading = h("h" + level, {});
      inline(m[2], heading);
      parent.append(heading);
      i++;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      parent.append(document.createElement("hr"));
      i++;
    } else if (/^\s*>/.test(line)) {
      flush();
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
      const box = h("blockquote", {});
      blocks(quote, box);
      parent.append(box);
    } else if (isTable(lines, i)) {
      flush();
      i = table(lines, i, parent);
    } else if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      flush();
      i = list(lines, i, parent);
    } else {
      paragraph.push(line);
      i++;
    }
  }
  flush();
}

function list(lines, start, parent) {
  const first = /^(\s*)([-*+]|\d+[.)])\s+/.exec(lines[start]);
  const ordered = /\d/.test(first[2]);
  const indent = first[1].length;
  const node = h(ordered ? "ol" : "ul", {});
  if (ordered && parseInt(first[2], 10) !== 1) node.start = parseInt(first[2], 10);
  let i = start;
  while (i < lines.length) {
    const item = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
    if (!item || item[1].length !== indent || /\d/.test(item[2]) !== ordered) break;
    const text = [item[3]];
    const nested = [];
    i++;
    while (i < lines.length && lines[i].trim()) {
      const next = /^(\s*)([-*+]|\d+[.)])\s+/.exec(lines[i]);
      if (next && next[1].length <= indent) break; // a sibling, or an item of the list around this one
      if (next || nested.length) nested.push(lines[i]); // a nested list, and what follows it
      else text.push(lines[i].trim()); // the item's text goes on
      i++;
    }
    const li = h("li", {});
    inline(text.join(" ").trim(), li);
    if (nested.length) {
      const margin = Math.min(...nested.filter((line) => line.trim()).map((line) => line.length - line.trimStart().length));
      blocks(nested.map((line) => line.slice(margin)), li);
    }
    node.append(li);
    while (i < lines.length && !lines[i].trim() && /^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i + 1] || "")) i++; // loose list
  }
  parent.append(node);
  return i;
}

const split = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());

function isTable(lines, i) {
  return i + 1 < lines.length && lines[i].includes("|") && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1]);
}

function table(lines, start, parent) {
  const head = split(lines[start]);
  const align = split(lines[start + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
  const node = h("table", {}, h("thead", {}, h("tr", {}, head.map((cell, n) => cellNode("th", cell, align[n])))));
  const body = h("tbody", {});
  let i = start + 2;
  while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
    body.append(h("tr", {}, split(lines[i]).map((cell, n) => cellNode("td", cell, align[n]))));
    i++;
  }
  node.append(body);
  parent.append(node);
  return i;
}

function cellNode(tag, text, align) {
  const cell = h(tag, {});
  if (align) cell.align = align;
  inline(text, cell);
  return cell;
}

function codeBlock(code, language) {
  const pre = h("pre", {}, h("code", {}, code));
  return h("div", { class: "codeblock" },
    h("div", { class: "lang" }, h("span", {}, language || "text"),
      h("button", { class: "ghost", onclick: () => copy(code), "aria-label": "Copy the code" }, icon("copy", { size: 14 }), "Copy")), pre);
}

// ---- inline ---------------------------------------------------------------------------------------------

const INLINE = new RegExp(
  [
    "(`+)([\\s\\S]*?[^`])\\1(?!`)", // 1,2 code
    "\\*\\*(?=\\S)([\\s\\S]*?\\S)\\*\\*", // 3 bold
    "__(?=\\S)([\\s\\S]*?\\S)__", // 4 bold
    "~~(?=\\S)([\\s\\S]*?\\S)~~", // 5 strike
    "\\*(?=[^\\s*])([\\s\\S]*?[^\\s*])\\*", // 6 italic
    "(?<![\\w])_(?=[^\\s_])([\\s\\S]*?[^\\s_])_(?![\\w])", // 7 italic
    "\\[([^\\]\\n]+)\\]\\(([^)\\s]+)(?:\\s+\"[^\"]*\")?\\)", // 8,9 link
    "<(https?:\\/\\/[^>\\s]+)>", // 10 <autolink>
    "(https?:\\/\\/[^\\s<>()\\[\\]]+[^\\s<>()\\[\\].,;:!?'\"])", // 11 bare url
    "\\\\\\$", // an escaped dollar sign: \$ (no group)
    "\\$\\$([^`]+?)\\$\\$", // 12 $$ display $$
    "\\\\\\[([\\s\\S]+?)\\\\\\]", // 13 \[ display \]
    "\\\\\\(([\\s\\S]+?)\\\\\\)", // 14 \( inline \)
    // 15 $ inline $: it hugs its formula and no digit follows, so "from $5 to $10" is not one
    "\\$(?![\\s$])((?:[^$`\\\\\\n]|\\\\[\\s\\S])+?)(?<!\\s)\\$(?!\\d)",
  ].join("|"),
);

export function inline(text, parent) {
  let rest = text;
  while (rest) {
    const m = INLINE.exec(rest);
    if (!m) break;
    if (m.index) parent.append(rest.slice(0, m.index));
    if (m[2] !== undefined) parent.append(h("code", {}, m[2].trim()));
    else if (m[3] !== undefined || m[4] !== undefined) wrap("strong", m[3] ?? m[4], parent);
    else if (m[5] !== undefined) wrap("del", m[5], parent);
    else if (m[6] !== undefined || m[7] !== undefined) wrap("em", m[6] ?? m[7], parent);
    else if (m[8] !== undefined) {
      if (SAFE_LINK.test(m[9])) wrap("a", m[8], parent, { href: m[9], target: "_blank", rel: "noopener noreferrer nofollow" });
      else parent.append(m[0]);
    } else if (m[10] !== undefined || m[11] !== undefined) link(m[10] ?? m[11], parent);
    else if (m[12] !== undefined || m[13] !== undefined) parent.append(math(m[12] ?? m[13], true));
    else if (m[14] !== undefined || m[15] !== undefined) parent.append(math(m[14] ?? m[15], false));
    else parent.append("$");
    rest = rest.slice(m.index + m[0].length);
  }
  if (rest) parent.append(rest);
}

function wrap(tag, text, parent, props) {
  const node = h(tag, props);
  inline(text, node);
  parent.append(node);
}

function link(url, parent) {
  parent.append(h("a", { href: url, target: "_blank", rel: "noopener noreferrer nofollow" }, url));
}

// ---- formulas -------------------------------------------------------------------------------------------
// KaTeX (web/katex/) builds DOM nodes, never HTML strings, so what the model writes cannot become markup, and the
// page's strict Content-Security-Policy holds. Its script is only fetched once an answer has a formula; until it is
// there the formulas show their source. An answer is repainted as it streams, so each formula is typeset once.

const KATEX_SCRIPT = "/katex/katex.min.js";
const waiting = new Set(); // formulas on screen that are waiting for KaTeX
const typeset = new Map(); // "d|tex" -> its nodes: what a streaming answer repaints is taken from here
let loading = null;

function loadKatex() {
  if (window.katex || loading) return;
  const script = document.createElement("script");
  script.src = KATEX_SCRIPT;
  script.onload = () => {
    for (const node of waiting) paintMath(node);
    waiting.clear();
  };
  script.onerror = () => { loading = null; }; // the formulas stay as source; the next answer tries again
  loading = script;
  document.head.append(script);
}

/** A formula as a node: typeset when KaTeX is there, else its source for now. */
function math(tex, display) {
  tex = tex.trim();
  const node = h("span", { class: display ? "math math-block" : "math" }, tex);
  node.dataset.tex = tex;
  if (window.katex) paintMath(node);
  else {
    waiting.add(node);
    loadKatex();
  }
  return node;
}

function paintMath(node) {
  const display = node.classList.contains("math-block");
  const key = (display ? "d|" : "i|") + node.dataset.tex;
  let done = typeset.get(key);
  if (!done) {
    const target = document.createElement("span");
    try {
      window.katex.render(node.dataset.tex, target, { displayMode: display, throwOnError: true, strict: "ignore", trust: false, maxExpand: 1000 });
      done = [...target.childNodes];
    } catch {
      return; // not (yet) a formula: half of one while it streams in. Its source stays
    }
    if (typeset.size > 300) typeset.clear();
    typeset.set(key, done);
  }
  node.replaceChildren(...done.map((child) => child.cloneNode(true)));
  node.classList.add("typeset");
}

/** Display math that starts at lines[start] and is alone in its lines: {tex, end}, or null if it is not one. */
function mathBlock(lines, start) {
  const opener = /^\s*(\$\$|\\\[)/.exec(lines[start]);
  if (!opener) return null;
  const closer = opener[1] === "$$" ? "$$" : "\\]";
  const body = [lines[start].slice(opener[0].length)];
  for (let i = start; i < lines.length; i++) {
    if (i > start) body.push(lines[i]);
    const last = body[body.length - 1];
    const at = last.indexOf(closer);
    if (at !== -1) {
      if (last.slice(at + closer.length).trim()) return null; // text follows the formula: it is part of a paragraph
      body[body.length - 1] = last.slice(0, at);
      return { tex: body.join("\n"), end: i + 1 };
    }
    if (i > start && !last.trim()) return null; // a blank line cannot be in a formula: it was never closed
  }
  return { tex: body.join("\n"), end: lines.length }; // not closed yet: the answer is still being written
}
